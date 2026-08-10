from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_XML_DIR = DATA_DIR / "raw_xml"
REPORT_DIR = DATA_DIR / "reports"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "registry_schema.sql"
DEFAULT_DB = DATA_DIR / "who_ictrp_cancer_trials.db"
SOURCE_NAME = "WHO_ICTRP"
PARSER_VERSION = "who_ictrp_xml_0.2"
ADVANCED_SEARCH_URL = "https://trialsearch.who.int/AdvSearch.aspx"
TRIAL_URL = "https://trialsearch.who.int/Trial2.aspx?TrialID={trial_id}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.lower()
    text = re.sub(r"[\u2010-\u2015_/.,;+():\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def load_term_config(path: Path) -> dict[str, list[str]]:
    terms: dict[str, list[str]] = {"hard_terms": [], "context_terms": [], "review_terms": []}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith(" ") and line.endswith(":"):
            current = line[:-1].strip()
            terms.setdefault(current, [])
            continue
        stripped = line.strip()
        if current and stripped.startswith("- "):
            term = stripped[2:].strip().strip("'\"").lower()
            if term:
                terms[current].append(term)
    return {key: sorted(set(value), key=len, reverse=True) for key, value in terms.items()}


class TermMatcher:
    def __init__(self, terms: list[str]) -> None:
        self.terms = [(normalize_text(term), term) for term in terms if normalize_text(term)]

    def match(self, text: str) -> list[str]:
        normalized = f" {normalize_text(text)} "
        return [original for normalized_term, original in self.terms if f" {normalized_term} " in normalized]


def split_lines(value: str) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw_part in re.split(r"[\n|;]+", value or ""):
        part = raw_part.strip()
        key = part.casefold()
        if part and key not in seen:
            seen.add(key)
            unique.append(part)
    return unique


def normalize_status(status: str) -> str:
    value = normalize_text(status)
    if not value:
        return "unknown"
    if "recruit" in value or "pending" in value or "not yet" in value:
        if "not" in value or "pending" in value:
            return "not_yet_recruiting"
        return "recruiting"
    if "complete" in value or "closed" in value:
        return "completed"
    if "suspend" in value:
        return "suspended"
    if "terminate" in value or "withdraw" in value:
        return "terminated_or_withdrawn"
    return "other"


def source_from_trial_id(trial_id: str) -> str:
    value = trial_id.upper()
    if value.startswith("NCT"):
        return "clinicaltrials.gov"
    if value.startswith("CHICTR"):
        return "chictr"
    if value.startswith("UMIN"):
        return "UMIN_CTR"
    if value.startswith("JPRN"):
        return "japan_registry_network"
    if value.startswith("EUCT") or value.startswith("CTIS") or value.startswith("20") and "-" in value:
        return "EU_CTI"
    return "unknown"


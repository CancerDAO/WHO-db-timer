from __future__ import annotations

import re


def split_eligibility_criteria(text: str) -> list[tuple[str, str, int]]:
    """Split a registry eligibility block while preserving unknown-format text."""
    sections: dict[str, list[str]] = {"inclusion": [], "exclusion": [], "unknown": []}
    current = "unknown"
    for raw_line in re.split(r"[\r\n]+", text or ""):
        line = raw_line.strip()
        if not line:
            continue
        heading = line.lower().rstrip(":")
        if "inclusion" in heading and "criteria" in heading:
            current = "inclusion"
            continue
        if "exclusion" in heading and "criteria" in heading:
            current = "exclusion"
            continue
        line = re.sub(r"^(?:[-*\u2022]+|\d+[.):])\s*", "", line).strip()
        if line:
            sections[current].append(line)
    if not any(sections.values()) and text.strip():
        sections["unknown"].append(text.strip())
    rows: list[tuple[str, str, int]] = []
    order = 0
    for criterion_type in ("inclusion", "exclusion", "unknown"):
        for criterion in sections[criterion_type]:
            order += 1
            rows.append((criterion_type, criterion, order))
    return rows

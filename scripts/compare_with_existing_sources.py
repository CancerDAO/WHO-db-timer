from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHO_DB = PROJECT_ROOT / "data" / "who_ictrp_cancer_trials.db"
DEFAULT_EXISTING_ROOT = PROJECT_ROOT.parent / "global-cancer-trials-db"
SOURCE_DBS = [
    "sources/clinicaltrials_gov/data/ctgov_cancer_trials.db",
    "sources/china_chictr/data/chictr_cancer_trials.db",
    "sources/eu_ctis/data/ctis_cancer_trials.db",
    "sources/japan_umin_ctr/data/umin_ctr_cancer_trials.db",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare WHO ICTRP cancer DB against the existing multi-source project by registry IDs.")
    parser.add_argument("--who-db", type=Path, default=DEFAULT_WHO_DB)
    parser.add_argument("--existing-root", type=Path, default=DEFAULT_EXISTING_ROOT)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "reports" / "who_vs_existing_comparison.json")
    return parser.parse_args()


def norm(value: str) -> str:
    return (value or "").strip().upper()


def collect_who_ids(path: Path) -> tuple[set[str], dict[str, list[str]]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ids: set[str] = set()
    by_trial: dict[str, list[str]] = {}
    for row in conn.execute("SELECT trial_uid, primary_registry_id FROM trial_master"):
        v = norm(row["primary_registry_id"])
        if v:
            ids.add(v); by_trial.setdefault(row["trial_uid"], []).append(v)
    for row in conn.execute("SELECT trial_uid, registry_id FROM trial_registry_ids"):
        v = norm(row["registry_id"])
        if v:
            ids.add(v); by_trial.setdefault(row["trial_uid"], []).append(v)
    conn.close()
    return ids, by_trial


def collect_existing_ids(root: Path) -> tuple[set[str], dict[str, int]]:
    ids: set[str] = set()
    counts: dict[str, int] = {}
    for rel in SOURCE_DBS:
        path = root / rel
        if not path.exists():
            counts[rel] = -1
            continue
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        local: set[str] = set()
        for row in conn.execute("SELECT primary_registry_id FROM trial_master"):
            v = norm(row["primary_registry_id"])
            if v:
                ids.add(v); local.add(v)
        for row in conn.execute("SELECT registry_id FROM trial_registry_ids"):
            v = norm(row["registry_id"])
            if v:
                ids.add(v); local.add(v)
        counts[rel] = len(local)
        conn.close()
    return ids, counts


def main() -> None:
    args = parse_args()
    who_ids, _ = collect_who_ids(args.who_db)
    existing_ids, existing_counts = collect_existing_ids(args.existing_root)
    overlap = who_ids & existing_ids
    only_who = sorted(who_ids - existing_ids)[:200]
    missing_from_who = sorted(existing_ids - who_ids)[:200]
    report = {
        "who_db": str(args.who_db),
        "existing_root": str(args.existing_root),
        "who_registry_ids": len(who_ids),
        "existing_registry_ids": len(existing_ids),
        "overlap_ids": len(overlap),
        "who_only_ids_sample": only_who,
        "existing_only_ids_sample": missing_from_who,
        "existing_source_unique_id_counts": existing_counts,
        "note": "For small WHO test builds, existing_only_ids_sample is expected to be large. Run a broader WHO XML export for meaningful gap analysis.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


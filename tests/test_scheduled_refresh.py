from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from refresh_production_who_db import production_database_mode, rotate_backups, validate_database


def create_valid_database(path: Path, trials: int = 3) -> None:
    with sqlite3.connect(path) as conn:
        for table in (
            "trial_registry_ids", "trial_interventions", "trial_eligibility_criteria",
            "trial_country_records", "trial_sites",
        ):
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE trial_master (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE trial_search_fts (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE database_metadata (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO trial_master(id) VALUES (?)", [(index,) for index in range(trials)])
        conn.executemany("INSERT INTO trial_search_fts(id) VALUES (?)", [(index,) for index in range(trials)])
        conn.execute("INSERT INTO database_metadata(id) VALUES (1)")


class ScheduledRefreshTests(unittest.TestCase):
    def test_valid_staging_database_passes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "staging.db"
            create_valid_database(database, 3)
            result = validate_database(
                database, old_count=4, minimum_trials=2, minimum_old_ratio=0.70,
            )
        self.assertEqual(result["trial_count"], 3)
        self.assertEqual(result["integrity_check"], "ok")

    def test_large_count_regression_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "staging.db"
            create_valid_database(database, 3)
            with self.assertRaisesRegex(RuntimeError, "production count"):
                validate_database(
                    database, old_count=10, minimum_trials=2, minimum_old_ratio=0.70,
                )

    def test_backup_rotation_keeps_configured_count(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            production = Path(folder) / "who.db"
            production.write_bytes(b"first")
            for index in range(4):
                production.write_bytes(f"version-{index}".encode())
                rotate_backups(production, 2)
            backups = list((production.parent / "backups").glob("who-*.db"))
        self.assertLessEqual(len(backups), 2)

    def test_hybrid_database_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "hybrid.db"
            create_valid_database(database, 3)
            conn = sqlite3.connect(database)
            try:
                conn.execute("CREATE TABLE trial_source_provenance (source_name TEXT)")
                conn.execute("INSERT INTO trial_source_provenance VALUES ('clinicaltrials.gov')")
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(production_database_mode(database), "hybrid")


if __name__ == "__main__":
    unittest.main()

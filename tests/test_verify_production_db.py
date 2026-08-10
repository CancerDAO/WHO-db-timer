import json
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_production_db.py"


class VerifyProductionDatabaseTests(unittest.TestCase):
    def test_valid_database_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE trial_master (trial_key TEXT PRIMARY KEY);
                    CREATE TABLE trial_registry_ids (id INTEGER PRIMARY KEY);
                    CREATE TABLE trial_interventions (id INTEGER PRIMARY KEY);
                    CREATE TABLE trial_eligibility_criteria (id INTEGER PRIMARY KEY);
                    CREATE TABLE trial_country_records (id INTEGER PRIMARY KEY);
                    CREATE TABLE trial_sites (id INTEGER PRIMARY KEY);
                    CREATE VIRTUAL TABLE trial_search_fts USING fts5(trial_key);
                    CREATE TABLE database_metadata (key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO trial_master VALUES ('NCT00000001');
                    INSERT INTO trial_search_fts VALUES ('NCT00000001');
                    INSERT INTO database_metadata VALUES ('database_built_at', '2026-08-10T00:00:00Z');
                    """
                )
                conn.commit()
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--db", str(path), "--minimum-trials", "1"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            result = json.loads(completed.stdout)
            self.assertTrue(result["passed"])
            self.assertEqual(result["trial_count"], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp_service"))

from matching_db import optimize_database  # noqa: E402
from plan_search import execute_search_plan  # noqa: E402
from query_service import (  # noqa: E402
    database_metadata,
    database_summary,
    get_trial,
    search_trials_multidimensional,
)


class MatchingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "trials.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript((PROJECT_ROOT / "schemas" / "registry_schema.sql").read_text(encoding="utf-8"))
        conn.executescript(
            """
            CREATE TABLE who_search_runs (
                search_id INTEGER PRIMARY KEY,
                field_name TEXT, term TEXT, total_records INTEGER, total_trials INTEGER,
                searched_at TEXT, raw_result_summary TEXT, recruitment_status TEXT,
                query_json TEXT, xml_path TEXT
            );
            CREATE TABLE who_trial_records (trial_id TEXT PRIMARY KEY);
            CREATE TABLE who_recall_hits (id INTEGER PRIMARY KEY, trial_id TEXT);
            """
        )
        conn.execute(
            "INSERT INTO who_search_runs VALUES (1, 'title_condition', 'core', 3, 3, ?, '', 'recruiting', '{}', '')",
            ("2026-07-09T00:45:35+00:00",),
        )
        self._insert_trial(
            conn,
            uid="who:NCT0001",
            registry_id="NCT0001",
            secondary_id="CTIS2024-000001-00",
            title="KRAS G12C colorectal cancer targeted treatment",
            disease="Metastatic colorectal cancer",
            intervention="Sotorasib and panitumumab",
            status="recruiting",
            country="China",
            criteria="Inclusion Criteria:\n* KRAS G12C mutation\nExclusion Criteria:\n* Active CNS metastases",
        )
        self._insert_trial(
            conn,
            uid="who:NCT0002",
            registry_id="NCT0002",
            secondary_id="",
            title="EGFR colorectal cancer study",
            disease="Colorectal cancer",
            intervention="Cetuximab",
            status="recruiting",
            country="United States",
            criteria="EGFR expression required",
        )
        self._insert_trial(
            conn,
            uid="who:NCT0004",
            registry_id="NCT0004",
            secondary_id="",
            title="KRAS G12C colorectal cancer observational registry",
            disease="Metastatic colorectal cancer",
            intervention="Biomarker observation",
            status="recruiting",
            country="China",
            criteria="KRAS G12C registry",
            study_type="Observational",
        )
        self._insert_trial(
            conn,
            uid="who:NCT0003",
            registry_id="NCT0003",
            secondary_id="",
            title="KRAS G12C lung cancer study",
            disease="Non-small cell lung cancer",
            intervention="Adagrasib",
            status="completed",
            country="China",
            criteria="KRAS G12C required",
        )
        optimize_database(
            conn,
            built_at="2026-07-09T00:46:11+00:00",
            coverage_scope={"source": "WHO ICTRP", "recruitment_status": ["recruiting"]},
        )
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _insert_trial(
        self,
        conn: sqlite3.Connection,
        *,
        uid: str,
        registry_id: str,
        secondary_id: str,
        title: str,
        disease: str,
        intervention: str,
        status: str,
        country: str,
        criteria: str,
        study_type: str = "Interventional",
    ) -> None:
        conn.execute(
            """INSERT INTO trial_master(
                trial_uid, primary_registry_id, primary_source, title, scientific_title,
                recruitment_status_normalized, study_type_normalized, disease_text, intervention_summary,
                countries, registration_date, last_update_date, last_fetched_at,
                cancer_recall_confidence
            ) VALUES (?, ?, 'WHO_ICTRP', ?, ?, ?, ?, ?, ?, ?, '2026-01-01',
                      '1 July 2026', '2026-07-09T00:45:35+00:00', 'high')""",
            (uid, registry_id, title, title, status, study_type, disease, intervention, country),
        )
        conn.execute(
            "INSERT INTO trial_registry_ids(trial_uid, registry_source, registry_id, id_type, is_primary) VALUES (?, 'WHO', ?, 'main_id', 1)",
            (uid, registry_id),
        )
        if secondary_id:
            conn.execute(
                "INSERT INTO trial_registry_ids(trial_uid, registry_source, registry_id, id_type, is_primary) VALUES (?, 'CTIS', ?, 'secondary_id', 0)",
                (uid, secondary_id),
            )
        conn.execute(
            "INSERT INTO trial_interventions(trial_uid, intervention_name_raw, intervention_type) VALUES (?, ?, 'drug')",
            (uid, intervention),
        )
        conn.execute(
            "INSERT INTO trial_eligibility_criteria(trial_uid, criterion_type, criterion_text, language, criterion_order) VALUES (?, 'unknown', ?, 'en', 1)",
            (uid, criteria),
        )
        conn.execute(
            "INSERT INTO trial_country_records(trial_uid, country, source_name) VALUES (?, ?, 'WHO_ICTRP')",
            (uid, country),
        )

    def test_metadata_and_cached_summary_include_watermark(self) -> None:
        metadata = database_metadata(self.db_path)
        self.assertEqual(metadata["database_as_of"], "2026-07-09T00:45:35+00:00")
        self.assertEqual(metadata["database_built_at"], "2026-07-09T00:46:11+00:00")
        summary = database_summary(self.db_path)
        self.assertEqual(summary["counts"]["trial_master"], 4)
        self.assertEqual(summary["country_classifiable_trials"], 4)

    def test_multidimensional_search_ands_dimensions(self) -> None:
        payload = search_trials_multidimensional(
            self.db_path,
            condition_terms=["colorectal cancer"],
            biomarker_terms=["KRAS G12C"],
            interventional_only=True,
            country="China",
        )
        self.assertEqual([row["primary_registry_id"] for row in payload["results"]], ["NCT0001"])
        self.assertEqual(payload["results"][0]["matched_dimensions"], ["condition", "biomarker"])
        self.assertEqual(payload["results"][0]["matching_site_count"], 0)
        self.assertEqual(payload["results"][0]["matching_country_record_count"], 1)

    def test_terms_within_dimension_are_ored_and_status_is_filtered(self) -> None:
        payload = search_trials_multidimensional(
            self.db_path,
            condition_terms=["colorectal cancer", "lung cancer"],
            biomarker_terms=["KRAS G12C"],
            interventional_only=True,
        )
        self.assertEqual([row["primary_registry_id"] for row in payload["results"]], ["NCT0001"])

    def test_intervention_dimension_and_secondary_id_lookup(self) -> None:
        payload = search_trials_multidimensional(
            self.db_path,
            condition_terms=["colorectal"],
            intervention_terms=["sotorasib", "adagrasib"],
        )
        self.assertEqual(payload["results"][0]["primary_registry_id"], "NCT0001")
        trial = get_trial(self.db_path, "ctis2024-000001-00")
        self.assertTrue(trial["found"])
        self.assertEqual(trial["primary_registry_id"], "NCT0001")
        self.assertEqual(trial["parsed_criteria"]["inclusion"], ["KRAS G12C mutation"])
        self.assertEqual(trial["parsed_criteria"]["exclusion"], ["Active CNS metastases"])
        self.assertEqual(trial["sites"], [])
        self.assertEqual(trial["country_records"][0]["country"], "China")


    def test_matching_plan_excludes_observational_studies(self) -> None:
        plan = {
            "keyword_groups": [{
                "label": "KRAS CRC",
                "queries": [{"condition": "colorectal cancer", "term": "KRAS G12C"}],
            }]
        }
        payload = execute_search_plan(self.db_path, plan)
        self.assertEqual([row["primary_registry_id"] for row in payload["results"]], ["NCT0001"])
        audit = payload["query_audit"][0]
        self.assertTrue(audit["interventional_only"])

    def test_query_cap_uses_one_row_probe_for_exact_truncation(self) -> None:
        exact_plan = {
            "keyword_groups": [{
                "label": "exact",
                "queries": [{"condition": "metastatic colorectal cancer", "term": "KRAS G12C"}],
            }]
        }
        exact = execute_search_plan(self.db_path, exact_plan, max_per_query=1)
        self.assertFalse(exact["query_audit"][0]["truncated"])
        self.assertTrue(exact["query_audit"][0]["complete"])

        broad_plan = {
            "keyword_groups": [{
                "label": "broad",
                "queries": [{"condition": "colorectal cancer", "term": ""}],
            }]
        }
        broad = execute_search_plan(self.db_path, broad_plan, max_per_query=1)
        self.assertTrue(broad["query_audit"][0]["truncated"])
        self.assertTrue(broad["query_audit"][0]["has_more"])
    def test_original_search_plan_is_unioned_and_deduplicated(self) -> None:
        plan = {
            "keyword_groups": [
                {
                    "label": "疾病+突变特异",
                    "source": "both",
                    "queries": [{"condition": "colorectal cancer", "term": "KRAS G12C"}],
                },
                {
                    "label": "泛化-实体瘤",
                    "source": "nct",
                    "queries": [{"condition": None, "term": "KRAS G12C"}],
                },
            ]
        }
        payload = execute_search_plan(self.db_path, plan, country="China")
        self.assertEqual(payload["search_stats"]["total_queries"], 2)
        self.assertEqual(payload["search_stats"]["unique_after_dedup"], 1)
        self.assertEqual(payload["results"][0]["primary_registry_id"], "NCT0001")
        self.assertEqual(payload["results"][0]["matched_by"], ["疾病+突变特异", "泛化-实体瘤"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from who_common import split_lines


class SplitLinesTests(unittest.TestCase):
    def test_stably_deduplicates_case_insensitively(self):
        self.assertEqual(
            split_lines("United States | China | china | United States | France"),
            ["United States", "China", "France"],
        )


if __name__ == "__main__":
    unittest.main()

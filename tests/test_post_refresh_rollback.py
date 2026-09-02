from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_mcp_or_rollback import decode_message, read_env, restore_backup


class PostRefreshRollbackTests(unittest.TestCase):
    def test_read_env_ignores_comments_and_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "who.env"
            path.write_text(
                "# comment\nWHO_MCP_HOST=127.0.0.1\nWHO_MCP_API_KEY='secret'\n",
                encoding="utf-8",
            )
            values = read_env(path)
        self.assertEqual(values["WHO_MCP_HOST"], "127.0.0.1")
        self.assertEqual(values["WHO_MCP_API_KEY"], "secret")

    def test_decode_json_and_sse_messages(self) -> None:
        expected = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
        self.assertEqual(decode_message('{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}'), expected)
        self.assertEqual(
            decode_message('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}\n\n'),
            expected,
        )

    def test_restore_backup_replaces_production_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            production = root / "who.db"
            backup = root / "backup.db"
            production.write_bytes(b"new")
            backup.write_bytes(b"old")
            restore_backup(production, backup)
            self.assertEqual(production.read_bytes(), b"old")
            self.assertFalse((root / "who.db.rollback").exists())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "control_hub_agent.py"
SPEC = importlib.util.spec_from_file_location("control_hub_agent", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load module spec from {MODULE_PATH}")
CONTROL_HUB_AGENT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTROL_HUB_AGENT
SPEC.loader.exec_module(CONTROL_HUB_AGENT)


class GeneratedInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        CONTROL_HUB_AGENT.init_db(conn)
        return conn

    def test_normalize_priority_thresholds(self) -> None:
        self.assertEqual(CONTROL_HUB_AGENT.normalize_priority(170), 1)
        self.assertEqual(CONTROL_HUB_AGENT.normalize_priority(120), 2)
        self.assertEqual(CONTROL_HUB_AGENT.normalize_priority(80), 3)
        self.assertEqual(CONTROL_HUB_AGENT.normalize_priority(10), 4)
        self.assertEqual(CONTROL_HUB_AGENT.normalize_priority(None), 3)

    def test_scan_chat_workstream_tasks_upserts_and_prunes(self) -> None:
        report = self.root / "chat_work_brief.json"
        report.write_text(
            json.dumps(
                {
                    "generated_at": "2026-02-26T18:00:00Z",
                    "recommendations": [
                        {"topic": "general", "why_now": "Close one open loop."},
                    ],
                    "workstreams": [
                        {
                            "topic": "general",
                            "latest_title": "Finish inventory dashboard",
                            "priority_score": 140,
                            "latest_updated_at": 1772128000,
                            "thread_count": 5,
                            "blocked_signals": 0,
                            "done_signals": 0,
                        },
                        {
                            "topic": "security",
                            "latest_title": "Verify access gate",
                            "priority_score": 90,
                            "latest_updated_at": 1772128100,
                            "thread_count": 2,
                            "blocked_signals": 1,
                            "done_signals": 0,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        conn = self._conn()
        count, status = CONTROL_HUB_AGENT.scan_chat_workstream_tasks(conn, report)
        self.assertEqual(status, "ok")
        self.assertEqual(count, 2)
        rows = conn.execute(
            "SELECT external_id, priority, status FROM tasks WHERE source = 'chat-workstream' ORDER BY external_id"
        ).fetchall()
        self.assertEqual([r["external_id"] for r in rows], ["general", "security"])
        self.assertEqual(rows[0]["priority"], 2)
        self.assertIn("blocked", rows[1]["status"])

        report.write_text(
            json.dumps(
                {
                    "generated_at": "2026-02-26T18:05:00Z",
                    "workstreams": [
                        {
                            "topic": "general",
                            "latest_title": "Only one stream remains",
                            "priority_score": 60,
                            "latest_updated_at": 1772128300,
                            "thread_count": 1,
                            "blocked_signals": 0,
                            "done_signals": 1,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        count, status = CONTROL_HUB_AGENT.scan_chat_workstream_tasks(conn, report)
        self.assertEqual(status, "ok")
        self.assertEqual(count, 1)
        rows = conn.execute(
            "SELECT external_id, title FROM tasks WHERE source = 'chat-workstream' ORDER BY external_id"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "general")
        self.assertIn("Only one stream remains", rows[0]["title"])
        conn.close()

    def test_scan_venture_repo_tasks_sets_priority_by_risk(self) -> None:
        report = self.root / "venture_autonomy_report.json"
        report.write_text(
            json.dumps(
                {
                    "generated_at": "2026-02-26T18:20:00Z",
                    "repos": [
                        {
                            "root": "/tmp/repo-a",
                            "name": "repo-a",
                            "score": 100,
                            "dirty": False,
                            "gaps": [],
                            "safe_checks": ["make test"],
                            "check_results": [{"command": "make test", "status": "pass"}],
                        },
                        {
                            "root": "/tmp/repo-b",
                            "name": "repo-b",
                            "score": 60,
                            "dirty": True,
                            "gaps": ["Missing CI workflow"],
                            "safe_checks": ["pytest"],
                            "check_results": [{"command": "pytest", "status": "fail"}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        conn = self._conn()
        count, status = CONTROL_HUB_AGENT.scan_venture_repo_tasks(conn, report)
        self.assertEqual(status, "ok")
        self.assertEqual(count, 2)
        rows = conn.execute(
            "SELECT external_id, priority, status FROM tasks WHERE source = 'venture-repo' ORDER BY external_id"
        ).fetchall()
        self.assertEqual(rows[0]["external_id"], "/tmp/repo-a")
        self.assertEqual(rows[0]["priority"], 4)
        self.assertIn("healthy", rows[0]["status"])
        self.assertEqual(rows[1]["external_id"], "/tmp/repo-b")
        self.assertEqual(rows[1]["priority"], 1)
        self.assertIn("failing-check", rows[1]["status"])
        conn.close()


if __name__ == "__main__":
    unittest.main()

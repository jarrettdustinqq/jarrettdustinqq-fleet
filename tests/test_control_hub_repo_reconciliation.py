from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "control_hub_agent.py"
SPEC = importlib.util.spec_from_file_location("control_hub_agent_reconciliation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load module spec from {MODULE_PATH}")
HUB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HUB
SPEC.loader.exec_module(HUB)


class RepoReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "control-hub.db"
        self.chat_report = self.root / "missing-chat-report.json"
        self.venture_report = self.root / "missing-venture-report.json"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _seed_repo(
        self,
        path: str = "/srv/preserved-repo",
        *,
        inventory_status: str = "active",
    ) -> None:
        conn = HUB.db_connect(self.db_path)
        HUB.init_db(conn)
        conn.execute(
            """
            INSERT INTO repos (
                path, name, branch, dirty, ahead, behind,
                focus_level, next_action, inventory_status, updated_at
            ) VALUES (?, 'preserved-repo', 'main', 0, 0, 0, 3, ?, ?, ?)
            """,
            (
                path,
                "keep this operator state",
                inventory_status,
                "2026-08-20T12:00:00+00:00",
            ),
        )
        conn.commit()
        conn.close()

    def _run_scan(self, projects_root: Path) -> dict[str, int | str]:
        return HUB.run_scan(
            self.db_path,
            projects_root,
            None,
            chat_work_json=self.chat_report,
            venture_report_json=self.venture_report,
        )

    def test_failed_root_refuses_before_database_open(self) -> None:
        with self.assertRaises(HUB.RepoDiscoveryError):
            self._run_scan(self.root / "missing")

        self.assertFalse(self.db_path.exists())

    def test_verified_empty_root_stale_marks_without_deleting_operator_state(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()

        summary = self._run_scan(projects_root)

        self.assertEqual(summary["repo_scan_status"], HUB.REPO_SCAN_COMPLETE)
        self.assertEqual(summary["repos"], 0)
        self.assertEqual(summary["stale_repos"], 1)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT focus_level, next_action, inventory_status, missing_since
            FROM repos WHERE path = '/srv/preserved-repo'
            """
        ).fetchone()
        scan_run = conn.execute(
            """
            SELECT status, observed_repo_count, stale_repo_count
            FROM repo_scan_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        conn.close()

        self.assertEqual(row[:3], (3, "keep this operator state", "stale"))
        self.assertIsNotNone(row[3])
        self.assertEqual(scan_run, (HUB.REPO_SCAN_COMPLETE, 0, 1))

    def test_repeated_empty_scan_preserves_original_missing_timestamp(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()

        self._run_scan(projects_root)
        conn = sqlite3.connect(self.db_path)
        first_missing_since = conn.execute(
            "SELECT missing_since FROM repos WHERE path = '/srv/preserved-repo'"
        ).fetchone()[0]
        conn.close()

        summary = self._run_scan(projects_root)
        conn = sqlite3.connect(self.db_path)
        second_missing_since = conn.execute(
            "SELECT missing_since FROM repos WHERE path = '/srv/preserved-repo'"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(summary["stale_repos"], 0)
        self.assertEqual(second_missing_since, first_missing_since)

    def test_legacy_prune_entrypoint_stale_marks_instead_of_deleting(self) -> None:
        self._seed_repo()
        conn = sqlite3.connect(self.db_path)
        HUB.prune_missing_repos(conn, [])
        conn.commit()
        row = conn.execute(
            "SELECT inventory_status, focus_level, next_action FROM repos"
        ).fetchone()
        conn.close()

        self.assertEqual(row, ("stale", 3, "keep this operator state"))

    def test_partial_scan_preserves_rows_and_open_recommendations(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        self._seed_repo()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO recommendations (
                fingerprint, category, title, details, priority, status, updated_at
            ) VALUES ('preserve-me', 'inventory', 'Existing', 'Keep', 1, 'open', ?)
            """,
            ("2026-08-20T12:00:00+00:00",),
        )
        conn.commit()
        conn.close()

        partial = HUB.RepoDiscoveryResult(
            HUB.REPO_SCAN_PARTIAL,
            (),
            ("subdirectory unreadable",),
            projects_root,
        )
        with mock.patch.object(HUB, "discover_git_repos", return_value=partial):
            summary = self._run_scan(projects_root)

        conn = sqlite3.connect(self.db_path)
        repo_row = conn.execute(
            "SELECT inventory_status, focus_level, next_action FROM repos"
        ).fetchone()
        recommendation = conn.execute(
            "SELECT status FROM recommendations WHERE fingerprint = 'preserve-me'"
        ).fetchone()
        recommendation_titles = {
            row[0] for row in conn.execute("SELECT title FROM recommendations")
        }
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        scan_run = conn.execute(
            "SELECT status, stale_repo_count FROM repo_scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        self.assertEqual(summary["repo_scan_status"], HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(summary["stale_repos"], 0)
        self.assertEqual(repo_row, ("active", 3, "keep this operator state"))
        self.assertEqual(recommendation, ("open",))
        self.assertIn("Repository observation incomplete", recommendation_titles)
        self.assertNotIn("No git repositories discovered", recommendation_titles)
        self.assertEqual(meta["last_repo_scan_status"], HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(scan_run, (HUB.REPO_SCAN_PARTIAL, 0))

    def test_walk_error_makes_observation_partial(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()

        def walk_with_error(_root: Path, *, onerror=None):
            if onerror is not None:
                onerror(PermissionError("blocked subtree"))
            return iter(())

        with mock.patch.object(HUB.os, "walk", side_effect=walk_with_error):
            result = HUB.discover_git_repos(projects_root)

        self.assertEqual(result.status, HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(result.repositories, ())
        self.assertIn("blocked subtree", result.errors[0])

    def test_malformed_git_file_is_partial_not_authoritative_empty(self) -> None:
        projects_root = self.root / "projects"
        candidate = projects_root / "broken-worktree"
        candidate.mkdir(parents=True)
        (candidate / ".git").write_text("not a valid gitdir\n", encoding="utf-8")

        result = HUB.discover_git_repos(projects_root)

        self.assertEqual(result.status, HUB.REPO_SCAN_PARTIAL)
        self.assertEqual(result.repositories, ())
        self.assertTrue(any("invalid Git candidate" in error for error in result.errors))

    def test_git_file_worktree_is_discovered(self) -> None:
        projects_root = self.root / "projects"
        primary = projects_root / "primary"
        linked = projects_root / "linked"
        primary.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "config", "user.name", "Fleet Test"], check=True)
        subprocess.run(
            ["git", "-C", str(primary), "config", "user.email", "fleet-test@example.invalid"],
            check=True,
        )
        (primary / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(primary), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(primary), "commit", "-qm", "fixture"], check=True)
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-qb", "linked-test", str(linked)],
            check=True,
        )

        result = HUB.discover_git_repos(projects_root)

        self.assertEqual(result.status, HUB.REPO_SCAN_COMPLETE)
        self.assertEqual(set(result.repositories), {primary.resolve(), linked.resolve()})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPOS_FILE = REPO_ROOT / "repos.txt"


class ControllerRepoInventoryTests(unittest.TestCase):
    def _active_entries(self) -> list[str]:
        entries: list[str] = []
        for raw in REPOS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(line)
        return entries

    def test_active_entries_are_unique_github_https_git_urls(self) -> None:
        entries = self._active_entries()
        self.assertEqual(len(entries), len(set(entries)), "duplicate active repo entries")
        for entry in entries:
            self.assertTrue(
                entry.startswith("https://github.com/jarrettdustinqq/"),
                f"unexpected repo host/owner: {entry}",
            )
            self.assertTrue(entry.endswith(".git"), f"repo entry must end in .git: {entry}")
            self.assertNotIn("?", entry, f"repo entry must not contain query credentials: {entry}")
            self.assertNotIn("@", entry.split("github.com/", 1)[0], f"repo entry contains credentials: {entry}")

    def test_required_controller_repositories_are_present(self) -> None:
        entries = set(self._active_entries())
        required = {
            "https://github.com/jarrettdustinqq/continuity.git",
            "https://github.com/jarrettdustinqq/phantom-shell.git",
            "https://github.com/jarrettdustinqq/jarrettdustinqq-fleet.git",
            "https://github.com/jarrettdustinqq/system-operator-agent.git",
            "https://github.com/jarrettdustinqq/continuity-spine.git",
            "https://github.com/jarrettdustinqq/ledger-witness-offsite.git",
            "https://github.com/jarrettdustinqq/phantom-shell-dr-vault.git",
            "https://github.com/jarrettdustinqq/aistudio.git",
        }
        self.assertTrue(required <= entries, f"missing controller repos: {sorted(required - entries)}")

    def test_archives_obsolete_and_financially_gated_repos_are_not_default_active(self) -> None:
        entries = set(self._active_entries())
        intentionally_inactive = {
            "https://github.com/jarrettdustinqq/continuity-master-plan.git",
            "https://github.com/jarrettdustinqq/minimal-showcase.git",
            "https://github.com/jarrettdustinqq/aether.git",
            "https://github.com/jarrettdustinqq/aetheros.git",
        }
        self.assertFalse(
            entries & intentionally_inactive,
            f"intentionally excluded repos activated by default: {sorted(entries & intentionally_inactive)}",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "ops" / "control_hub_agent.py"

SPEC = importlib.util.spec_from_file_location(
    "fleet_control_hub_repo_discovery_tests",
    MODULE_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {MODULE_PATH}")

MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

find_git_repos = MODULE.find_git_repos


def mark_git_directory(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def mark_git_file(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text(
        "gitdir: /temporary/git/worktree\n",
        encoding="utf-8",
    )


class FindGitReposTests(unittest.TestCase):
    def test_missing_root_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            self.assertEqual(find_git_repos(missing), [])

    def test_discovers_root_and_nested_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested-repo"

            mark_git_directory(root)
            mark_git_directory(nested)

            # A marker under Git metadata must not be traversed.
            mark_git_directory(root / ".git" / "objects" / "fake-repo")

            self.assertEqual(
                find_git_repos(root),
                sorted([root, nested]),
            )

    def test_discovers_git_file_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree-repo"

            mark_git_file(worktree)

            self.assertEqual(find_git_repos(root), [worktree])

    def test_prunes_cache_dependency_and_virtualenv_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            visible = root / "visible-repo"
            mark_git_directory(visible)

            for excluded in (
                ".cache",
                "node_modules",
                ".venv",
                "venv",
            ):
                mark_git_directory(root / excluded / "hidden-repo")

            self.assertEqual(find_git_repos(root), [visible])


if __name__ == "__main__":
    unittest.main()

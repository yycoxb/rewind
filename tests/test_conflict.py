"""Conflict adjudication and the no-blanket-clean untracked-file rule."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rewind import gitops, restore
from rewind.killtest import conflict_scenario
try:
    from helpers import RepoFixture
except ImportError:  # pragma: no cover
    from tests.helpers import RepoFixture


class AdjudicationTest(unittest.TestCase):
    def test_real_kill_conflict_keeps_external_and_agent_versions(self) -> None:
        root = tempfile.mkdtemp(prefix="rewind-conflict-case-")
        try:
            outcome = conflict_scenario(root)
        finally:
            pass
        self.assertEqual(outcome["status"], restore.BLOCKED_ON_CONFLICT)
        self.assertTrue(outcome["external_kept"])
        self.assertTrue(outcome["unrelated_external_kept"])
        self.assertTrue(outcome["agent_bytes_preserved"])
        self.assertTrue(outcome["agent_ref"].startswith("refs/rewind/conflicts/"))
        self.assertTrue(outcome["diff_in_report"])

    def test_only_journal_recorded_agent_files_are_removed(self) -> None:
        fx = RepoFixture({"tracked.txt": b"tracked\n"})
        try:
            fx.write("external_untracked.txt", b"human notes\n")
            agent_tree = fx.agent_tree({"agent_new.txt": b"agent created\n"})
            first = restore.restore_workspace(
                fx.repo,
                fx.repo,
                base_tree=fx.base_tree,
                agent_tree=agent_tree,
                current_tree=fx.snapshot(),
                apply=True,
            )
            self.assertEqual(first.status, restore.RESUMED_MERGED)
            self.assertTrue((fx.repo / "agent_new.txt").exists())

            new_base = fx.snapshot()
            deletion = gitops.write_tree_from_changes(
                fx.repo, new_base, {"agent_new.txt": None}
            )
            second = restore.restore_workspace(
                fx.repo,
                fx.repo,
                base_tree=new_base,
                agent_tree=deletion,
                current_tree=fx.snapshot(),
                apply=True,
            )
            self.assertEqual(second.status, restore.RESUMED)
            self.assertFalse((fx.repo / "agent_new.txt").exists())
            self.assertTrue((fx.repo / "external_untracked.txt").exists())
            self.assertEqual(
                (fx.repo / "external_untracked.txt").read_bytes(), b"human notes\n"
            )
        finally:
            fx.cleanup()


if __name__ == "__main__":
    unittest.main()

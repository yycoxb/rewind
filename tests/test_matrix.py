"""One test per cell of the six-case restore decision matrix."""

from __future__ import annotations

import unittest

from rewind import restore

try:
    from helpers import RepoFixture, multiline_file, replace_line
except ImportError:  # pragma: no cover
    from tests.helpers import RepoFixture, multiline_file, replace_line



class SixCellMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture(
            {
                "both.txt": b"both unchanged\n",
                "external.txt": b"external original\n",
                "agent.txt": b"agent original\n",
                "merge.txt": multiline_file("merge"),
                "conflict.txt": b"line one\nline two\nline three\n",
            }
        )

    def tearDown(self) -> None:
        self.fx.cleanup()

    def _restore(self, agent_tree: str) -> restore.RestoreResult:
        current_tree = self.fx.snapshot()
        return restore.restore_workspace(
            self.fx.repo,
            self.fx.repo,
            base_tree=self.fx.base_tree,
            agent_tree=agent_tree,
            current_tree=current_tree,
            apply=True,
        )

    def test_cell_1_both_unchanged_resumed(self) -> None:
        result = self._restore(self.fx.base_tree)
        self.assertEqual(result.status, restore.RESUMED)
        self.assertEqual((self.fx.repo / "both.txt").read_bytes(), b"both unchanged\n")

    def test_cell_2_only_external_resumed_and_preserved(self) -> None:
        self.fx.write("external.txt", b"external changed\n")
        result = self._restore(self.fx.base_tree)
        self.assertEqual(result.status, restore.RESUMED)
        self.assertEqual(
            (self.fx.repo / "external.txt").read_bytes(), b"external changed\n"
        )
        self.assertIn("external.txt", result.external_only_paths)

    def test_cell_3_only_agent_resumed_and_replayed(self) -> None:
        agent_tree = self.fx.agent_tree({"agent.txt": b"agent changed\n"})
        result = self._restore(agent_tree)
        self.assertEqual(result.status, restore.RESUMED)
        self.assertEqual(
            (self.fx.repo / "agent.txt").read_bytes(), b"agent changed\n"
        )
        self.assertIn("agent.txt", result.agent_only_paths)

    def test_cell_4_both_changed_disjoint_paths_merged(self) -> None:
        agent_tree = self.fx.agent_tree({"agent.txt": b"agent changed\n"})
        self.fx.write("external.txt", b"external changed\n")
        result = self._restore(agent_tree)
        self.assertEqual(result.status, restore.RESUMED_MERGED)
        self.assertEqual(
            (self.fx.repo / "agent.txt").read_bytes(), b"agent changed\n"
        )
        self.assertEqual(
            (self.fx.repo / "external.txt").read_bytes(), b"external changed\n"
        )

    def test_cell_5_both_changed_same_file_mergeable(self) -> None:
        original = (self.fx.repo / "merge.txt").read_bytes()
        agent = replace_line(original, 1, "AGENT first line")
        external = replace_line(original, 11, "EXTERNAL eleventh line")
        agent_tree = self.fx.agent_tree({"merge.txt": agent})
        self.fx.write("merge.txt", external)
        result = self._restore(agent_tree)
        self.assertEqual(result.status, restore.RESUMED_MERGED)
        merged = (self.fx.repo / "merge.txt").read_text()
        self.assertIn("AGENT first line", merged)
        self.assertIn("EXTERNAL eleventh line", merged)
        self.assertIn("merge.txt", result.merged_paths)

    def test_cell_6_true_conflict_blocked_external_wins(self) -> None:
        original = (self.fx.repo / "conflict.txt").read_bytes()
        agent = replace_line(original, 2, "AGENT version")
        external = replace_line(original, 2, "HUMAN version")
        agent_tree = self.fx.agent_tree({"conflict.txt": agent})
        self.fx.write("conflict.txt", external)
        result = self._restore(agent_tree)
        self.assertEqual(result.status, restore.BLOCKED_ON_CONFLICT)
        self.assertEqual(
            (self.fx.repo / "conflict.txt").read_bytes(), external
        )
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].agent_content, agent)
        self.assertIn("AGENT version", result.conflicts[0].diff)
        self.assertIn("HUMAN version", result.conflicts[0].diff)


if __name__ == "__main__":
    unittest.main()

"""Real child processes, external SIGKILL x 20, resume, sha256 vs golden."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from rewind.killtest import clean_golden, kill_round, spawn_env_crash, resume_and_manifest


class KillNineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="rewind-kill9-"))
        cls.golden = clean_golden(cls.root, "kill9_golden")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_twenty_external_sigkill_rounds_match_golden_sha256(self) -> None:
        # Twenty distinct interruption steps across the 50-step plan.
        steps = [1 + 2 * i for i in range(20)]
        results = []
        for index, step in enumerate(steps):
            with self.subTest(round=index + 1, step=step):
                result = kill_round(
                    self.root,
                    round_index=index,
                    step=step,
                    golden=self.golden,
                    prefix="k9t",
                )
                results.append(result)
                self.assertTrue(
                    result["ok"],
                    msg=(
                        f"step {step} failed: {result.get('reason')}\n"
                        f"{result.get('log_tail', '')}"
                    ),
                )
        self.assertEqual(sum(1 for r in results if r["ok"]), 20)

    def test_rewind_crash_at_env_self_sigkill_matches_golden(self) -> None:
        returncode, log = spawn_env_crash(self.root, "k9t_selfkill", 7)
        self.assertEqual(returncode, -9, msg=log[-2000:])
        state, manifest = resume_and_manifest(self.root, "k9t_selfkill")
        self.assertIn(state["status"], {"RESUMED", "RESUMED_MERGED"})
        self.assertEqual(manifest, self.golden)


if __name__ == "__main__":
    unittest.main()

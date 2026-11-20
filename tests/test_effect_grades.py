"""Class-B compensation and explicit effect grading evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rewind.effects import execute_compensatable, recover_compensatable
from rewind.journal import INTENT, Journal


class EffectGradesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rewind-effects-")
        self.root = Path(self.tmp.name)
        self.journal = Journal(
            self.root / "journal.jsonl", task_id="b", run_id="b"
        )
        self.applied = self.root / "applied.txt"

    def tearDown(self) -> None:
        self.journal.close()
        self.tmp.cleanup()

    def test_class_b_in_doubt_is_compensated(self) -> None:
        key = "b-effect"

        def apply(payload):
            self.applied.write_text(payload)

        execute_compensatable(
            self.journal,
            idem_key=key,
            payload="side effect",
            apply=apply,
        )
        self.assertTrue(self.applied.exists())
        # Simulate a crash after apply but before result by making a second
        # intent/ side effect with no result.
        self.applied.write_text("crashed side effect")
        self.journal.append(
            INTENT,
            op="compensatable_effect",
            effect_class="B",
            idem_key=key + "-crashed",
            payload="crashed side effect",
        )
        before = self.journal.find_in_doubt()
        self.assertEqual(len(before), 1)

        def compensate(payload):
            self.applied.unlink(missing_ok=True)

        did = recover_compensatable(
            self.journal,
            idem_key=key + "-crashed",
            compensate=compensate,
        )
        self.assertTrue(did)
        self.assertFalse(self.applied.exists())
        compensated = [
            r
            for r in self.journal.records()
            if r.get("idem_key") == key + "-crashed" and r.get("type") == "result"
        ]
        self.assertEqual(compensated[0]["kind"], "compensated")
        self.assertEqual(self.journal.find_in_doubt(), [])


if __name__ == "__main__":
    unittest.main()

"""Durability and in-doubt discovery for the JSONL journal."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rewind.journal import INTENT, RESULT, Journal


class JournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rewind-journal-")
        self.path = Path(self.tmp.name) / "journal.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_intent_without_result_is_in_doubt(self) -> None:
        journal = Journal(self.path, task_id="t", run_id="r")
        journal.append(INTENT, idem_key="k1", op="external_effect", effect_class="C")
        journal.append(RESULT, idem_key="k1", status="applied", kind="ack")
        journal.append(INTENT, idem_key="k2", op="external_effect", effect_class="C")
        pending = journal.find_in_doubt()
        self.assertEqual([r["idem_key"] for r in pending], ["k2"])
        journal.append(RESULT, idem_key="k2", status="reconciled", kind="reconcile")
        self.assertEqual(journal.find_in_doubt(), [])
        journal.close()

    def test_every_append_flushes_and_fsyncs(self) -> None:
        with mock.patch("rewind.journal.os.fsync", wraps=os.fsync) as fsync:
            journal = Journal(self.path, task_id="t", run_id="r")
            journal.append(INTENT, idem_key="k1")
            journal.append(RESULT, idem_key="k1")
            journal.close()
        self.assertGreaterEqual(fsync.call_count, 3)

    def test_torn_final_line_is_tolerated(self) -> None:
        journal = Journal(self.path, task_id="t", run_id="r")
        journal.append(INTENT, idem_key="complete")
        journal.close()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 2, "type": "intent", "idem_key": "tor')
        reopened = Journal(self.path, task_id="t", run_id="r")
        self.assertEqual(len(reopened.records()), 1)
        self.assertEqual(reopened.records()[0]["idem_key"], "complete")
        reopened.close()


if __name__ == "__main__":
    unittest.main()

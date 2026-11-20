"""Outbox idempotency and read-only in-doubt reconciliation against localhost."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rewind.journal import INTENT, Journal
from rewind.outbox import (
    OutboxClient,
    _request,
    recover_external_effects,
    start_local_server,
)


class OutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rewind-outbox-")
        root = Path(self.tmp.name)
        self.handle = start_local_server(
            Path(__file__).resolve().parent.parent,
            store=root / "store.json",
            port_file=root / "port.txt",
        )

    def tearDown(self) -> None:
        self.handle.stop()
        self.tmp.cleanup()

    def _journal(self, name: str) -> Journal:
        path = Path(self.tmp.name) / f"{name}.jsonl"
        return Journal(path, task_id=name, run_id=name)

    def test_repeated_idempotency_key_applies_once(self) -> None:
        journal = self._journal("idem")
        client = OutboxClient(journal, self.handle.url)
        key = "idem-test-key"
        first = client.execute(key, {"value": 1})
        second = client.execute(key, {"value": 1})
        self.assertFalse(first["dedup"])
        self.assertTrue(second["dedup"])
        self.assertEqual(first["server_seq"], second["server_seq"])
        stats = _request("GET", f"{self.handle.url}/api/stats", None)
        self.assertGreaterEqual(stats["post_count"], 2)
        self.assertEqual(stats["stored_effects"], 1)
        journal.close()

    def test_in_doubt_queries_but_never_retries(self) -> None:
        key = "in-doubt-test-key"
        applied = self._journal("applied")
        OutboxClient(applied, self.handle.url).execute(key, {"value": "once"})
        applied.close()

        # Simulate the dangerous crash window: the effect exists on the
        # server, but this journal only has the intent.
        crashed = self._journal("crashed")
        crashed.append(
            INTENT,
            op="external_effect",
            effect_class="C",
            idem_key=key,
            payload={"value": "once"},
        )
        before = _request("GET", f"{self.handle.url}/api/stats", None)
        reconciliation = recover_external_effects(crashed, self.handle.url)
        after = _request("GET", f"{self.handle.url}/api/stats", None)

        self.assertEqual(reconciliation.status, "IN_DOUBT")
        self.assertTrue(reconciliation.reconciled)
        self.assertEqual(reconciliation.items[0]["found"], True)
        self.assertEqual(after["post_count"], before["post_count"])
        self.assertEqual(after["stored_effects"], 1)
        results = [
            r
            for r in crashed.records()
            if r.get("type") == "result" and r.get("idem_key") == key
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "reconcile")
        self.assertEqual(crashed.find_in_doubt(), [])
        crashed.close()


if __name__ == "__main__":
    unittest.main()

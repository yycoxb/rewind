"""Append-only JSONL journal with fsync durability and idempotency keys."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

INTENT = "intent"
RESULT = "result"
CHECKPOINT = "checkpoint"
EFFECT = "effect"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    """One journal file; every append is flushed and fsynced before returning."""

    def __init__(self, path: str | os.PathLike[str], *, task_id: str, run_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
        self.run_id = run_id
        self._records: list[dict] = self._read_lenient(self.path)
        self._seq = max((int(r.get("seq", 0)) for r in self._records), default=0)
        existed = self.path.exists()
        self._fh = self.path.open("a", encoding="utf-8")
        if not existed:
            _fsync_dir(self.path.parent)

    @staticmethod
    def _read_lenient(path: Path) -> list[dict]:
        if not path.exists():
            return []
        records: list[dict] = []
        raw = path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A SIGKILL can leave a torn final line.  Anything before the
                # final line would indicate real corruption, so surface it.
                if index == len(lines) - 1:
                    break
                raise
        return records

    def append(self, record_type: str, **fields) -> dict:
        record = {
            "seq": self._seq + 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "type": record_type,
            **fields,
        }
        self._seq += 1
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        self._fh.write(line)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._records.append(record)
        return record

    def records(self) -> list[dict]:
        return list(self._records)

    def find_in_doubt(self) -> list[dict]:
        """Intents that have no terminal result yet.

        An effect intent without a result means the side effect may or may not
        have happened.  A result with ``kind=reconcile`` also resolves an
        intent: it is evidence, not a retry.
        """

        results: dict[str, dict] = {}
        for record in self._records:
            if record.get("type") == RESULT and record.get("idem_key"):
                results[record["idem_key"]] = record
        pending: list[dict] = []
        for record in self._records:
            if record.get("type") != INTENT:
                continue
            key = record.get("idem_key")
            if key and key not in results:
                pending.append(record)
        return pending

    def unresolved_external_effects(self) -> list[dict]:
        return [
            r
            for r in self.find_in_doubt()
            if r.get("op") == "external_effect" and r.get("effect_class") == "C"
        ]

    def last_completed_checkpoint(self) -> dict | None:
        found = None
        for record in self._records:
            if record.get("type") == CHECKPOINT and record.get("completed", False):
                found = record
        return found

    def has_intent(self, idem_key: str) -> bool:
        return any(
            r.get("type") == INTENT and r.get("idem_key") == idem_key
            for r in self._records
        )

    def has_result(self, idem_key: str) -> bool:
        return any(
            r.get("type") == RESULT and r.get("idem_key") == idem_key
            for r in self._records
        )

    def close(self) -> None:
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        finally:
            self._fh.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

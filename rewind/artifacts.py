"""Run artifacts, trace output, and the human-readable markdown report."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from . import task
from .journal import CHECKPOINT, INTENT, RESULT, Journal


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def new_run_id(task_id: str) -> str:
    return f"{utc_stamp()}-{task_id}-{secrets.token_hex(3)}"


def atomic_write(path: str | os.PathLike[str], data: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_state(path: str | os.PathLike[str], state: dict) -> None:
    state = dict(state)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def read_state(path: str | os.PathLike[str]) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Artifacts:
    def __init__(self, runs_root: Path, run_id: str, *, echo: bool = True):
        self.run_id = run_id
        self.dir = runs_root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.dir / "trace.txt"
        self.journal_path = self.dir / "journal.jsonl"
        self.report_path = self.dir / "report.md"
        self.diff_path = self.dir / "final.diff"
        self.echo = echo
        self._trace = self.trace_path.open("a", encoding="utf-8")

    def trace(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {self.run_id} {message}\n"
        self._trace.write(line)
        self._trace.flush()
        os.fsync(self._trace.fileno())
        if self.echo:
            print(line.rstrip(), flush=True)

    def session(self, title: str) -> None:
        self.trace(f"===== {title} =====")

    def close(self) -> None:
        try:
            self._trace.flush()
            os.fsync(self._trace.fileno())
        finally:
            self._trace.close()


def generate_report(
    *,
    state: dict,
    journal_path: str | os.PathLike[str],
    artifacts: Artifacts,
    total_steps: int = task.TOTAL_STEPS,
) -> str:
    records = Journal._read_lenient(Path(journal_path))
    checkpoints = [
        r for r in records if r.get("type") == CHECKPOINT and r.get("completed", False)
    ]
    pending = _pending_intents(records)
    conflicts = []
    reconciliations = []
    for record in records:
        if record.get("type") == RESULT:
            for conflict in record.get("conflicts", []) or []:
                conflicts.append(conflict)
            if record.get("kind") == "reconcile":
                reconciliations.append(record)

    lines: list[str] = []
    lines.append(f"# Rewind report -- task `{state.get('task_id')}`")
    lines.append("")
    lines.append(f"- **Status:** `{state.get('status')}`")
    lines.append(f"- **Run id:** `{state.get('run_id')}`")
    lines.append(f"- **Completed steps:** {len(checkpoints)} / {total_steps}")
    lines.append(f"- **Journal records:** {len(records)} (`journal.jsonl`)")
    lines.append(f"- **In-doubt intents at report time:** {len(pending)}")
    lines.append(f"- **Seed tree:** `{state.get('seed_tree')}`")
    lines.append(f"- **Last checkpoint tree:** `{state.get('current_tree')}`")
    lines.append("- **Agent policy:** deterministic 50-step/30-file plan (not an LLM)")
    lines.append("")

    lines.append("## Recovery decision")
    lines.append("")
    lines.append(
        f"Final harness decision: **{state.get('status')}**. "
        f"Last completed checkpoint: step {state.get('steps_done', 0)}."
    )
    if any(r.get("in_doubt") for r in records if r.get("type") == RESULT):
        lines.append("")
        lines.append(
            "At least one file-step intent was found without a result after a "
            "crash.  It was classified as in-doubt, then resolved by the "
            "content-hash matrix as class-A idempotent work (no blind replay)."
        )
    lines.append("")

    if conflicts:
        lines.append("## Conflict adjudication")
        lines.append("")
        lines.append(
            "External/human edits win true conflicts.  Each agent version was "
            "retained in a conflict ref; the working tree kept the external bytes."
        )
        lines.append("")
        for c in conflicts:
            lines.append(f"### `{c.get('path')}`")
            lines.append("")
            lines.append(f"- Reason: {c.get('reason')}")
            lines.append(f"- Agent version ref: `{c.get('ref')}`")
            lines.append(f"- Agent snapshot commit: `{c.get('commit')}`")
            lines.append(
                f"- Blobs (base / agent / external): `{c.get('base_oid')}` / "
                f"`{c.get('agent_oid')}` / `{c.get('current_oid')}`"
            )
            lines.append("")
            diff = c.get("diff") or ""
            lines.append("```diff")
            lines.append(diff.rstrip("\n"))
            lines.append("```")
            lines.append("")
    else:
        lines.append("## Conflict adjudication")
        lines.append("")
        lines.append("No true conflicts in this run.")
        lines.append("")

    lines.append("## Irreversible effects")
    lines.append("")
    if reconciliations:
        for rec in reconciliations:
            lines.append(
                f"- `{rec.get('idem_key')}`: IN_DOUBT, reconciled by read-only "
                f"query (found={rec.get('found')}, server_seq={rec.get('server_seq')}); "
                "no retry was issued."
            )
    else:
        lines.append(
            "No external class-C effect was part of this task run.  The "
            "outbox/reconciliation path is exercised by `python3 -m rewind "
            "demo` and by `tests/test_outbox.py`."
        )
    lines.append("")

    lines.append("## Artifacts")
    lines.append("")
    for name in ("journal.jsonl", "trace.txt", "report.md", "final.diff"):
        lines.append(f"- `runs/{artifacts.run_id}/{name}`")
    lines.append("")
    lines.append("## Journal tail")
    lines.append("")
    lines.append("```text")
    for record in records[-8:]:
        lines.append(
            json.dumps(
                {
                    k: record.get(k)
                    for k in ("seq", "type", "step", "status", "idem_key", "kind")
                    if k in record
                },
                sort_keys=True,
            )
        )
    lines.append("```")
    lines.append("")

    text = "\n".join(lines)
    atomic_write(artifacts.report_path, text)
    return text


def _pending_intents(records: list[dict]) -> list[dict]:
    results = {
        r.get("idem_key")
        for r in records
        if r.get("type") == RESULT and r.get("idem_key")
    }
    return [
        r
        for r in records
        if r.get("type") == INTENT and r.get("idem_key") not in results
    ]

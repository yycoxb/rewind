"""Auxiliary class-C effect task used by the demo and tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import artifacts as artifacts_mod
from . import restore
from .artifacts import Artifacts, read_state, write_state
from .journal import Journal
from .outbox import OutboxClient, recover_external_effects


class EffectTaskError(RuntimeError):
    pass


def _dir(root: Path, task_id: str) -> Path:
    return root / ".rewind" / "effects" / task_id


def _write_effect_report(
    artifacts: Artifacts,
    state: dict,
    records: list[dict],
) -> str:
    lines = [
        f"# Rewind class-C effect report -- `{state['task_id']}`",
        "",
        f"- **Status:** `{state['status']}`",
        f"- **Run id:** `{state['run_id']}`",
        f"- **Idempotency key:** `{state['idem_key']}`",
        f"- **Journal records:** {len(records)}",
        "",
        "## Result",
        "",
    ]
    if state["status"] == "IN_DOUBT":
        lines += [
            "The intent was durable, the HTTP request was sent, and the client "
            "was killed before the acknowledgement was journaled.  Recovery "
            "entered **IN_DOUBT** and performed a read-only lookup by "
            "idempotency key.  It did **not** retry the POST.",
        ]
    elif state["status"] == "APPLIED":
        lines += ["The effect was acknowledged and durably recorded."]
    else:
        lines += [f"Terminal state: {state['status']}."]
    lines += [
        "",
        "## Evidence",
        "",
        "```json",
    ]
    for record in records:
        lines.append(
            f"{record.get('seq')}: {record.get('type')} "
            f"{record.get('op', '')} {record.get('status', '')} "
            f"{record.get('kind', '')}"
        )
    lines += ["```", ""]
    text = "\n".join(lines)
    artifacts_mod.atomic_write(artifacts.report_path, text)
    return text


def effect_run(
    root: str | os.PathLike[str],
    task_id: str,
    server_url: str,
    *,
    crash_after_ack: bool = False,
    echo: bool = True,
) -> dict:
    root = Path(root).resolve()
    task_dir = _dir(root, task_id)
    if task_dir.exists():
        raise EffectTaskError(f"effect task {task_id!r} already exists")
    task_dir.mkdir(parents=True)
    run_id = artifacts_mod.new_run_id(task_id)
    artifacts = Artifacts(root / "runs", run_id, echo=echo)
    artifacts.session("class-C effect run started")
    journal = Journal(
        artifacts.journal_path, task_id=task_id, run_id=run_id
    )
    idem_key = f"{task_id}:publish:1"
    state = {
        "kind": "effect",
        "task_id": task_id,
        "run_id": run_id,
        "root": os.fspath(root),
        "server_url": server_url,
        "idem_key": idem_key,
        "status": "RUNNING",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifact_dir": os.fspath(artifacts.dir),
        "journal": os.fspath(artifacts.journal_path),
    }
    write_state(task_dir / "state.json", state)
    journal.append(
        "run_start",
        mode="effect",
        effect_class="C",
        idem_key=idem_key,
        server_url=server_url,
    )
    artifacts.trace(
        f"effect: intent for {idem_key} will be fsynced before POST to {server_url}"
    )
    client = OutboxClient(journal, server_url)
    try:
        ack = client.execute(
            idem_key,
            {"task": task_id, "publication": "irreversible-poC"},
            crash_after_ack=crash_after_ack,
        )
    except BaseException:
        write_state(task_dir / "state.json", state)
        raise
    state["status"] = "APPLIED"
    state["ack"] = ack
    write_state(task_dir / "state.json", state)
    journal.append("run_end", status="APPLIED", idem_key=idem_key)
    _write_effect_report(artifacts, state, journal.records())
    artifacts.trace(
        f"effect: server acknowledged {idem_key} at server_seq={ack.get('server_seq')} "
        f"dedup={ack.get('dedup')}"
    )
    journal.close()
    artifacts.close()
    return state


def effect_resume(
    root: str | os.PathLike[str],
    task_id: str,
    server_url: str | None = None,
    *,
    echo: bool = True,
) -> dict:
    root = Path(root).resolve()
    task_dir = _dir(root, task_id)
    if not task_dir.exists():
        raise EffectTaskError(f"effect task {task_id!r} does not exist")
    state = read_state(task_dir / "state.json")
    server_url = server_url or state["server_url"]
    artifacts = Artifacts(Path(state["root"]) / "runs", state["run_id"], echo=echo)
    artifacts.session("class-C effect resume")
    journal = Journal(
        artifacts.journal_path, task_id=task_id, run_id=state["run_id"]
    )
    pending = journal.unresolved_external_effects()
    if not pending:
        artifacts.trace("effect resume: no unresolved class-C intents")
        if state.get("status") in {"APPLIED", "IN_DOUBT"}:
            _write_effect_report(artifacts, state, journal.records())
            journal.close()
            artifacts.close()
            return state
        state["status"] = "NO_EFFECT_INTENT"
        write_state(task_dir / "state.json", state)
        _write_effect_report(artifacts, state, journal.records())
        journal.close()
        artifacts.close()
        return state

    artifacts.trace(
        f"effect resume: {len(pending)} in-doubt intent(s); entering IN_DOUBT; "
        "no POST will be sent"
    )
    reconciliation = recover_external_effects(journal, server_url)
    state["status"] = restore.IN_DOUBT
    state["reconciled"] = reconciliation.reconciled
    state["reconciliation"] = reconciliation.items
    state["unresolved"] = reconciliation.unresolved
    write_state(task_dir / "state.json", state)
    for item in reconciliation.items:
        artifacts.trace(
            f"effect resume: lookup {item['idem_key']} found={item['found']} "
            f"server_seq={item['server_seq']} (read-only; no retry)"
        )
    if reconciliation.unresolved:
        artifacts.trace(
            f"effect resume: still unresolved: {reconciliation.unresolved}"
        )
    _write_effect_report(artifacts, state, journal.records())
    journal.close()
    artifacts.close()
    return state


def get_effect_state(root: str | os.PathLike[str], task_id: str) -> dict:
    root = Path(root).resolve()
    path = _dir(root, task_id) / "state.json"
    if not path.exists():
        raise EffectTaskError(f"effect task {task_id!r} does not exist")
    return read_state(path)

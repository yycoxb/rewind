"""Real process-kill helpers shared by the test suite and the demo."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import gitops, task
from .runner import resume_run, start_run
from .workspace import load_workspace


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    root = str(repo_root())
    env["PYTHONPATH"] = root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("REWIND_CRASH_AT", None)
    env.pop("REWIND_CRASH_AFTER_EFFECT", None)
    return env


def spawn_paused_run(
    root: str | os.PathLike[str],
    task_id: str,
    step: int,
    *,
    timeout: float = 120.0,
) -> tuple[int, str]:
    """Run a real child until step writes are on disk, then SIGKILL it."""

    root = Path(root).resolve()
    log_dir = root / ".rewind" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}-kill.log"
    env = _python_env()
    env["REWIND_TEST_PAUSE_AT"] = str(step)
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "rewind",
        "--root",
        os.fspath(root),
        "run",
        "--task",
        task_id,
    ]
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        cwd=os.fspath(repo_root()),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    marker = f"REWIND_KILL_READY step={step}"
    deadline = time.time() + timeout
    observed = ""
    try:
        while time.time() < deadline:
            observed = log_path.read_text(encoding="utf-8", errors="replace")
            if marker in observed:
                # The child is sleeping after its file writes and intent fsync.
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
                return process.returncode, observed
            if process.poll() is not None:
                return process.returncode if process.returncode is not None else -1, observed
            time.sleep(0.02)
        process.kill()
        process.wait(timeout=10)
        raise TimeoutError(
            f"child did not reach {marker}; tail:\n{observed[-2000:]}"
        )
    finally:
        log.close()


def spawn_env_crash(
    root: str | os.PathLike[str],
    task_id: str,
    step: int,
    *,
    timeout: float = 120.0,
) -> tuple[int, str]:
    """Exercise the in-process ``REWIND_CRASH_AT`` SIGKILL support."""

    root = Path(root).resolve()
    log_dir = root / ".rewind" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}-crash-at.log"
    env = _python_env()
    env["REWIND_CRASH_AT"] = str(step)
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "rewind",
        "--root",
        os.fspath(root),
        "run",
        "--task",
        task_id,
    ]
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.run(
            cmd,
            cwd=os.fspath(repo_root()),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    return process.returncode, log_path.read_text(encoding="utf-8", errors="replace")


def clean_golden(root: str | os.PathLike[str], task_id: str) -> dict[str, str]:
    start_run(root, task_id, echo=False)
    workspace = load_workspace(root, task_id)
    return gitops.worktree_file_sha256(workspace.worktree)


def resume_and_manifest(root: str | os.PathLike[str], task_id: str) -> tuple[dict, dict[str, str]]:
    state = resume_run(root, task_id, echo=False)
    workspace = load_workspace(root, task_id)
    return state, gitops.worktree_file_sha256(workspace.worktree)


def kill_round(
    root: str | os.PathLike[str],
    *,
    round_index: int,
    step: int,
    golden: dict[str, str],
    prefix: str = "k9",
) -> dict:
    task_id = f"{prefix}_{round_index:02d}_s{step:02d}_{secrets.token_hex(2)}"
    returncode, log = spawn_paused_run(root, task_id, step)
    if returncode != -signal.SIGKILL:
        return {
            "ok": False,
            "step": step,
            "task": task_id,
            "reason": f"child exit was {returncode}, expected -{signal.SIGKILL}",
            "log_tail": log[-1500:],
        }
    state, manifest = resume_and_manifest(root, task_id)
    if state.get("status") not in {"RESUMED", "RESUMED_MERGED"}:
        return {
            "ok": False,
            "step": step,
            "task": task_id,
            "reason": f"resume status {state.get('status')}",
            "log_tail": log[-1500:],
        }
    if manifest != golden:
        missing = sorted(set(golden) - set(manifest))
        extra = sorted(set(manifest) - set(golden))
        differing = sorted(
            p for p in set(golden) & set(manifest) if golden[p] != manifest[p]
        )
        return {
            "ok": False,
            "step": step,
            "task": task_id,
            "reason": (
                f"manifest mismatch missing={missing} extra={extra} "
                f"differing={differing}"
            ),
            "log_tail": log[-1500:],
        }
    return {
        "ok": True,
        "step": step,
        "task": task_id,
        "status": state.get("status"),
        "exit": returncode,
        "files": len(manifest),
    }


def conflict_scenario(root: str | os.PathLike[str]) -> dict:
    """Crash, let an external editor overwrite the same file, then resume."""

    root = Path(root).resolve()
    tag = secrets.token_hex(3)
    task_id = f"conflict_{tag}"
    step = 5
    returncode, log = spawn_paused_run(root, task_id, step)
    if returncode != -signal.SIGKILL:
        raise AssertionError(f"conflict child exit {returncode}; log:\n{log[-2000:]}")
    workspace = load_workspace(root, task_id)
    target = task.relpath(4)  # step 5 edits mod_04
    untouched = task.relpath(10)
    external_target = (
        '"""Module 04 -- HUMAN OVERRIDE."""\n'
        'MODULE = "mod_04"\n'
        "REVISION = 999\n"
        'TOKENS = ["human-change"]\n'
        "\n"
        "\n"
        "def describe():\n"
        '    return "human owns this file"\n'
    ).encode("utf-8")
    external_other = (
        '"""Module 10 -- edited externally while agent was down."""\n'
        'MODULE = "mod_10"\n'
        "REVISION = 77\n"
        "TOKENS = []\n"
        "\n"
        "\n"
        "def describe():\n"
        '    return "external note"\n'
    ).encode("utf-8")
    gitops.write_worktree_file(workspace.worktree, target, external_target)
    gitops.write_worktree_file(workspace.worktree, untouched, external_other)

    state = resume_run(root, task_id, echo=False)
    records = [
        json.loads(line)
        for line in Path(state["journal"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    conflicts: list[dict] = []
    for record in records:
        conflicts.extend(record.get("conflicts", []) or [])
    if state.get("status") != "BLOCKED_ON_CONFLICT":
        raise AssertionError(f"expected BLOCKED_ON_CONFLICT, got {state.get('status')}")
    if not conflicts:
        raise AssertionError("no conflict record in journal")
    conflict = conflicts[0]
    kept = (workspace.worktree / target).read_bytes()
    if kept != external_target:
        raise AssertionError("external bytes were not preserved in the worktree")
    other = (workspace.worktree / untouched).read_bytes()
    if other != external_other:
        raise AssertionError("unrelated external edit was not preserved")
    if not conflict.get("ref"):
        raise AssertionError("agent version has no conflict ref")
    agent_bytes = gitops.run_git(
        ["cat-file", "blob", f"{conflict['commit']}:{target}"],
        cwd=workspace.repo,
    ).stdout
    expected_agent = task.revision_content(4, 1)
    if agent_bytes != expected_agent:
        raise AssertionError("conflict ref does not contain the agent version")
    report = Path(state["artifact_dir"]) / "report.md"
    report_text = report.read_text(encoding="utf-8")
    if "Conflict adjudication" not in report_text or "```diff" not in report_text:
        raise AssertionError("report lacks the conflict diff")
    return {
        "task": task_id,
        "step": step,
        "status": state["status"],
        "external_kept": True,
        "unrelated_external_kept": True,
        "agent_ref": conflict["ref"],
        "agent_bytes_preserved": True,
        "diff_in_report": True,
        "report": os.fspath(report),
    }


def effect_scenario(root: str | os.PathLike[str]) -> dict:
    """Crash after POST+ack, before journal result; reconcile via idempotency key."""

    from .effect_task import effect_resume
    from .outbox import _request, start_local_server

    root = Path(root).resolve()
    tag = secrets.token_hex(3)
    task_id = f"effect_{tag}"
    server_dir = root / ".rewind" / "effect-server" / tag
    handle = start_local_server(
        repo_root(),
        store=server_dir / "store.json",
        port_file=server_dir / "port.txt",
    )
    try:
        env = _python_env()
        env["REWIND_CRASH_AFTER_EFFECT"] = "1"
        log_path = root / ".rewind" / "logs" / f"{task_id}-effect.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "rewind",
            "--root",
            os.fspath(root),
            "effect-run",
            "--task",
            task_id,
            "--server",
            handle.url,
        ]
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.run(
                cmd,
                cwd=os.fspath(repo_root()),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
        if process.returncode != -signal.SIGKILL:
            raise AssertionError(
                f"effect child exit {process.returncode}, expected -9; log:\n"
                f"{log_path.read_text(encoding='utf-8')[-2000:]}"
            )
        state = effect_resume(root, task_id, handle.url, echo=False)
        if state.get("status") != "IN_DOUBT":
            raise AssertionError(f"effect resume status {state.get('status')}")
        if not state.get("reconciled"):
            raise AssertionError("effect was not reconciled")
        stats = _request("GET", f"{handle.url}/api/stats", None)
        if stats.get("post_count") != 1:
            raise AssertionError(
                f"expected exactly one POST (no retry), got {stats}"
            )
        if stats.get("stored_effects") != 1:
            raise AssertionError(f"expected one stored effect, got {stats}")
        return {
            "task": task_id,
            "status": state["status"],
            "reconciled": True,
            "post_count": stats["post_count"],
            "stored_effects": stats["stored_effects"],
            "server_seq": (state.get("reconciliation") or [{}])[0].get("server_seq"),
            "report": os.fspath(Path(state["artifact_dir"]) / "report.md"),
        }
    finally:
        handle.stop()

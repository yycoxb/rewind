"""Command line entry points for the rewind PoC."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import restore, task
from .artifacts import read_state
from .effect_task import EffectTaskError, effect_resume, effect_run
from .runner import RunnerError, resume_run, start_run
from .workspace import WorkspaceError, load_workspace


def _exit_for_status(status: str) -> int:
    if status == restore.IN_DOUBT:
        return 3
    if status == restore.BLOCKED_ON_CONFLICT:
        return 4
    return 0


def _print_state(state: dict) -> None:
    print(
        f"task={state.get('task_id')} status={state.get('status')} "
        f"steps={state.get('steps_done')}/{state.get('total_steps', task.TOTAL_STEPS)} "
        f"run_id={state.get('run_id')}",
        flush=True,
    )
    if state.get("status") == restore.BLOCKED_ON_CONFLICT:
        print(f"report: {Path(state['artifact_dir']) / 'report.md'}", flush=True)
    elif state.get("artifact_dir"):
        print(f"artifacts: {state['artifact_dir']}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m rewind",
        description="Crash-safe checkpoint/resume harness PoC",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("REWIND_ROOT", os.getcwd()),
        help="workspace root containing .rewind/ and runs/ (default: cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="create a task and run it")
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--crash-at", type=int, default=None)

    p_resume = sub.add_parser("resume", help="resume from the journal")
    p_resume.add_argument("--task", required=True)

    p_report = sub.add_parser("report", help="print the latest report")
    p_report.add_argument("--task", required=True)

    p_effect_run = sub.add_parser(
        "effect-run", help="run a class-C effect (demo/test helper)"
    )
    p_effect_run.add_argument("--task", required=True)
    p_effect_run.add_argument("--server", required=True)

    p_effect_resume = sub.add_parser(
        "effect-resume", help="reconcile a class-C effect (demo/test helper)"
    )
    p_effect_resume.add_argument("--task", required=True)
    p_effect_resume.add_argument("--server", default=None)

    p_demo = sub.add_parser("demo", help="run the full three-part demo")
    p_demo.add_argument("--rounds", type=int, default=20)
    return parser


def _report_task(root: Path, task_id: str) -> int:
    try:
        workspace = load_workspace(root, task_id)
        state = read_state(workspace.state_path)
        report = Path(state["artifact_dir"]) / "report.md"
    except WorkspaceError:
        # A fresh checkout has the retained artifacts but not the ignored
        # .rewind state.  Fall back to the matching run directory.
        candidates = sorted(
            root.joinpath("runs").glob(f"*-{task_id}-*/report.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print(f"task {task_id!r} not found under {root}", file=sys.stderr)
            return 2
        report = candidates[0]
    if not report.exists():
        print(f"report not found: {report}", file=sys.stderr)
        return 2
    sys.stdout.write(report.read_text(encoding="utf-8"))
    print(f"\nreport_file={report}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "run":
            crash_at = args.crash_at
            if crash_at is None and os.environ.get("REWIND_CRASH_AT"):
                crash_at = int(os.environ["REWIND_CRASH_AT"])
            pause_at = (
                int(os.environ["REWIND_TEST_PAUSE_AT"])
                if os.environ.get("REWIND_TEST_PAUSE_AT")
                else None
            )
            state = start_run(root, args.task, crash_at=crash_at, pause_at=pause_at)
            _print_state(state)
            return _exit_for_status(state.get("status", ""))
        if args.command == "resume":
            state = resume_run(root, args.task)
            _print_state(state)
            return _exit_for_status(state.get("status", ""))
        if args.command == "report":
            return _report_task(root, args.task)
        if args.command == "effect-run":
            crash_after = bool(os.environ.get("REWIND_CRASH_AFTER_EFFECT"))
            state = effect_run(
                root, args.task, args.server, crash_after_ack=crash_after
            )
            print(
                f"effect task={args.task} status={state['status']} "
                f"server_seq={(state.get('ack') or {}).get('server_seq')}",
                flush=True,
            )
            return 0
        if args.command == "effect-resume":
            state = effect_resume(root, args.task, args.server)
            print(
                f"effect task={args.task} status={state['status']} "
                f"reconciled={state.get('reconciled')}",
                flush=True,
            )
            return _exit_for_status(state.get("status", ""))
        if args.command == "demo":
            from .demo import run_demo

            return run_demo(args.rounds)
        raise AssertionError(args.command)
    except (WorkspaceError, RunnerError, EffectTaskError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

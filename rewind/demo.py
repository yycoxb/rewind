"""One-command demo: kill -9 x N, conflict adjudication, effect in-doubt."""

from __future__ import annotations

import secrets
import sys
import time

from .killtest import clean_golden, conflict_scenario, effect_scenario, kill_round, repo_root


def run_demo(rounds: int = 20) -> int:
    if rounds < 1:
        raise SystemExit("rounds must be positive")
    root = repo_root()
    tag = secrets.token_hex(3)
    started = time.monotonic()
    print("=" * 78)
    print("REWIND crash-safety PoC demo")
    print("=" * 78, flush=True)

    print("\n[setup] one clean golden run of the deterministic 30-file task", flush=True)
    golden_task = f"golden_{tag}"
    golden = clean_golden(root, golden_task)
    print(f"  golden task={golden_task} files={len(golden)}", flush=True)

    steps = [1 + 2 * i for i in range(rounds)]
    print(f"\n[a] external SIGKILL x {rounds}, then resume and compare per-file sha256", flush=True)
    results = []
    for index, step in enumerate(steps):
        result = kill_round(root, round_index=index, step=step, golden=golden, prefix="demo")
        results.append(result)
        if result["ok"]:
            print(
                f"  round {index + 1:02d}: step {step:02d} exit={result['exit']} "
                f"resume={result['status']} files={result['files']} sha256=match",
                flush=True,
            )
        else:
            print(
                f"  round {index + 1:02d}: step {step:02d} FAILED: {result['reason']}",
                flush=True,
            )
            if result.get("log_tail"):
                print(result["log_tail"], flush=True)
    passed = sum(1 for r in results if r["ok"])
    kill_ok = passed == rounds

    print("\n[b] conflict: external edit to the same file, agent version retained", flush=True)
    try:
        conflict = conflict_scenario(root)
        conflict_ok = True
        print(
            f"  status={conflict['status']} external_bytes_kept="
            f"{conflict['external_kept']} unrelated_external_kept="
            f"{conflict['unrelated_external_kept']}",
            flush=True,
        )
        print(
            f"  agent_bytes_preserved={conflict['agent_bytes_preserved']} "
            f"ref={conflict['agent_ref']}",
            flush=True,
        )
        print(f"  report={conflict['report']}", flush=True)
    except Exception as exc:  # noqa: BLE001
        conflict_ok = False
        conflict = {"error": str(exc)}
        print(f"  FAILED: {exc}", flush=True)

    print("\n[c] irreversible effect: crash after POST+ack, reconcile by key", flush=True)
    try:
        effect = effect_scenario(root)
        effect_ok = True
        print(
            f"  status={effect['status']} reconciled={effect['reconciled']} "
            f"post_count={effect['post_count']} stored_effects={effect['stored_effects']} "
            f"server_seq={effect['server_seq']}",
            flush=True,
        )
        print(f"  report={effect['report']}", flush=True)
    except Exception as exc:  # noqa: BLE001
        effect_ok = False
        effect = {"error": str(exc)}
        print(f"  FAILED: {exc}", flush=True)

    elapsed = time.monotonic() - started
    print("\n" + "=" * 78)
    print("SUMMARY")
    print(f"  (a) kill -9: {passed}/{rounds} passed "
          f"({'all final sha256 match golden' if kill_ok else 'FAILURES ABOVE'})", flush=True)
    if conflict_ok:
        print(
            f"  (b) conflict: {conflict['status']}; external edit kept; "
            f"agent version at {conflict['agent_ref']}", flush=True
        )
    else:
        print(f"  (b) conflict: FAILED: {conflict.get('error')}", flush=True)
    if effect_ok:
        print(
            f"  (c) effect: {effect['status']}; reconciled={effect['reconciled']}; "
            f"POST count {effect['post_count']} (no retry)", flush=True
        )
    else:
        print(f"  (c) effect: FAILED: {effect.get('error')}", flush=True)
    print(f"  elapsed: {elapsed:.2f}s", flush=True)
    print("=" * 78, flush=True)
    return 0 if (kill_ok and conflict_ok and effect_ok) else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rounds = 20
    if "--rounds" in argv:
        index = argv.index("--rounds")
        rounds = int(argv[index + 1])
    return run_demo(rounds)


if __name__ == "__main__":
    raise SystemExit(main())

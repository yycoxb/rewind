"""The deliberately deterministic 'agent'.

The harness is the subject of this PoC, so the agent is a fixed plan: it
rewrites 30 files in 50 steps.  No model, randomness, clock, or network is
involved in producing file content.  That makes a crash/resume run directly
comparable with a golden run.
"""

from __future__ import annotations

TOTAL_STEPS = 50
FILE_COUNT = 30


def relpath(index: int) -> str:
    return f"src/mod_{index:02d}.py"


def seed_content(index: int) -> bytes:
    return (
        f'"""Module {index:02d} (seed)."""\n'
        f'MODULE = "mod_{index:02d}"\n'
        "REVISION = 0\n"
        "TOKENS = []\n"
        "\n"
        "\n"
        "def describe():\n"
        '    return f"{MODULE} r{REVISION}"\n'
    ).encode("utf-8")


def revision_content(index: int, revision: int) -> bytes:
    tokens = ", ".join(f'"r{revision}-{index:02d}-{n:02d}"' for n in range(3))
    return (
        f'"""Module {index:02d} -- rewritten by the scripted agent, r{revision}."""\n'
        f'MODULE = "mod_{index:02d}"\n'
        f"REVISION = {revision}\n"
        f"TOKENS = [{tokens}]\n"
        "\n"
        "\n"
        "def describe():\n"
        '    return f"{MODULE} r{REVISION}"\n'
    ).encode("utf-8")


def seed_files() -> dict[str, bytes]:
    return {relpath(i): seed_content(i) for i in range(FILE_COUNT)}


def step_target(step: int) -> tuple[str, bytes]:
    """Return the path and exact content produced by a 1-based step."""

    if not 1 <= step <= TOTAL_STEPS:
        raise ValueError(f"step out of range: {step}")
    if step <= FILE_COUNT:
        index = step - 1
        revision = 1
    else:
        index = step - FILE_COUNT - 1
        revision = 2
    return relpath(index), revision_content(index, revision)


def step_changes(step: int) -> dict[str, bytes | None]:
    path, content = step_target(step)
    return {path: content}

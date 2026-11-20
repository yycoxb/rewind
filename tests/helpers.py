from __future__ import annotations

import os
import tempfile
from pathlib import Path

from rewind import gitops
from rewind.workspace import EMPTY_TREE


class RepoFixture:
    """A throw-away repo used as both object store and working tree."""

    def __init__(self, files: dict[str, bytes]):
        self.tmp = tempfile.TemporaryDirectory(prefix="rewind-matrix-")
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        gitops.init_repo(self.repo)
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.base_tree = gitops.snapshot_worktree(self.repo, EMPTY_TREE)

    def write(self, path: str, content: bytes) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def delete(self, path: str) -> None:
        (self.repo / path).unlink()

    def snapshot(self) -> str:
        return gitops.snapshot_worktree(self.repo, self.base_tree)

    def agent_tree(self, changes: dict[str, bytes | None]) -> str:
        return gitops.write_tree_from_changes(self.repo, self.base_tree, changes)

    def cleanup(self) -> None:
        self.tmp.cleanup()


def multiline_file(prefix: str, count: int = 12) -> bytes:
    return "".join(f"{prefix} line {i:02d}\n" for i in range(1, count + 1)).encode()


def replace_line(content: bytes, number: int, replacement: str) -> bytes:
    lines = content.decode().splitlines(keepends=True)
    lines[number - 1] = replacement + "\n"
    return "".join(lines).encode()

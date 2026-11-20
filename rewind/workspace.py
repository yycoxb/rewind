"""Per-task isolated repository and linked worktree creation."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import gitops, task

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    root: Path
    task_id: str
    task_dir: Path
    repo: Path
    worktree: Path
    seed_tree: str
    seed_commit: str

    @property
    def state_path(self) -> Path:
        return self.task_dir / "state.json"

    def require_worktree(self) -> None:
        if not self.worktree.is_dir():
            raise WorkspaceError(f"missing worktree: {self.worktree}")


def _validate_task_id(task_id: str) -> None:
    if not task_id or not all(c.isalnum() or c in "_-" for c in task_id):
        raise WorkspaceError(
            "task id must contain only letters, digits, underscore, or hyphen"
        )


def create_workspace(root: str | os.PathLike[str], task_id: str) -> Workspace:
    _validate_task_id(task_id)
    root = Path(root).resolve()
    task_dir = root / ".rewind" / "tasks" / task_id
    if task_dir.exists():
        raise WorkspaceError(
            f"task {task_id!r} already exists at {task_dir}; choose a new task id"
        )
    repo = task_dir / "repo"
    worktree = task_dir / "worktree"
    repo.mkdir(parents=True)
    gitops.init_repo(repo)

    seed_tree = gitops.write_tree_from_changes(repo, EMPTY_TREE, task.seed_files())
    seed_commit = gitops.commit_tree(
        repo,
        seed_tree,
        message="seed: 30 deterministic files for the scripted agent task",
    )
    gitops.update_ref(repo, "refs/heads/main", seed_commit)

    # This is the only populated checkout.  The repository's primary working
    # tree stays empty, so nothing in the harness edits somebody's checkout.
    proc = gitops.run_git(
        ["worktree", "add", "--detach", "--quiet", os.fspath(worktree), seed_commit],
        cwd=repo,
        check=False,
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            "git worktree add failed: "
            + proc.stderr.decode("utf-8", "replace")
        )

    actual = gitops.worktree_file_sha256(worktree)
    expected = {path: content for path, content in task.seed_files().items()}
    if set(actual) != set(expected):
        raise WorkspaceError(
            f"seed checkout mismatch: {sorted(actual)} vs {sorted(expected)}"
        )
    return Workspace(
        root=root,
        task_id=task_id,
        task_dir=task_dir,
        repo=repo,
        worktree=worktree,
        seed_tree=seed_tree,
        seed_commit=seed_commit,
    )


def load_workspace(root: str | os.PathLike[str], task_id: str) -> Workspace:
    _validate_task_id(task_id)
    root = Path(root).resolve()
    task_dir = root / ".rewind" / "tasks" / task_id
    repo = task_dir / "repo"
    worktree = task_dir / "worktree"
    if not task_dir.exists():
        raise WorkspaceError(f"task {task_id!r} does not exist under {root}")
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise WorkspaceError(f"task repository is missing: {repo}")
    seed_tree = _seed_tree(repo, worktree)
    seed_commit = gitops.git_text(["rev-parse", "refs/heads/main"], cwd=repo)
    return Workspace(
        root=root,
        task_id=task_id,
        task_dir=task_dir,
        repo=repo,
        worktree=worktree,
        seed_tree=seed_tree,
        seed_commit=seed_commit,
    )


def _seed_tree(repo: Path, worktree: Path) -> str:
    if not worktree.exists():
        raise WorkspaceError(f"task worktree is missing: {worktree}")
    commit = gitops.git_text(["rev-parse", "refs/heads/main"], cwd=repo)
    return gitops.git_text(["rev-parse", f"{commit}^{{tree}}"], cwd=repo)


def remove_task(root: str | os.PathLike[str], task_id: str) -> None:
    """Explicit cleanup helper for tests, never called by the harness."""

    _validate_task_id(task_id)
    target = Path(root).resolve() / ".rewind" / "tasks" / task_id
    if target.exists():
        shutil.rmtree(target)

"""Thin, explicit git plumbing helpers used by the crash-safety harness.

The harness never resets, checks out, or cleans any tree.  Snapshots are built
from a throw-away index (``GIT_INDEX_FILE``), so the index of a user checkout
is never touched.  All content addressing is done with real git objects.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

AUTHOR_NAME = "yycoxb"
AUTHOR_EMAIL = "yycoxb@users.noreply.github.com"


class GitError(RuntimeError):
    """A git plumbing command failed."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str
    oid: str


@dataclass(frozen=True)
class FileChange:
    """One file was made absent (``content is None``) or written."""

    content: bytes | None


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # Do not inherit an index override from the caller.
    env.pop("GIT_INDEX_FILE", None)
    env.setdefault("GIT_AUTHOR_NAME", AUTHOR_NAME)
    env.setdefault("GIT_AUTHOR_EMAIL", AUTHOR_EMAIL)
    env.setdefault("GIT_COMMITTER_NAME", AUTHOR_NAME)
    env.setdefault("GIT_COMMITTER_EMAIL", AUTHOR_EMAIL)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if extra:
        env.update(extra)
    return env


def run_git(
    args: Iterable[str],
    *,
    cwd: str | os.PathLike[str],
    input: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=os.fspath(cwd),
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(env),
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"{' '.join(cmd)} failed ({proc.returncode}) in {cwd}:\n"
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc


def git_text(args: Iterable[str], *, cwd: str | os.PathLike[str], **kwargs) -> str:
    return run_git(args, cwd=cwd, **kwargs).stdout.decode("utf-8").strip()


def init_repo(path: str | os.PathLike[str]) -> None:
    run_git(["init", "--quiet", "--initial-branch=main"], cwd=path)


@contextmanager
def temp_index():
    """Yield an environment with a brand-new, unlinked-afterwards index."""

    fd, name = tempfile.mkstemp(prefix="rewind-index-", suffix=".tmp")
    os.close(fd)
    os.unlink(name)
    try:
        yield {"GIT_INDEX_FILE": name}
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def blob_oid(content: bytes) -> str:
    """The SHA-1 git would assign to a blob, without writing an object."""

    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def hash_blob(repo: str | os.PathLike[str], content: bytes) -> str:
    proc = run_git(["hash-object", "-w", "--stdin"], cwd=repo, input=content)
    return proc.stdout.decode("ascii").strip()


def read_tree(repo: str | os.PathLike[str], tree: str) -> dict[str, TreeEntry]:
    proc = run_git(["ls-tree", "-r", "-z", tree], cwd=repo)
    out: dict[str, TreeEntry] = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split(" ")
        out[path.decode("utf-8")] = TreeEntry(mode, kind, oid)
    return out


def cat_blob(repo: str | os.PathLike[str], oid: str) -> bytes:
    return run_git(["cat-file", "blob", oid], cwd=repo).stdout


def write_tree_from_changes(
    repo: str | os.PathLike[str],
    base_tree: str,
    changes: dict[str, bytes | None],
) -> str:
    """Build a tree from ``base_tree`` plus explicit writes/deletions.

    This is used to construct the agent's *intended* tree without ever writing
    the intended files into the live worktree.  It is deliberately independent
    of a branch or commit graph.
    """

    with temp_index() as env:
        run_git(["read-tree", base_tree], cwd=repo, env=env)
        for path, content in changes.items():
            _validate_path(path)
            if content is None:
                run_git(
                    ["update-index", "--force-remove", "--", path],
                    cwd=repo,
                    env=env,
                )
            else:
                oid = hash_blob(repo, content)
                cacheinfo = f"100644,{oid},{path}"
                run_git(
                    ["update-index", "--add", "--cacheinfo", cacheinfo],
                    cwd=repo,
                    env=env,
                )
        return git_text(["write-tree"], cwd=repo, env=env)


def snapshot_worktree(
    worktree: str | os.PathLike[str],
    base_tree: str,
) -> str:
    """Snapshot a worktree's content with the temporary-index recipe.

    ``read-tree <base>`` -> ``add -A`` -> ``write-tree``.  The repository's
    real index is never used: ``GIT_INDEX_FILE`` points at a throw-away file.
    """

    with temp_index() as env:
        run_git(["read-tree", base_tree], cwd=worktree, env=env)
        run_git(["add", "-A"], cwd=worktree, env=env)
        tree = git_text(["write-tree"], cwd=worktree, env=env)
    return tree


def commit_tree(
    repo: str | os.PathLike[str],
    tree: str,
    *,
    parent: str | None = None,
    message: str,
) -> str:
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return git_text(args, cwd=repo)


def update_ref(repo: str | os.PathLike[str], ref: str, object_oid: str) -> None:
    run_git(["update-ref", ref, object_oid], cwd=repo)


def make_ref_commit(
    repo: str | os.PathLike[str],
    *,
    tree: str,
    ref: str,
    parent: str | None,
    message: str,
) -> str:
    commit = commit_tree(repo, tree, parent=parent, message=message)
    update_ref(repo, ref, commit)
    return commit


def materialize_tree(
    repo: str | os.PathLike[str],
    tree: str,
    destination: str | os.PathLike[str],
) -> Path:
    """Write every blob in ``tree`` to a directory, independent of commits."""

    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    for path, entry in read_tree(repo, tree).items():
        _validate_path(path)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = cat_blob(repo, entry.oid)
        if entry.mode == "120000":
            target.symlink_to(content.decode("utf-8"))
        else:
            target.write_bytes(content)
            if entry.mode == "100755":
                target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return root


def diff_trees(
    repo: str | os.PathLike[str],
    old_tree: str,
    new_tree: str,
) -> str:
    """Unified diff between two tree objects using a plumbing command."""

    proc = run_git(
        [
            "diff-tree",
            "-r",
            "-p",
            "--no-commit-id",
            "--no-color",
            "--full-index",
            old_tree,
            new_tree,
        ],
        cwd=repo,
    )
    return proc.stdout.decode("utf-8", "replace")


def _validate_path(path: str) -> None:
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise ValueError(f"unsafe repository path: {path!r}")
    if "\0" in path:
        raise ValueError("NUL in repository path")


def worktree_fingerprint(worktree: str | os.PathLike[str]) -> dict[str, TreeEntry]:
    """Hash a worktree in Python without touching an index.

    The returned mapping has the same shape as :func:`read_tree`, which lets
    the runner cheaply answer "does the live worktree still equal the last
    checkpoint tree?".  ``.git`` is metadata, never content.
    """

    root = Path(worktree)
    entries: dict[str, TreeEntry] = {}
    for current, dirs, files in os.walk(root):
        rel_dir = Path(current).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            if name == ".git" and rel_dir == Path("."):
                continue
            full = Path(current) / name
            rel = (rel_dir / name).as_posix()
            st = full.lstat()
            if stat.S_ISLNK(st.st_mode):
                content = os.readlink(full).encode("utf-8")
                entries[rel] = TreeEntry("120000", "blob", blob_oid(content))
            elif stat.S_ISREG(st.st_mode):
                content = full.read_bytes()
                mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
                entries[rel] = TreeEntry(mode, "blob", blob_oid(content))
    return entries


def write_worktree_file(
    worktree: str | os.PathLike[str],
    path: str,
    content: bytes,
    mode: str = "100644",
) -> None:
    _validate_path(path)
    root = Path(worktree)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.rewind-tmp-{os.getpid()}")
    tmp.write_bytes(content)
    if mode == "100755":
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)
    os.replace(tmp, target)


def remove_worktree_file(worktree: str | os.PathLike[str], path: str) -> None:
    _validate_path(path)
    target = Path(worktree) / path
    try:
        target.unlink()
    except FileNotFoundError:
        return
    parent = target.parent
    root = Path(worktree)
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def list_worktree_files(worktree: str | os.PathLike[str]) -> list[str]:
    root = Path(worktree)
    result: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            rel_dir = Path(current).relative_to(root)
            if name == ".git" and rel_dir == Path("."):
                continue
            result.append((rel_dir / name).as_posix())
    return result


def worktree_file_sha256(worktree: str | os.PathLike[str]) -> dict[str, str]:
    """Per-file SHA-256, used by the acceptance tests."""

    root = Path(worktree)
    out: dict[str, str] = {}
    for rel in list_worktree_files(root):
        out[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    return out

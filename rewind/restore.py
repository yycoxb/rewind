"""The restore decision matrix and three-way merge.

Inputs are three immutable trees: the last agreed base, the agent's intended
tree, and a snapshot of the live worktree.  File identity is the git blob hash,
not mtime.  External (human) edits win a true conflict; the agent version is
kept as a commit under ``refs/rewind/conflicts/...`` and never discarded.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import gitops

RESUMED = "RESUMED"
RESUMED_MERGED = "RESUMED_MERGED"
BLOCKED_ON_CONFLICT = "BLOCKED_ON_CONFLICT"
IN_DOUBT = "IN_DOUBT"

TERMINAL_STATES = {RESUMED, RESUMED_MERGED, BLOCKED_ON_CONFLICT, IN_DOUBT}


@dataclass
class Conflict:
    path: str
    base_oid: str | None
    agent_oid: str | None
    current_oid: str | None
    agent_content: bytes | None
    current_content: bytes | None
    diff: str
    reason: str
    commit: str | None = None
    ref: str | None = None


@dataclass
class RestoreResult:
    status: str
    restored: dict[str, bytes | None]
    merged_paths: list[str] = field(default_factory=list)
    agent_only_paths: list[str] = field(default_factory=list)
    external_only_paths: list[str] = field(default_factory=list)
    both_changed_paths: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)

    @property
    def changed_by_agent(self) -> bool:
        return bool(self.agent_only_paths or self.both_changed_paths)

    @property
    def changed_externally(self) -> bool:
        return bool(self.external_only_paths or self.both_changed_paths)


def _content(
    repo: str | os.PathLike[str],
    entries: dict[str, gitops.TreeEntry],
    path: str,
    cache: dict[str, bytes],
) -> bytes | None:
    entry = entries.get(path)
    if entry is None:
        return None
    if entry.oid not in cache:
        cache[entry.oid] = gitops.cat_blob(repo, entry.oid)
    return cache[entry.oid]


def _diff(current: bytes | None, agent: bytes | None, path: str) -> str:
    if current is not None and b"\0" in current:
        return f"binary content differs for {path}\n"
    if agent is not None and b"\0" in agent:
        return f"binary content differs for {path}\n"
    current_text = (current or b"").decode("utf-8", "replace").splitlines(keepends=True)
    agent_text = (agent or b"").decode("utf-8", "replace").splitlines(keepends=True)
    from_name = f"external:{path}"
    to_name = f"agent:{path}" + (" (deleted)" if agent is None else "")
    if current is None:
        from_name += " (deleted)"
    return "".join(
        difflib.unified_diff(
            current_text,
            agent_text,
            fromfile=from_name,
            tofile=to_name,
            lineterm="\n",
        )
    )


def _merge_file(
    repo: str | os.PathLike[str],
    *,
    base_path: Path,
    current: bytes,
    agent: bytes,
) -> tuple[bool, bytes]:
    """Return (clean, bytes).  ``current`` is 'ours', external wins ties."""

    if b"\0" in current or b"\0" in agent:
        return False, current
    with tempfile.TemporaryDirectory(prefix="rewind-merge-") as tmp:
        root = Path(tmp)
        ours = root / "ours"
        theirs = root / "theirs"
        ours.write_bytes(current)
        theirs.write_bytes(agent)
        proc = gitops.run_git(
            ["merge-file", "-p", os.fspath(ours), os.fspath(base_path), os.fspath(theirs)],
            cwd=repo,
            check=False,
        )
        if proc.returncode == 0:
            return True, proc.stdout
        if proc.returncode == 1:
            return False, current
        raise gitops.GitError(
            "git merge-file failed: "
            + proc.stderr.decode("utf-8", "replace")
        )


def restore_workspace(
    repo: str | os.PathLike[str],
    worktree: str | os.PathLike[str],
    *,
    base_tree: str,
    agent_tree: str,
    current_tree: str,
    apply: bool = True,
) -> RestoreResult:
    """Classify every path and optionally apply the restored content.

    The four terminal states are the only values returned.  A clean merge or
    two non-overlapping change sets are ``RESUMED_MERGED``; a true conflict is
    ``BLOCKED_ON_CONFLICT`` and leaves the external version in place.
    """

    base_entries = gitops.read_tree(repo, base_tree)
    agent_entries = gitops.read_tree(repo, agent_tree)
    current_entries = gitops.read_tree(repo, current_tree)
    cache: dict[str, bytes] = {}

    restored: dict[str, bytes | None] = {}
    merged_paths: list[str] = []
    agent_only: list[str] = []
    external_only: list[str] = []
    both_changed: list[str] = []
    conflicts: list[Conflict] = []

    all_paths = sorted(set(base_entries) | set(agent_entries) | set(current_entries))

    # A base materialization is used as the merge-file BASE.  It is produced
    # from the tree object, not by walking commits, so it works even if the
    # original commit is unreachable.
    merge_candidates: list[str] = []
    for path in all_paths:
        hb = _oid(base_entries, path)
        ha = _oid(agent_entries, path)
        hc = _oid(current_entries, path)
        if (
            ha != hb
            and hc != hb
            and ha is not None
            and hc is not None
            and hb is not None
        ):
            merge_candidates.append(path)

    base_dir: Path | None = None
    base_tmp = None
    if merge_candidates:
        base_tmp = tempfile.TemporaryDirectory(prefix="rewind-base-")
        base_dir = gitops.materialize_tree(repo, base_tree, base_tmp.name)

    try:
        for path in all_paths:
            hb = _oid(base_entries, path)
            ha = _oid(agent_entries, path)
            hc = _oid(current_entries, path)
            base_content = _content(repo, base_entries, path, cache)
            agent_content = _content(repo, agent_entries, path, cache)
            current_content = _content(repo, current_entries, path, cache)

            if ha == hc:
                # Both sides agree (including both deleting, or the agent's
                # side effect is already present in the live worktree).  Keep
                # it; this is not a conflict and needs no merge.
                restored[path] = current_content
                continue

            if ha == hb:
                # Only the external world moved: preserve it verbatim.
                restored[path] = current_content
                external_only.append(path)
                continue

            if hc == hb:
                # Only the agent moved: replay the agent's version.
                restored[path] = agent_content
                agent_only.append(path)
                continue

            # Both moved differently.
            both_changed.append(path)
            if ha is None or hc is None:
                reason = (
                    "agent deleted the file but the external side modified it"
                    if ha is None
                    else "external side deleted the file but the agent modified it"
                )
                conflicts.append(
                    Conflict(
                        path=path,
                        base_oid=hb,
                        agent_oid=ha,
                        current_oid=hc,
                        agent_content=agent_content,
                        current_content=current_content,
                        diff=_diff(current_content, agent_content, path),
                        reason=reason,
                    )
                )
                # External wins.
                restored[path] = current_content
                continue

            if base_content is None:
                # Both sides independently created the same path with
                # different bytes.  There is no three-way base to merge.
                conflicts.append(
                    Conflict(
                        path=path,
                        base_oid=hb,
                        agent_oid=ha,
                        current_oid=hc,
                        agent_content=agent_content,
                        current_content=current_content,
                        diff=_diff(current_content, agent_content, path),
                        reason="both sides created the file with different content",
                    )
                )
                restored[path] = current_content
                continue
            assert base_dir is not None
            clean, merged = _merge_file(
                repo,
                base_path=base_dir / path,
                current=current_content,
                agent=agent_content,
            )
            if clean:
                restored[path] = merged
                merged_paths.append(path)
            else:
                conflicts.append(
                    Conflict(
                        path=path,
                        base_oid=hb,
                        agent_oid=ha,
                        current_oid=hc,
                        agent_content=agent_content,
                        current_content=current_content,
                        diff=_diff(current_content, agent_content, path),
                        reason="three-way merge produced overlapping changes",
                    )
                )
                # External/human version wins; agent bytes stay in the report
                # and will be pinned to a conflict ref by the runner.
                restored[path] = current_content
    finally:
        if base_tmp is not None:
            base_tmp.cleanup()

    if conflicts:
        status = BLOCKED_ON_CONFLICT
    elif agent_only or external_only or both_changed:
        if (agent_only or both_changed) and (external_only or both_changed):
            status = RESUMED_MERGED
        else:
            status = RESUMED
    else:
        status = RESUMED

    applied: list[str] = []
    if apply:
        for path in sorted(restored):
            content = restored[path]
            current = _content(repo, current_entries, path, cache)
            if content is None:
                if (Path(worktree) / path).exists():
                    gitops.remove_worktree_file(worktree, path)
                    applied.append(path)
                continue
            if current == content:
                continue
            gitops.write_worktree_file(worktree, path, content)
            applied.append(path)

    return RestoreResult(
        status=status,
        restored=restored,
        merged_paths=merged_paths,
        agent_only_paths=agent_only,
        external_only_paths=external_only,
        both_changed_paths=both_changed,
        conflicts=conflicts,
        applied_paths=applied,
    )


def _oid(entries: dict[str, gitops.TreeEntry], path: str) -> str | None:
    entry = entries.get(path)
    return entry.oid if entry else None


def save_conflict_refs(
    repo: str | os.PathLike[str],
    *,
    task_id: str,
    seq: int,
    agent_tree: str,
    parent_commit: str | None,
    conflicts: Iterable[Conflict],
) -> str | None:
    """Pin the complete agent tree to one commit and one ref per conflict path."""

    conflicts = list(conflicts)
    if not conflicts:
        return None
    paths = ", ".join(c.path for c in conflicts)
    commit = gitops.commit_tree(
        repo,
        agent_tree,
        parent=parent_commit,
        message=f"rewind conflict snapshot for {task_id} step {seq}: {paths}",
    )
    for conflict in conflicts:
        ref = f"refs/rewind/conflicts/{task_id}/{seq:04d}/{conflict.path}"
        if not _valid_ref(repo, ref):
            ref = (
                f"refs/rewind/conflicts/{task_id}/{seq:04d}/"
                + _sanitize_path(conflict.path)
            )
        gitops.update_ref(repo, ref, commit)
        conflict.commit = commit
        conflict.ref = ref
    return commit


def _valid_ref(repo: str | os.PathLike[str], ref: str) -> bool:
    proc = gitops.run_git(["check-ref-format", ref], cwd=repo, check=False)
    return proc.returncode == 0


def _sanitize_path(path: str) -> str:
    result = []
    for part in path.split("/"):
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in part)
        safe = safe.lstrip(".") or "_"
        result.append(safe)
    return "/".join(result)

"""Crash-safe task execution and resume.

A step is journaled as four records:

    intent -> effect -> result -> checkpoint

The intent is fsynced before any file is touched.  The checkpoint is written
only after the worktree has been snapshotted and a ref has been updated.  A
SIGKILL anywhere in that window therefore leaves a discoverable in-doubt
intent, never a half-recorded success.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import gitops, restore, task
from .artifacts import Artifacts, read_state, write_state
from .journal import CHECKPOINT, EFFECT, INTENT, RESULT, Journal
from .workspace import Workspace, create_workspace, load_workspace


class RunnerError(RuntimeError):
    pass


class Runner:
    def __init__(
        self,
        *,
        workspace: Workspace,
        artifacts: Artifacts,
        state: dict,
        journal: Journal,
        crash_at: int | None = None,
        pause_at: int | None = None,
    ):
        self.workspace = workspace
        self.artifacts = artifacts
        self.state = state
        self.journal = journal
        self.crash_at = crash_at
        self.pause_at = pause_at
        self._closed = False

    # ------------------------------------------------------------------ start
    @classmethod
    def start(
        cls,
        root: str | os.PathLike[str],
        task_id: str,
        *,
        crash_at: int | None = None,
        pause_at: int | None = None,
        echo: bool = True,
    ) -> "Runner":
        workspace = create_workspace(root, task_id)
        run_id = artifacts_mod.new_run_id(task_id)
        artifacts = Artifacts(workspace.root / "runs", run_id, echo=echo)
        artifacts.session("run started")
        journal = Journal(
            artifacts.journal_path, task_id=task_id, run_id=run_id
        )
        state = {
            "task_id": task_id,
            "run_id": run_id,
            "root": os.fspath(workspace.root),
            "repo": os.fspath(workspace.repo),
            "worktree": os.fspath(workspace.worktree),
            "seed_tree": workspace.seed_tree,
            "seed_commit": workspace.seed_commit,
            "current_tree": workspace.seed_tree,
            "current_commit": workspace.seed_commit,
            "steps_done": 0,
            "status": "RUNNING",
            "merge_used": False,
            "in_doubt_seen": False,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "artifact_dir": os.fspath(artifacts.dir),
            "journal": os.fspath(artifacts.journal_path),
            "total_steps": task.TOTAL_STEPS,
            "crash_at": crash_at,
            "pause_at": pause_at,
        }
        write_state(workspace.state_path, state)
        journal.append(
            "run_start",
            mode="fresh",
            total_steps=task.TOTAL_STEPS,
            seed_tree=workspace.seed_tree,
            crash_at=crash_at,
            pause_at=pause_at,
        )
        journal.append(
            "workspace_init",
            repo=os.fspath(workspace.repo),
            worktree=os.fspath(workspace.worktree),
            seed_commit=workspace.seed_commit,
            isolated_worktree=True,
        )
        artifacts.trace(
            f"workspace ready: isolated git worktree {workspace.worktree} "
            f"(seed tree {workspace.seed_tree}); user tree/index untouched"
        )
        runner = cls(
            workspace=workspace,
            artifacts=artifacts,
            state=state,
            journal=journal,
            crash_at=crash_at,
            pause_at=pause_at,
        )
        return runner

    # ----------------------------------------------------------------- resume
    @classmethod
    def resume(
        cls,
        root: str | os.PathLike[str],
        task_id: str,
        *,
        crash_at: int | None = None,
        pause_at: int | None = None,
        echo: bool = True,
    ) -> "Runner":
        workspace = load_workspace(root, task_id)
        state = read_state(workspace.state_path)
        artifacts = Artifacts(
            Path(state["root"]) / "runs", state["run_id"], echo=echo
        )
        artifacts.session("resume")
        journal = Journal(
            artifacts.journal_path, task_id=task_id, run_id=state["run_id"]
        )
        runner = cls(
            workspace=workspace,
            artifacts=artifacts,
            state=state,
            journal=journal,
            crash_at=crash_at,
            pause_at=pause_at,
        )
        runner._sync_state_from_journal()
        runner.artifacts.trace(
            f"resume: journal contains {len(journal.records())} records, "
            f"{len(journal.find_in_doubt())} unresolved intent(s); "
            f"last completed step {state.get('steps_done')}"
        )
        return runner

    # ------------------------------------------------------------------- run
    def execute(self) -> dict:
        try:
            if self.state.get("status") in {restore.BLOCKED_ON_CONFLICT, restore.IN_DOUBT}:
                self.artifacts.trace(
                    f"task is terminal ({self.state['status']}); not resuming blindly"
                )
                self._finish_artifacts()
                return self.state
            if self.state.get("steps_done", 0) >= task.TOTAL_STEPS:
                self.artifacts.trace("all steps already checkpointed")
                self._finalize_success()
                return self.state

            while self.state["steps_done"] < task.TOTAL_STEPS:
                step = self.state["steps_done"] + 1
                idem_key = self._step_key(step)
                already_intent = self.journal.has_intent(idem_key)
                already_result = self.journal.has_result(idem_key)
                if already_result and not self._has_completed_checkpoint(step):
                    # Result survived but the checkpoint record did not.  The
                    # ref is the authority; recover the checkpoint record from
                    # the content matrix rather than trusting the result alone.
                    self.artifacts.trace(
                        f"step {step:03d}: result without checkpoint; revalidating"
                    )
                status = self._do_step(step, recovery=already_intent)
                if status == restore.BLOCKED_ON_CONFLICT:
                    break
                if self.state["steps_done"] < step:
                    raise RunnerError(
                        f"step {step} did not advance state (stuck at "
                        f"{self.state['steps_done']})"
                    )
            if self.state.get("status") == restore.BLOCKED_ON_CONFLICT:
                self._finish_artifacts()
                return self.state
            self._finalize_success()
            return self.state
        finally:
            if not self._closed:
                self.journal.close()
                self.artifacts.close()
                self._closed = True

    # --------------------------------------------------------------- one step
    def _do_step(self, step: int, *, recovery: bool) -> str:
        idem_key = self._step_key(step)
        changes = task.step_changes(step)
        base_tree = self.state["current_tree"]
        base_commit = self.state["current_commit"]
        in_doubt = recovery and not self.journal.has_result(idem_key)
        base_entries = gitops.read_tree(self.workspace.repo, base_tree)
        creates = sorted(
            path
            for path, content in changes.items()
            if content is not None and path not in base_entries
        )
        deletes = sorted(path for path, content in changes.items() if content is None)

        if not self.journal.has_intent(idem_key):
            self.journal.append(
                INTENT,
                op="file_step",
                effect_class="A",
                idem_key=idem_key,
                step=step,
                paths=sorted(changes),
                creates=creates,
                deletes=deletes,
                note="class-A deterministic file write; idempotent replay",
            )
            self.artifacts.trace(
                f"step {step:03d}: intent {idem_key} persisted before side effects"
            )
        else:
            self.artifacts.trace(
                f"step {step:03d}: found existing intent {idem_key}"
                + (" (in-doubt: no result)" if in_doubt else "")
            )

        live = gitops.worktree_fingerprint(self.workspace.worktree)
        clean = live == base_entries

        if clean and not recovery:
            # Fast path: the live worktree equals the last checkpoint.  Apply
            # the deterministic step directly and snapshot once.
            for path, content in changes.items():
                if content is None:
                    gitops.remove_worktree_file(self.workspace.worktree, path)
                else:
                    gitops.write_worktree_file(self.workspace.worktree, path, content)
            self.artifacts.trace(
                f"step {step:03d}: applied scripted change(s) {sorted(changes)}"
            )
            self._crash_hook(step)
            tree = gitops.snapshot_worktree(self.workspace.worktree, base_tree)
            effect_paths = sorted(changes)
            status = restore.RESUMED
            merged = False
        else:
            # Recovery / external-change path.  Build the agent's intended
            # tree without touching the worktree, snapshot the live world, and
            # let the content matrix decide what survives.
            agent_tree = gitops.write_tree_from_changes(
                self.workspace.repo, base_tree, changes
            )
            # Pin the matrix inputs as diagnostic commits too; nothing the
            # harness creates should depend on unreferenced objects.
            self._pin_tree(
                agent_tree,
                f"intended/{step:04d}",
                base_commit,
                f"rewind intended tree {self.workspace.task_id} step {step:04d}",
            )
            current_tree = gitops.snapshot_worktree(
                self.workspace.worktree, base_tree
            )
            self._pin_tree(
                current_tree,
                f"inspect/{step:04d}",
                base_commit,
                f"rewind pre-restore worktree {self.workspace.task_id} step {step:04d}",
            )
            result = restore.restore_workspace(
                self.workspace.repo,
                self.workspace.worktree,
                base_tree=base_tree,
                agent_tree=agent_tree,
                current_tree=current_tree,
                apply=True,
            )
            self.artifacts.trace(
                f"step {step:03d}: restore matrix -> {result.status} "
                f"(agent-only={len(result.agent_only_paths)}, "
                f"external-only={len(result.external_only_paths)}, "
                f"both={len(result.both_changed_paths)}, "
                f"conflicts={len(result.conflicts)})"
            )
            if result.conflicts:
                restore.save_conflict_refs(
                    self.workspace.repo,
                    task_id=self.workspace.task_id,
                    seq=step,
                    agent_tree=agent_tree,
                    parent_commit=base_commit,
                    conflicts=result.conflicts,
                )
                blocked_tree = gitops.snapshot_worktree(
                    self.workspace.worktree, base_tree
                )
                return self._block_on_conflict(
                    step=step,
                    idem_key=idem_key,
                    tree=blocked_tree,
                    base_commit=base_commit,
                    conflicts=result.conflicts,
                )
            self._crash_hook(step)
            tree = gitops.snapshot_worktree(self.workspace.worktree, base_tree)
            effect_paths = sorted(result.applied_paths)
            status = result.status
            merged = status == restore.RESUMED_MERGED

        ref = f"refs/rewind/{self.workspace.task_id}/{step:04d}"
        commit = gitops.make_ref_commit(
            self.workspace.repo,
            tree=tree,
            ref=ref,
            parent=base_commit,
            message=(
                f"rewind checkpoint {self.workspace.task_id} step {step:04d} "
                f"({idem_key})"
            ),
        )
        self.journal.append(
            EFFECT,
            op="file_write",
            effect_class="A",
            idem_key=idem_key,
            step=step,
            phase="applied",
            paths=effect_paths,
            creates=creates,
            deletes=deletes,
            tree=tree,
        )
        self.journal.append(
            RESULT,
            op="file_step",
            effect_class="A",
            idem_key=idem_key,
            step=step,
            status=status,
            tree=tree,
            commit=commit,
            ref=ref,
            creates=creates,
            deletes=deletes,
            in_doubt=in_doubt,
            resolved_in_doubt=in_doubt,
        )
        self.journal.append(
            CHECKPOINT,
            step=step,
            tree=tree,
            commit=commit,
            ref=ref,
            completed=True,
            status=status,
        )

        self.state["current_tree"] = tree
        self.state["current_commit"] = commit
        self.state["steps_done"] = step
        self.state["status"] = "RUNNING"
        self.state["merge_used"] = bool(self.state.get("merge_used")) or merged
        self.state["in_doubt_seen"] = bool(self.state.get("in_doubt_seen")) or in_doubt
        write_state(self.workspace.state_path, self.state)

        self.artifacts.trace(
            f"step {step:03d}: checkpoint tree={tree} ref={ref} "
            f"status={status}"
        )
        return status

    def _block_on_conflict(
        self,
        *,
        step: int,
        idem_key: str,
        tree: str,
        base_commit: str,
        conflicts: list[restore.Conflict],
    ) -> str:
        ref = f"refs/rewind/{self.workspace.task_id}/{step:04d}"
        commit = gitops.make_ref_commit(
            self.workspace.repo,
            tree=tree,
            ref=ref,
            parent=base_commit,
            message=(
                f"rewind blocked checkpoint {self.workspace.task_id} "
                f"step {step:04d} (external wins)"
            ),
        )
        serialized = [
            {
                "path": c.path,
                "base_oid": c.base_oid,
                "agent_oid": c.agent_oid,
                "current_oid": c.current_oid,
                "reason": c.reason,
                "diff": c.diff,
                "commit": c.commit,
                "ref": c.ref,
            }
            for c in conflicts
        ]
        self.journal.append(
            RESULT,
            op="file_step",
            effect_class="A",
            idem_key=idem_key,
            step=step,
            status=restore.BLOCKED_ON_CONFLICT,
            tree=tree,
            commit=commit,
            ref=ref,
            conflicts=serialized,
            in_doubt=False,
        )
        self.journal.append(
            CHECKPOINT,
            step=step,
            tree=tree,
            commit=commit,
            ref=ref,
            completed=False,
            status=restore.BLOCKED_ON_CONFLICT,
            conflicts=[c["path"] for c in serialized],
        )
        self.state["status"] = restore.BLOCKED_ON_CONFLICT
        self.state["blocked_tree"] = tree
        self.state["blocked_ref"] = ref
        self.state["blocked_commit"] = commit
        self.state["blocked_step"] = step
        write_state(self.workspace.state_path, self.state)
        for c in conflicts:
            self.artifacts.trace(
                f"step {step:03d}: CONFLICT {c.path}; external wins; "
                f"agent version pinned at {c.ref}"
            )
        return restore.BLOCKED_ON_CONFLICT

    def _crash_hook(self, step: int) -> None:
        if self.crash_at == step:
            self.artifacts.trace(
                f"step {step:03d}: injecting SIGKILL before result/checkpoint"
            )
            self.journal.close()
            self.artifacts.close()
            self._closed = True
            os.kill(os.getpid(), signal.SIGKILL)
        if self.pause_at == step:
            self.artifacts.trace(
                f"step {step:03d}: paused for external SIGKILL before checkpoint"
            )
            print(f"REWIND_KILL_READY step={step}", flush=True)
            time.sleep(600)

    # --------------------------------------------------------------- finalize
    def _finalize_success(self) -> None:
        self.state["status"] = (
            restore.RESUMED_MERGED
            if self.state.get("merge_used")
            else restore.RESUMED
        )
        self.state["final_tree"] = self.state["current_tree"]
        self._finish_artifacts()
        if not self._has_run_end():
            self.journal.append(
                "run_end",
                status=self.state["status"],
                completed_steps=self.state["steps_done"],
                final_tree=self.state["current_tree"],
            )
            write_state(self.workspace.state_path, self.state)
            self._render_report()
        self.artifacts.trace(
            f"run finished: status={self.state['status']} "
            f"steps={self.state['steps_done']}/{task.TOTAL_STEPS}"
        )

    def _finish_artifacts(self) -> None:
        final_tree = (
            self.state.get("blocked_tree")
            if self.state.get("status") == restore.BLOCKED_ON_CONFLICT
            else self.state.get("current_tree")
        )
        if final_tree:
            diff = gitops.diff_trees(
                self.workspace.repo, self.workspace.seed_tree, final_tree
            )
            artifacts_mod.atomic_write(self.artifacts.diff_path, diff)
        self._render_report()

    def _render_report(self) -> None:
        artifacts_mod.generate_report(
            state=self.state,
            journal_path=self.artifacts.journal_path,
            artifacts=self.artifacts,
        )

    def _has_run_end(self) -> bool:
        return any(r.get("type") == "run_end" for r in self.journal.records())

    def _has_completed_checkpoint(self, step: int) -> bool:
        return any(
            r.get("type") == CHECKPOINT
            and r.get("step") == step
            and r.get("completed", False)
            for r in self.journal.records()
        )

    def _sync_state_from_journal(self) -> None:
        checkpoint = self.journal.last_completed_checkpoint()
        if not checkpoint:
            return
        step = int(checkpoint["step"])
        if step > self.state.get("steps_done", 0):
            self.state["steps_done"] = step
            self.state["current_tree"] = checkpoint["tree"]
            self.state["current_commit"] = checkpoint["commit"]
            self.artifacts.trace(
                f"state was behind journal; advanced to checkpoint step {step}"
            )
        merge_used = any(
            r.get("type") == RESULT and r.get("status") == restore.RESUMED_MERGED
            for r in self.journal.records()
        )
        in_doubt = any(
            r.get("type") == RESULT and r.get("in_doubt") for r in self.journal.records()
        )
        self.state["merge_used"] = bool(self.state.get("merge_used")) or merge_used
        self.state["in_doubt_seen"] = bool(self.state.get("in_doubt_seen")) or in_doubt

    def _pin_tree(
        self,
        tree: str,
        suffix: str,
        parent: str,
        message: str,
    ) -> str:
        ref = f"refs/rewind/{self.workspace.task_id}/{suffix}"
        return gitops.make_ref_commit(
            self.workspace.repo,
            tree=tree,
            ref=ref,
            parent=parent,
            message=message,
        )

    def _step_key(self, step: int) -> str:
        return f"{self.workspace.task_id}:step:{step:04d}"


def start_run(
    root: str | os.PathLike[str],
    task_id: str,
    *,
    crash_at: int | None = None,
    pause_at: int | None = None,
    echo: bool = True,
) -> dict:
    runner = Runner.start(
        root, task_id, crash_at=crash_at, pause_at=pause_at, echo=echo
    )
    return runner.execute()


def resume_run(
    root: str | os.PathLike[str],
    task_id: str,
    *,
    crash_at: int | None = None,
    pause_at: int | None = None,
    echo: bool = True,
) -> dict:
    runner = Runner.resume(
        root, task_id, crash_at=crash_at, pause_at=pause_at, echo=echo
    )
    return runner.execute()


def get_state(root: str | os.PathLike[str], task_id: str) -> dict:
    workspace = load_workspace(root, task_id)
    return read_state(workspace.state_path)

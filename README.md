# rewind — crash-safe checkpoint/resume harness for a terminal coding agent

`rewind` is a small, real, runnable PoC of the harness layer around a coding
agent.  It answers two questions with code and evidence, not mock logs:

1. If a long task is killed in the middle, how does a restart know what
   happened and finish without losing work?
2. If the agent's last checkpoint and an external edit disagree, who wins, how
   is that decided, and where is the losing version kept?

The agent in this repository is intentionally **not an LLM**.  It is a
deterministic script that rewrites 30 files in 50 steps.  That is a deliberate
choice: the harness, not model quality, is the subject.  Every clean run and
every crash/resume run is therefore byte-for-byte comparable.

Everything is Python 3 standard library only.  There are no pip dependencies
and no network calls outside localhost.

## Quick start

```bash
python3 -m unittest discover -s tests -v   # full suite
bash demo.sh                               # three-part demo, exit 0 = all good

python3 -m rewind run    --task t30
python3 -m rewind resume --task t30
python3 -m rewind report --task t30
```

A task id can be used once.  Use a fresh id (for example `t30b`) to run a new
attempt.  State lives under `.rewind/tasks/<task>/`; artifacts live under
`runs/<run_id>/`.

To exercise the two crash windows directly:

```bash
# Real in-process SIGKILL after step 7, before its result/checkpoint.
REWIND_CRASH_AT=7 python3 -m rewind run --task crash7
python3 -m rewind resume --task crash7

# The test/demo hook used by the suite: pause after step 5's writes, then a
# different process sends SIGKILL.  It prints: REWIND_KILL_READY step=5
REWIND_TEST_PAUSE_AT=5 python3 -m rewind run --task kill5
```

## What the measured runs show

These numbers are copied from actual output of this repository, not estimated;
wall-clock timings vary slightly between machines and runs.

| Check | Actual result |
|---|---|
| Full test suite | **16 tests, OK**, `Ran 16 tests in 83.552s` |
| External `SIGKILL` rounds | **20/20** resumed runs whose final 30-file per-file SHA-256 map equals a clean golden run |
| Interruption steps used | 1, 3, 5, ..., 39 (20 distinct points) |
| Clean task | 50 steps, 30 files, 203 journal records |
| Retained clean run | `runs/20261120T041713Z-t30-615779/` |
| Retained journal | `journal.jsonl` = **203 lines** |
| Retained trace | `trace.txt` = **153 lines** |
| Retained final patch | `final.diff` = **450 lines** |
| Task refs after clean run | 50 `refs/rewind/t30/0001..0050` plus `refs/heads/main` |
| Demo | **exit 0**, summary `20/20`, elapsed `78.47s` |
| Committed crash+resume run | `runs/20261120T053536Z-crash25-ca99cb/` — `SIGKILL` at step 25, resumed to 50/50; final tree `e41dd5c2...` is identical to the clean run's final tree |

Two runs are committed so the evidence can be checked without trusting prose:
the clean `t30` run, and `crash25`, which was really killed at step 25 and then
resumed.  Both are `journal.jsonl` + `trace.txt` + `report.md` + `final.diff`.

The demo's measured summary was:

```text
SUMMARY
  (a) kill -9: 20/20 passed (all final sha256 match golden)
  (b) conflict: BLOCKED_ON_CONFLICT; external edit kept; agent version at refs/rewind/conflicts/conflict_72ade5/0005/src/mod_04.py
  (c) effect: IN_DOUBT; reconciled=True; POST count 1 (no retry)
  elapsed: 78.47s
```

The conflict ref and task id above are from that particular demo run; running
`bash demo.sh` creates fresh random task ids and prints its own refs.

In the retained artifacts, the absolute host path of this checkout is replaced
by `$REPO` so the logs carry no machine-specific paths.  No other field in the
retained run was edited.

## Repository layout

```text
rewind/
├── demo.sh                    # one-command three-part demo
├── rewind/
│   ├── journal.py             # JSONL append, flush + fsync, in-doubt query
│   ├── gitops.py              # git plumbing, temp-index snapshots, refs
│   ├── workspace.py           # isolated task repo + linked git worktree
│   ├── restore.py             # six-cell decision matrix + merge-file
│   ├── task.py                # deterministic 30-file / 50-step plan
│   ├── runner.py              # intent -> effect -> result -> checkpoint
│   ├── artifacts.py           # journal/trace/report/final.diff
│   ├── effects.py             # class-B compensatable effects
│   ├── outbox.py              # class-C outbox client + localhost server harness
│   ├── outbox_server.py       # localhost durable idempotency-key service
│   ├── effect_task.py         # crash/resume demo for a class-C effect
│   ├── killtest.py            # real child SIGKILL and golden-manifest helpers
│   ├── demo.py                # (a) kill9 (b) conflict (c) effect
│   └── cli.py                 # python3 -m rewind ...
├── tests/
│   ├── test_matrix.py         # six cells, one test each
│   ├── test_journal.py
│   ├── test_outbox.py
│   ├── test_conflict.py
│   ├── test_effect_grades.py
│   └── test_kill9.py          # 20 external SIGKILL rounds + REWIND_CRASH_AT
└── runs/
    └── 20261120T041713Z-t30-615779/   # retained real clean-run artifacts
```

## Crash-consistency protocol

### Journal

`journal.jsonl` is append-only JSONL.  Every record is written with one
`write`, then `flush()`, then `os.fsync()`.  A record has a monotonic sequence
number, run id, task id, type, and operation-specific fields.

A step emits exactly four records in this order:

```text
intent  ->  effect  ->  result  ->  checkpoint
```

The **intent carries the idempotency key** (`<task>:step:<number>`) and is
fsynced before any file is touched.  The checkpoint is written only after:

1. the live worktree has been snapshotted to a tree OID;
2. that tree has been committed with `commit-tree`;
3. `refs/rewind/<task>/<step>` has been updated with `update-ref`.

Consequently, a crash can leave an intent without a result.  That is the
definition of in-doubt.  In recovery, the harness re-runs the deterministic
step through the content matrix instead of blindly applying writes or assuming
success.  For class-A file steps this is an idempotent replay; for a class-C
effect it is a read-only lookup and **no retry**.

A torn final JSONL line caused by a hard kill is tolerated and ignored;
corruption before the final line is not silently ignored.

### Snapshot without touching anyone's index

The worktree is snapshotted with exactly this recipe:

```text
GIT_INDEX_FILE=<temp>
git read-tree <base_tree>
git add -A
git write-tree
```

The temporary index is unlinked afterwards.  The harness never uses the task
repository's real index, and never invokes a worktree-wide destructive command.
The task's linked worktree is created with `git worktree add`; the repository's
primary checkout is left empty and unused.  The caller's repository, working
tree, and index are untouched.

`git merge-file` receives a base file materialized directly from the base
**tree object** (via `ls-tree` + `cat-file`), not through a commit history, so
the base remains usable even if a commit is not reachable.

### Refs

Every checkpoint tree is kept alive by a real commit and ref:

```text
refs/rewind/<task>/0001 ... refs/rewind/<task>/0050
```

Recovery additionally pins its matrix inputs as diagnostic refs
(`refs/rewind/<task>/intended/<step>` and `inspect/<step>`).  A blocked
checkpoint also gets a numeric ref.  Conflict versions use the required form:

```text
refs/rewind/conflicts/<task>/<step>/<path>
```

The ref points at a commit whose tree is the complete agent version, so the
losing bytes can be inspected with ordinary `git cat-file`:

```bash
git cat-file blob 'refs/rewind/conflicts/<task>/0005/src/mod_04.py:src/mod_04.py'
```

## The six-cell restore matrix

The inputs are three immutable trees: `base_tree` (last agreed checkpoint),
`agent_tree` (the intended step), and `current_tree` (a snapshot of the live
worktree).  Each path is compared by git blob hash, not mtime.

| Base vs agent | Base vs current | Paths | Decision |
|---|---|---|---|
| same | same | — | `RESUMED` |
| same | external changed | — | keep external, `RESUMED` |
| agent changed | same | — | replay agent, `RESUMED` |
| both changed | both changed | disjoint path sets | `RESUMED_MERGED` |
| both changed | both changed | overlapping, `git merge-file` clean | `RESUMED_MERGED` |
| both changed | both changed | overlapping, true conflict | `BLOCKED_ON_CONFLICT` |

The implementation returns exactly one of `RESUMED`, `RESUMED_MERGED`,
`BLOCKED_ON_CONFLICT`, or `IN_DOUBT`.  The six content cells produce the first
three; `IN_DOUBT` is the fourth terminal state, produced when a class-C effect
intent has no result and must be reconciled.  Deletions are handled as absent
versions; an agent delete versus an external edit is a true conflict.

### Conflict policy

External/human edits win.  The external bytes remain in the worktree.  The
agent version is not discarded: it is committed and pinned at
`refs/rewind/conflicts/<task>/<step>/<path>`, and `report.md` contains a unified
diff plus the ref and blob ids.  Unrelated external edits are preserved too.
The run stops at `BLOCKED_ON_CONFLICT`; it does not auto-resolve a human
decision.

Untracked files are never bulk-deleted.  Only a deletion already represented by
the tree matrix is applied; an external untracked file remains.  The test suite
exercises this explicitly.

## Effects A / B / C

- **A — idempotent replay.** The 30-file task uses class-A file writes.  The
  deterministic content and idempotency key make replay safe.
- **B — compensatable.** `execute_compensatable` journals an intent, applies
  work, and records a result.  Recovery calls a caller-supplied compensator for
  an in-doubt B intent before any retry.  This is unit-tested, not used by the
  t30 plan.
- **C — external irreversible.** `OutboxClient` fsyncs an intent before the
  HTTP POST.  The localhost server durably stores the effect under the
  idempotency key before acknowledging it.  The demo injects a real `SIGKILL`
  after the server applied the effect but before the client journaled the ack.
  Recovery enters `IN_DOUBT`, performs a read-only `GET` lookup by key, records
  the reconciliation, and sends **no second POST**.  The measured server stats
  after recovery were `post_count=1, stored_effects=1`.

The irreversible-effect demo is a separate auxiliary task, not a step inside
t30.  This is deliberate and is listed again under limitations.

## Tests and demo

```text
$ python3 -m unittest discover -s tests -v
...
Ran 16 tests in 83.552s

OK
```

Coverage includes one test per matrix cell, journal in-doubt detection and
torn-tail handling, outbox idempotency and read-only reconciliation, real
conflict adjudication with the agent version retained, the untracked-file rule,
class-B compensation, 20 rounds of external `SIGKILL`, and the
`REWIND_CRASH_AT` self-kill path.

`tests/test_kill9.py` launches a real child process, waits for
`REWIND_KILL_READY step=N` after the step's writes, sends `SIGKILL` from the
parent, verifies the child exit code is `-9`, resumes in a separate call, and
compares the complete per-file SHA-256 mapping with the golden clean run.

`bash demo.sh` runs 20 kill/resume rounds, the conflict scenario, and the
class-C in-doubt scenario.  It prints a summary and exits `0` only when all
three checks pass.  The measured run above exited `0`.

## Honest limitations / non-goals

These are the places where this PoC is narrower than a production harness:

1. **The agent is scripted.** It is a fixed 50-step plan, not an LLM and not a
   tool-using coding agent.  The README number "30 files" is the task's actual
   file count, not a model benchmark.
2. **The class-C effect is not embedded in t30.** t30 is all class-A file
   writes; the irreversible outbox path is demonstrated by the auxiliary
   effect task, the demo, and tests.  There is no single run that contains all
   30 file edits plus a C effect.
3. **External edits are simulated in the task worktree.** After the kill, the
   demo/test writes the human version into the same isolated task worktree.
   This models an external editor touching that checkout.  The harness itself
   never touches the caller's checkout or index, and there is no second
   user-facing checkout merge workflow.
4. **Crash windows are sampled, not exhaustive.** The external-kill test covers
   one window per step (file writes durable, result/checkpoint absent) at 20
   distinct steps.  The class-C test covers the post-ack/pre-result window.
   Other syscall boundaries are not exhaustively fault-injected.
5. **Single writer.** There is no lock against two agents mutating the same
   worktree concurrently.  `git merge-file` is line-based; binary files and
   mode-only changes are treated conservatively (binary overlap is a conflict;
   pure mode changes are not modeled).
6. **The effect server is local and unauthenticated.** It exists to make the
   idempotency-key reconciliation observable, not to be a production service.
7. **The harness relies on git and filesystem semantics.** It fsyncs journal
   records and server state, but does not fsync every worktree file write
   before the snapshot; the snapshot is the commit point.  A machine or
   filesystem that reorders or loses data below that layer is out of scope.
8. **No remote backup.** `refs/rewind/...` protects objects from local gc; it
   does not push them anywhere.

## Reproducing the acceptance checks

```bash
# 1. tests
python3 -m unittest discover -s tests -v

# 2. demo
bash demo.sh
echo $?

# 3. retained artifacts
wc -l runs/20261120T041713Z-t30-615779/journal.jsonl
wc -l runs/20261120T041713Z-t30-615779/trace.txt
head -20 runs/20261120T041713Z-t30-615779/report.md

# 4. report CLI against the retained run
python3 -m rewind report --task t30
```

`report` reads the complete report and prints the path.  All numbers in this
README are from the runs cited above.

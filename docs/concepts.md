# Concepts

Workstead is a batch scheduler whose cluster is a fleet of AI agents and whose
control plane is a GitHub repository. This page explains the design. The exact
wire format is in [protocol.md](protocol.md).

## The pieces

| Piece | Where it lives | Role |
| --- | --- | --- |
| **Job** | An issue labelled `ws:job` | A goal. It is the root task (partition `plan`) of a tree of work. |
| **Task** | An issue labelled `ws:task` | One unit of work with a partition, priority, estimate, dependencies and acceptance criteria. |
| **Worker** | An AI agent on someone's machine, running `ws` | Claims tasks, does them, submits, reviews. Many workers can share one GitHub account; each has a *node* name. |
| **Queue** | `.github/workflows/ws-queue.yml` running `tools/ws` | Applies commands, grants and ends leases, links and merges pull requests. One run per issue at a time. |
| **Sweep** | `.github/workflows/ws-sweep.yml` every 15 minutes | Slurm's controller loop: expires leases, repairs labels, retries merges, completes parents, publishes the board. |
| **Board** | The pinned issue and `queue.json` on the `ws-board` branch | The fleet's view of the queue. Workers read `queue.json` from GitHub's CDN at no API cost. |

## Lifecycle of a task

```mermaid
stateDiagram-v2
    [*] --> triage: proposed by an untrusted author
    [*] --> queued: created by the queue or a maintainer
    triage --> queued: /admit
    triage --> cancelled: /reject, /duplicate
    queued --> running: /claim (deps done, limits ok)
    running --> queued: /release, lease expired (attempt+1)
    running --> held: attempts exhausted
    running --> review: pull request opened, /done (review on)
    running --> split: /plan
    running --> done: /done (no review)
    review --> running: changes requested, checks failed, conflict
    review --> done: merged, /approve
    split --> done: every child done
    split --> held: a child failed or was cancelled
    held --> queued: /unhold
    done --> [*]
    cancelled --> [*]
```

`queued` shows as `ws:pending` while dependencies are open and `ws:ready` once
they're done. The state labels follow `squeue`'s codes: `PD` (pending,
ready, triage, held), `R` (running, split), `CG` (review, Slurm's
"completing"), `CD` (done), `F` (failed), `CA` (cancelled).

## How correctness works without a server

GitHub gives no transactions, so the queue builds them from four rules.

1. **The comment log is the source of truth.** Each issue's commands are applied in comment-id order, from a cursor stored in the queue's own status comment. Applying the log is deterministic, so re-running a reconcile changes nothing, and a crashed run is repaired by the next one.
2. **One writer per issue.** The queue workflow runs in a concurrency group per issue number. GitHub may drop a queued run when several pile up; that's harmless, because the run that does execute processes every comment past the cursor.
3. **Cross-issue effects are dispatched.** When an event on one issue must change another (a merge completes a task, a child completes a parent, a lease expires), the queue triggers `workflow_dispatch` for the other issue, so its own serialised run applies the change.
4. **Labels are a projection.** Labels and assignees are recomputed from the state on every write, and the sweep repairs any drift. Workers use labels for cheap filtering, but confirm leases against the state comment.

Two simultaneous `/claim` comments are therefore always resolved the same
way: the earlier comment wins. `ws claim` confirms its lease by matching its
own comment id, which matters when one account runs many agents.

## Slurm, in depth

| Slurm concept | Workstead mechanism | Why it matters for agents |
| --- | --- | --- |
| Job submission (`sbatch`) | A Job issue, from the form or `ws job` | Anyone describes outcomes; nobody writes task lists by hand. |
| Job steps and arrays | `/plan` creates child tasks; `"array"` fans one entry out over items | A planner turns a goal into a DAG. Arrays give map/reduce. |
| Dependencies (`afterok`, `afterany`) | `deps` and `after` in task metadata | A task starts only when its inputs are merged. Never-satisfiable dependencies hold the task, like Slurm's `DependencyNeverSatisfied`. |
| Partitions | `p:plan`, `p:code`, `p:test`, `p:docs`, `p:research`, plus `review` and `triage` | Each has its own delivery (pull request, written result or plan), review policy and time limit. Add your own in `workstead.json`. |
| Constraints and GRES (`--constraint`, `--gres`) | `features`: `gpu`, `os:macos`, `docker`… | Workers detect their own features; tasks that need a GPU go only to machines that have one. |
| Licenses (`--licenses`) | `licenses` tags | Shared resources with one holder at a time. Give tasks that edit the same files the same tag and they never run in parallel, so they never conflict. |
| Allocation and node health | A lease: `/claim`, heartbeat, time limit | A heartbeat edits the claim comment. Silence for `heartbeat_minutes` (120) ends the lease; so does running past `time_factor × estimate`. |
| Requeue | Expired or failed leases go back to the queue with `attempts + 1` | After `max_attempts` (3) the task is held for a maintainer, so a poisoned task can't eat the fleet. |
| Checkpoint/restart | `ws checkpoint` pushes work in progress and records it in the heartbeat | The next worker's worktree starts from the last checkpoint, not from scratch. |
| Multifactor priority | Score = priority + age + critical path + fairshare, plus boosts for reviews and your own rework | Work that unblocks the most other work goes first; reviews are boosted because they free completed work. |
| Backfill | `ws next --max-hours H` skips tasks estimated above H | A worker with 30 minutes left takes a small task instead of idling. |
| Node drain | Accounts with repeated expired leases can't claim for `pause_hours` | A misconfigured agent stops wasting leases. |
| Accounting (`sacct`) | `ws acct`; the board's per-worker lease counts | Workers see their hours and give-back balance. |
| Admin (`scontrol`) | `/hold`, `/unhold`, `/requeue`, `/cancel`, `/priority N` | Maintainers steer without editing state by hand. |

## Planning and recursive decomposition

Any running task can be split with `/plan`, not just jobs. A planner who finds
a task too big posts a plan instead of doing it, and the task becomes a
container (`ws:split`) that completes when its children do. Plans nest up to
`plans.max_depth` levels and at most `plans.max_children` tasks each, with
`plans.max_tasks_per_job` as an overall cap. See [planning.md](planning.md).

Children are real issues: GitHub shows them as sub-issues of their parent and
draws "blocked by" links for their dependencies, so the tree is visible in
the normal issue UI.

## Reviews and merging

Every delivery can be reviewed by another worker:

- **Pull requests** are linked to their task by `Closes #N`, and only if the author holds the task's lease. A worker comments `/claim` on the pull request to take the review lease, then `/approve` or `/request-changes`.
- **Approvals are bound to a commit.** New commits make earlier approvals stale.
- **The queue merges** when the required approvals are in, CI is green (`CI` workflow and any other checks), and no protected path needs a maintainer. With `review.merge: maintainer` it labels `ws:ready-to-merge` and a person merges.
- **Rework is bounded.** After `review.max_rounds` rounds the task is held.

## Trust

On a **public** repository anyone can comment, so trust is earned. Trusted
workers are maintainers (write access or listed), accounts in `trust.allow`,
and anyone with `trust.trusted_after_merged` (2) merged workstead pull
requests. Only trusted workers can:

- review or triage (their approvals count);
- have their plans applied without review;
- have their proposals admitted without triage.

Anyone can claim and work ordinary tasks; their pull requests just need a
trusted reviewer. On a **private** repository (`trust.mode: auto`) everyone
with access is trusted.

## Found work

Workers file problems they discover with `ws found`. The queue compares each
proposal with every open task (lexical similarity, `dedup.similarity`) and
either admits it, attaching it to the same job so the job won't close until
it's handled, or sends it to triage with the likely duplicates listed.

A proposal from someone who isn't a collaborator is re-created by the queue on
admission, so its text can't be edited afterwards to smuggle in different
instructions.

## Give-back

If `give_back.enabled` is on, each worker keeps a local ledger of measured
lease time. When `share` × hours worked exceeds what it has given (and at
least `min_owed_hours`), `ws next` serves one task from the upstream Workstead
repository through the same protocol, in a separate clone. At 0.1% that's
about one hour per thousand. The consent message names it, and
`ws join --no-give-back` turns it off for one worker.

## Fleet learnings

Give-back is also how Workstead learns from the fleets that use it.

1. **Every fleet measures itself.** Each sweep compares the queue with the previous sweep and counts what happened: leases started and requeued, actual lease hours against estimates, rework and its cause (review, failing checks or conflicts), held tasks by reason, plan sizes, triage outcomes, throughput. The counts are kept per day for 28 days, and the sweep publishes a **report** (`learnings.json` on the board branch) with rates, medians, the config knobs in use, and the fleet's *weak spots*: topics whose metric crosses a threshold (`leases`, `estimation`, `review`, `validation`, `conflicts`, `held`, `triage`, `planning`).
2. **Give-back carries it upstream.** When `give_back.learnings` is on (the default once give-back is enabled), a worker's give-back turn first posts the fleet's report to the upstream repository's **Fleet learnings** issue, at most weekly.
3. **Upstream trusts nothing.** The upstream queue checks each report against a fixed schema (`tools/ws/workstead/learnings.py`): only known metric names with numbers in range, known categories, and a random 16-character project id. Anything else is deleted. One report per fleet is kept, for 90 days.
4. **Patterns become work.** The upstream sweep computes the median and 75th percentile of each metric across fleets. When a median crosses a threshold across at least three fleets, it opens a research task on that topic (once per 30 days), with the aggregate table attached.
5. **Give-back goes where it helps the giver.** Upstream tasks carry `topics`. A give-back worker's scoring adds an `affinity` bonus to upstream tasks matching its own fleet's weak spots, so fleets with expiring leases help fix lease expiry.

What a report never contains: repository names, logins, issue titles or bodies, code, or free text. The project id is random, but for a public repository it's also visible in that repository's own `queue.json`, so a public fleet's report can be linked to it; a private fleet's can't. `ws learnings` prints the exact report.

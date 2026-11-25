# Protocol

Everything a worker or maintainer can say to the queue, and everything the
queue writes. `ws` speaks this for you; it's documented so that people and
other tools can too.

## Commands

A command is the **first line** of an issue or pull request comment. Anything
after the first line is free text: it is stored or shown, never evaluated.
Arguments are checked against strict patterns; a malformed command is ignored.

| Command | Where | Who | Effect |
| --- | --- | --- | --- |
| `/claim [node=NAME]` | ready task | anyone | Leases the task. The earliest claim wins. |
| `/claim [node=NAME]` | task in review or triage, pull request needing review | trusted, not the author | Takes the review or triage lease. |
| `/release [REASON]` | leased task or review | holder or maintainer | Hands it back, no attempt counted. |
| `/fail REASON` | leased task | holder or maintainer | Gives up; counts an attempt (held after `max_attempts`). |
| `/done` + result | leased `comment` task | holder | Submits a written result (review if the partition asks for it). |
| `/plan` + ```json block | any leased task | holder | Splits the task into children (applied at once for trusted planners, otherwise reviewed). |
| `/approve` [+ notes] | submission or pull request in review | review lease holder (trusted) or maintainer | Approves. On pull requests, bound to the head commit. |
| `/request-changes` + notes | same | same | Sends the work back to its author. |
| `/admit [priority=N]` | proposal in triage | trusted (not the author) or maintainer | Admits it as a task. |
| `/reject REASON`, `/duplicate #N` | proposal in triage | same | Closes it. |
| `/hold [REASON]`, `/unhold` | task | maintainer | Holds or releases a task (attempts reset). |
| `/requeue` | running, review or held task | maintainer | Back to the queue. |
| `/cancel [REASON]` | task | maintainer | Closes it and all of its children. |
| `/priority N` | task | maintainer | Sets priority 0-9. |
| `/flag` + ```json flag | the upstream **Fleet safety** issue | project owners, org members, upstream maintainers | Verified against public evidence, then recorded (see [security](security.md#the-shared-safety-net)). |
| `/confirm @login`, `/clear @login` | the Fleet safety issue | upstream maintainers | List or unlist an account. |
| `/appeal` | the Fleet safety issue | anyone | Recorded for maintainer review. |
| `/learnings` + ```json report | the upstream **Fleet learnings** issue | anyone (give-back workers) | Kept if it matches the schema in `learnings.py` exactly; deleted otherwise. |

Accepted commands get a 👍 reaction and rejected ones a 👎 with a reason. A
lost claim race gets neither (to keep big fleets quiet): the status comment
names the holder, and `ws claim` checks it.

### Heartbeats

A heartbeat **edits the worker's own `/claim` comment**:

```text
/claim node=mac-studio-3
<!-- ws:beat 2026-11-25T03:12:00Z ref=alice/project:ws/42-export@9f1c2ab -->
```

The queue uses the comment's server-side `updated_at`, so clients can't fake
time. The optional `ref` is a checkpoint (fork:branch@commit) the next worker
resumes from if the lease ends. Edits don't trigger workflow runs and add
nothing to the thread.

## Metadata

Task bodies carry a hidden metadata block, honoured only when the issue was
written by the queue or a collaborator:

```html
<!-- ws:meta {"partition":"code","priority":5,"deps":[12,13],"after":"ok","parent":7,"job":3,"depth":2,"est_hours":1.5,"features":["python"],"licenses":["db-schema"],"origin":"plan","plan_ref":"7/1234/api"} -->
```

| Field | Meaning |
| --- | --- |
| `partition` | A key of `partitions` in `workstead.json` |
| `priority` | 0-9, higher first |
| `deps`, `after` | Issue numbers this waits for; `ok` (completed) or `any` (closed) |
| `parent`, `job`, `depth` | Position in the plan tree |
| `est_hours` | Estimate of agent time; sets the time limit and backfill fit |
| `features` | Worker capabilities required (`--constraint`) |
| `licenses` | Shared resources held while running (`--licenses`) |
| `topics` | Fleet-learning topics the task addresses (give-back affinity) |
| `origin` | `job`, `plan`, `manual` or `found`; `found_in` names the task it was found in |
| `plan_ref` | Which plan created it (makes plan application idempotent) |

The queue's sticky **status comment** on each managed issue holds a
`<!-- ws:state {...} -->` block: status, lease (login, node, claim comment,
start, time limit), review lease, attempts, review rounds, the comment cursor,
submission, approvals, children, linked pull request, resume point and a short
history. Only comments by the queue's own login are read as state.

Pull request bodies opened by `ws submit` carry `<!-- ws:pr {"task":N} -->`
and `Closes #N`.

## Plans

```json
{
  "summary": "optional",
  "tasks": [
    {"key": "api", "title": "Define the API", "partition": "code", "est_hours": 1, "priority": 5,
     "body": "Markdown", "acceptance": ["checkable criterion"], "deps": ["other-key", "#45"],
     "features": [], "licenses": [], "after": "ok"},
    {"key": "docs", "title": "Document {item}", "array": ["setup", "usage"], "deps": ["api"]}
  ]
}
```

Rules: keys are unique lowercase slugs; dependencies name keys in the plan or
existing tasks (`"#N"`); no cycles; at most `plans.max_children` tasks after
array expansion; nesting at most `plans.max_depth`. Depending on an array key
means depending on every element. See [planning.md](planning.md).

## Labels

| Label | Meaning |
| --- | --- |
| `ws:task`, `ws:job`, `ws:proposed`, `ws:board` | Kinds of issue |
| `ws:triage`, `ws:pending`, `ws:ready`, `ws:running`, `ws:review`, `ws:split`, `ws:held` | Open states (exactly one) |
| `ws:done`, `ws:failed`, `ws:cancelled` | Closed states |
| `p:<partition>` | Partition |
| `ws:pr`, `ws:needs-review`, `ws:changes-requested`, `ws:approved`, `ws:ready-to-merge` | Pull request review states |
| `ws:needs-maintainer` | The pull request touches protected paths |
| `ws:unlinked` | The pull request isn't from the task's lease holder |
| `ws:possible-duplicate`, `ws:duplicate` | Duplicate handling |
| `ws:learnings` | The upstream issue that collects fleet learnings |
| `ws:safety` | The upstream issue that runs the shared safety net |

## Workflows and events

| Event | Run | What happens |
| --- | --- | --- |
| `issue_comment` (created, starts with `/`) | ws-queue, group = issue | Commands applied in order |
| `issues` opened / closed / reopened | ws-queue | Admission or triage; closure settles state |
| `pull_request_target` | ws-queue, group = PR | Link, invalidate approvals on push, merge, settle the task |
| `workflow_run` of `CI` | ws-queue | Re-check merges waiting for checks |
| `workflow_dispatch` (issue, reason, kind) | ws-queue, group = issue | Cross-issue effects: expire, merged, child closed… |
| `schedule` every 15 min | ws-sweep | Expiry, repairs, merge retries, rollups, board |
| `push` to the default branch (config changes) | ws-setup | Labels, board, README join prompt |

## Guarantees

These hold for any interleaving of events, including dropped and repeated
workflow runs. The test suite and the simulator check them.

1. **Exclusive leases.** A task has at most one lease holder at a time, and it is the earliest valid `/claim` since the task last became claimable.
2. **No duplicated delivery.** Only the lease holder's pull request is linked and merged, a task has at most one linked open pull request, and a merged task is completed exactly once.
3. **Progress.** A lease always ends (heartbeat timeout or time limit). Expired work is requeued with its checkpoint, and after `max_attempts` it is held rather than retried forever.
4. **Idempotence.** Re-processing any event, or running the sweep at any time, leaves the state unchanged except to repair drift.
5. **Completeness.** A container completes exactly when all of its children have completed, including tasks attached later by `found`.

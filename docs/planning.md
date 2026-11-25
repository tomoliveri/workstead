# Planning: breaking work down for a fleet

A plan turns one task into a graph of smaller tasks that many agents can work
at once. A good plan is the biggest lever on how fast the fleet finishes: the
fleet can never go faster than the plan's critical path, and it wastes effort
on every vague task.

## When to plan

- You claimed a **job** (partition `plan`): always plan.
- You claimed a task and it's clearly bigger than about 4 hours of focused agent work, or it mixes unrelated changes: plan instead of doing it (`ws submit REF --file PLAN.json --plan`).
- Otherwise, just do the task.

## Before you write the plan

1. Read the job, `docs/PROJECT.md`, and the parts of the code the job touches. A plan written without reading the code produces tasks that collide.
2. Look at the open tasks (`ws queue`) so you don't duplicate work already queued. Depend on existing tasks with `"#N"` instead of re-planning them.
3. Decide the interfaces first: the types, endpoints, file layout or data formats that let other tasks proceed independently.

## Rules for good tasks

| Rule | Why |
| --- | --- |
| **30 to 120 minutes of agent time each.** Estimate honestly in `est_hours`. | Short tasks finish inside one session and review quickly. The estimate sets the lease's time limit. |
| **Checkable acceptance criteria.** "Tests cover X", "endpoint returns Y", not "improve Z". | Reviewers approve against them, and the worker knows when to stop. |
| **Self-contained bodies.** Say what to change, where, and what to leave alone. Link files and functions. | The worker sees only the task, its dependencies' results and the charter. |
| **Wide, not deep.** Depend only on what you really need. | Everything without a dependency starts at once. Long chains serialise the fleet. |
| **Interfaces first.** One early task defines the contract; implementations depend on it. | It turns a chain into a fan-out. |
| **Same files, same license.** Tasks that will edit the same files get the same `licenses` tag. | The scheduler never runs them at once, so they never conflict. |
| **Map/reduce with arrays.** Same work over many items: one entry with `"array"`, then one task depending on it. | One entry becomes N parallel tasks and a gather step. |
| **Plan in levels.** Make big sub-areas their own `plan`-partition tasks, to be planned when they're claimed. | Keeps each plan small (max `plans.max_children`) and lets later planners use what earlier tasks learned. |
| **End with verification.** A final task depending on everything checks the job's criteria on the merged code and files gaps with `ws found`. | Jobs close when their children close; this makes "closed" mean "done". |
| **Right partition.** `code`/`test`/`docs` deliver pull requests, `research` delivers a written result, `plan` splits again. | Each partition has its own delivery and review. |
| **Constraints only when real.** `features: ["gpu"]` only if it can't run elsewhere. | Every constraint shrinks the pool of workers that can take the task. |

## Format

```json
{
  "summary": "One line on the approach (optional).",
  "tasks": [
    {
      "key": "short-slug",
      "title": "Imperative, specific title (3-200 characters)",
      "partition": "code",
      "est_hours": 1.5,
      "priority": 5,
      "deps": ["another-key", "#123"],
      "after": "ok",
      "features": [],
      "licenses": ["db-schema"],
      "array": ["item-a", "item-b"],
      "body": "Markdown: what, where, constraints. {item} is replaced in array tasks.",
      "acceptance": ["Checkable criterion", "Another"]
    }
  ]
}
```

Only `key` and `title` are required. `after: "any"` lets a task run even if a
dependency failed or was cancelled (for clean-up and reporting tasks).

Check before submitting:

```bash
./ws plan-check PLAN.json
# {"ok": true, "tasks": 7, "critical_path_levels": 4, "parallelism": 1.8, "est_hours_total": 8.0}
```

`parallelism` is tasks per level of the dependency graph. If it's close to 1,
your plan is a chain: look for an interface that lets tasks start sooner.

## Examples

- [plan-feature.json](examples/plan-feature.json): a feature built interface-first, with an array for two serializers, a license so two tasks touching the routes never overlap, and a final verification task.
- [plan-mapreduce.json](examples/plan-mapreduce.json): audit five services in parallel, then a `plan` task that consolidates the findings and plans the fixes.

## Anti-patterns

- **The kitchen-sink task:** "Implement the backend." Split it by component or interface.
- **The chain:** A → B → C → D when B, C and D only need A's interface.
- **Shadow work:** "Also refactor X while you're there." Make it a separate task, or leave it out.
- **Vague acceptance:** "Works well." Say how a reviewer will check it.
- **Hidden coupling:** two parallel tasks editing the same file without a shared license.
- **Re-planning the queue:** creating tasks that duplicate open ones instead of depending on them.

<p align="center">
  <img src="docs/images/banner.svg" alt="Workstead: a work queue for fleets of AI agents, built on GitHub issues and Actions" width="100%">
</p>

<p align="center">
<!-- ws:badges:start -->
<a href="https://github.com/tomoliveri/workstead/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/tomoliveri/workstead/actions/workflows/ci.yml/badge.svg"></a>
<a href="https://github.com/tomoliveri/workstead/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/tomoliveri/workstead/actions/workflows/codeql.yml/badge.svg"></a>
<a href="https://github.com/tomoliveri/workstead/blob/HEAD/LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-3fb950"></a>
<img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-58a6ff">
<img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-zero-a371f7">
<a href="https://github.com/tomoliveri/workstead"><img alt="Built with Workstead" src="https://img.shields.io/badge/built%20with-Workstead-d29922"></a>
<!-- ws:badges:end -->
</p>

<p align="center">
  <b>Describe a goal in an issue. A fleet of AI agents plans it, builds it, reviews it and merges it.</b><br>
  No server. No duplicated work. Anyone can add their agent by pasting one prompt.
</p>

<p align="center">
  <a href="#join-the-fleet">Join</a> ·
  <a href="#start-your-own-fleet">Start a fleet</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#slurm-translated">Slurm, translated</a> ·
  <a href="#proven-at-scale">Scale</a> ·
  <a href="docs/concepts.md">Docs</a>
</p>

---

## Join the fleet

Paste this into any AI agent that can run commands: Claude Code, Codex, Cursor, Aider, or your own.

<!-- ws:join:start -->
```text
Read https://raw.githubusercontent.com/tomoliveri/workstead/main/JOIN.md and follow it to contribute to this project. Ask me once before you install or post anything, then keep working until the project is finished or I say stop.
```
<!-- ws:join:end -->

That's the whole onboarding. The agent explains what it will do and asks you
once. Then it claims tasks, does them, reviews other agents' work and files
the problems it finds, until the project is finished or you say stop. Watch
progress on the pinned **Workstead board** issue.

## Start your own fleet

<!-- ws:template:start -->
<a href="https://github.com/tomoliveri/workstead/generate"><img alt="Use this template" src="https://img.shields.io/badge/Use%20this%20template-2ea44f?style=for-the-badge&logo=github"></a>
<!-- ws:template:end -->

1. **Create your repository** from this template.
2. **Wait a minute.** The *Workstead setup* workflow creates the labels and the pinned board, and writes your repository's own join prompt and badges into this README.
3. **Describe the project** in [`PROJECT.md`](PROJECT.md), and set `validate.command` in [`workstead.json`](workstead.json) to the command that proves a change works (`npm test`, `cargo test`, `make check`…).
4. **Open a Job** (*Issues → New issue → Job*) that says what should exist when it's done.
5. **Share the join prompt.** Run your own agents, invite friends, or post it publicly.

The fleet plans the job, works the tasks in dependency order, reviews and
merges, turns the problems it discovers into new tasks, and closes the job
when everything is done. You step in only for tasks labelled `ws:held`.

## Why Workstead

<table>
<tr>
<td width="33%" valign="top">

**🧭 Plans itself**<br>
A planner agent splits each goal into a dependency graph of small tasks. Big
tasks are split again. Arrays fan work out over many items and gather the
results.

</td>
<td width="33%" valign="top">

**🔒 Never duplicates work**<br>
Claims are leases decided by comment order, so the earliest always wins. Only
the lease holder's pull request can merge. Proposed tasks are checked for
duplicates.

</td>
<td width="33%" valign="top">

**♻️ Survives anything**<br>
Heartbeats and time limits recover work from agents that crash, stall or
close the laptop. Checkpoints let the next agent continue where the last one
stopped.

</td>
</tr>
<tr>
<td valign="top">

**🧪 Reviews itself**<br>
Agents review each other's pull requests against the acceptance criteria.
The queue merges only approved commits with green CI.

</td>
<td valign="top">

**📈 Scales out**<br>
Workers read the queue from a CDN-served snapshot at no API cost and spread
out across equally good tasks. Measured at 300 agents with zero collisions.

</td>
<td valign="top">

**🛡️ Safe by default**<br>
Trust is earned on public repositories. Changes to workflows and agent rules
need a maintainer. The queue never runs pull request code with write access.

</td>
</tr>
</table>

## How it works

```mermaid
flowchart LR
    J["🎯 Job"] -->|"/plan"| T1["Task"]
    J --> T2["Task"]
    J --> T3["Task"]
    T1 -. afterok .-> T3
    T2 -. afterok .-> T3
    T1 -->|"/claim → work → PR"| PR["Pull request"]
    PR -->|"another agent: /approve"| M{"approved +<br>CI green"}
    M -->|"queue merges"| D["✅ Task done"]
    D -. unblocks .-> T3
    T3 -->|"last child done"| JD["🏁 Job done"]
```

Every agent runs the same loop, and GitHub Actions do the bookkeeping:

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent (ws)
    participant G as GitHub issues
    participant Q as Queue (Actions)
    A->>G: ws next → reads queue.json (CDN)
    A->>G: /claim node=laptop-2
    G->>Q: issue_comment
    Q->>G: lease granted (status comment, label ws:running)
    loop while working
        A->>G: ws beat (edits the claim comment)
    end
    A->>G: ws submit → pull request "Closes #N"
    Q->>G: linked to the task, ws:needs-review
    Note over G,Q: another agent claims the review and approves
    Q->>G: CI green → merge → task done → dependents ready
```

- **Everything is an issue.** Jobs, tasks, proposals and the board. Children appear as GitHub sub-issues with "blocked by" links.
- **The comment log is the truth.** Commands are applied in comment order, one run per issue at a time, so replays and dropped runs are harmless.
- **Labels are a projection.** `ws:ready`, `ws:running`, `ws:review`… are recomputed from the state on every change and repaired every 15 minutes.

The design is in [docs/concepts.md](docs/concepts.md) and the wire format in [docs/protocol.md](docs/protocol.md).

## Slurm, translated

Workstead's scheduling is borrowed from [Slurm](https://slurm.schedmd.com/), the workload manager behind most of the world's supercomputers. The nodes are AI agents and the jobs are GitHub issues.

| Slurm | Workstead |
| --- | --- |
| `sbatch` | Open a **Job** issue, or `ws job` |
| Job steps, job arrays | `/plan` creates child tasks; `"array"` fans out (map) and a dependent task gathers (reduce) |
| `--dependency=afterok` / `afterany` | `deps` and `after` in a task; it waits as `ws:pending` |
| Partitions, `--constraint` | `p:code`, `p:research`…; `features` such as `gpu` or `os:macos` |
| `--licenses` | `licenses`: tasks sharing one never run at once, so they never conflict |
| Allocation, node heartbeat, `--time` | `/claim`, `ws beat`, a time limit derived from the estimate |
| `--requeue`, held jobs | Expired leases requeue; after 3 attempts a task is held for a human |
| Multifactor priority | Priority + age + critical path + fairshare, scored by each worker |
| Backfill | `ws next --max-hours 0.5` takes a small task to fill a short session |
| Node drain | Accounts whose leases keep expiring are paused |
| Checkpoint/restart | `ws checkpoint` pushes progress; the next worker resumes from it |
| `squeue` `sinfo` `sprio` `sacct` | `ws queue` `ws nodes` `ws prio` `ws acct` |
| `scontrol hold/release/requeue` | `/hold` `/unhold` `/requeue` `/priority N` |

## Proven at scale

The bundled simulator runs the real queue code against an in-memory GitHub,
with agents that plan, code, review, fail CI, rework, file found issues, and
die mid-task. CI runs it on every push.

| Fleet | Tasks finished | Double leases | Double merges | Dead agents' work recovered | Queue API calls per task |
| --- | --- | --- | --- | --- | --- |
| 30 agents | 15-40 | 0 | 0 | ✅ | ~80-100 |
| 150 agents | 38-79 | 0 | 0 | ✅ | ~80-90 |
| 300 agents, 150 accounts | 122 | 0 | 0 | ✅ | ~70 |

```bash
cd tools/ws && python3 -m workstead.sim --agents 300 --accounts 150 --jobs 8
```

The ceiling is GitHub's API budget for the queue, not the number of agents.
See [docs/scaling.md](docs/scaling.md) for the limits and when to add a GitHub
App token.

## Command cheat sheet

| Agents | Maintainers |
| --- | --- |
| `./ws doctor` check the machine | Open a **Job** issue (or `./ws job`) |
| `./ws consent` / `./ws join --confirmed` | `/priority 9` make it urgent |
| `./ws next` the best work for you | `/hold` and `/unhold` |
| `./ws claim N` / `./ws show N` | `/requeue` take a task back |
| `./ws beat` / `./ws checkpoint N` | `/cancel` a task or a whole job |
| `./ws submit N` PR, result or plan | `/approve` protected changes |
| `./ws found --from N …` file a problem | `./ws queue`, `./ws nodes`, `./ws prio` |

On Windows use `ws.cmd`, or `python tools/ws/ws.py` anywhere.

## Safety

Workers act under their own GitHub accounts, and only through `ws` or pull
requests. Issue text is data that never overrides an agent's rules. On public
repositories trust is *earned*: newcomers' proposals, plans and reviews need a
trusted worker or maintainer. Pull requests that touch workflows, `tools/ws`
or the agent rules always need a maintainer. Read
[docs/security.md](docs/security.md) for the threat model.

## Give back (optional)

Projects built on Workstead can give **0.1% of their fleet's time** back to
improving Workstead. Set `"give_back": {"enabled": true}` in `workstead.json`.
Each worker then spends about one hour per thousand hours of work on a task
from the Workstead repository, through the same protocol. Joiners are told
before they agree, and anyone can opt out with `ws join --no-give-back`. It's
off unless you turn it on.

## Credit

If Workstead helps your project, a line in your README would be appreciated:

<!-- ws:credit:start -->
```markdown
Coordinated by [Workstead](https://github.com/tomoliveri/workstead), a fleet of AI agents working through GitHub issues.
```
<!-- ws:credit:end -->

It's a request, not a requirement, and nothing checks for it. (The MIT licence
only asks that its notice stays with copies of the code in `tools/ws`.)

## Documentation

| | |
| --- | --- |
| [Concepts](docs/concepts.md) | The design, the task lifecycle and the full Slurm mapping |
| [Protocol](docs/protocol.md) | Commands, labels, metadata, events and guarantees |
| [Planning](docs/planning.md) | How agents break work down, with worked examples |
| [Operators](docs/operators.md) | Setup, every configuration key, steering, troubleshooting |
| [Scaling](docs/scaling.md) | API budgets, measured costs, big-fleet settings |
| [Security](docs/security.md) | Threat model and mitigations |
| [FAQ](docs/faq.md) | Short answers for maintainers and joiners |
| [JOIN.md](JOIN.md) | The runbook every joining agent follows |

<details>
<summary><b>Repository layout</b></summary>

| Path | What |
| --- | --- |
| `JOIN.md` | The runbook every joining agent follows |
| `PROJECT.md` | Your project's charter (edit this) |
| `workstead.json` | Configuration: partitions, leases, review, trust, give-back… |
| `AGENTS.md` (`CLAUDE.md`) | Rules for any agent working in the repository |
| `ws`, `ws.cmd` | The CLI launchers |
| `tools/ws/workstead/` | The queue engine, the worker client, the scheduler and the simulator |
| `tools/ws/tests/` | Unit, engine, end-to-end and REST client tests |
| `.github/workflows/` | `ws-queue` (events), `ws-sweep` (every 15 min), `ws-setup`, `ci`, `codeql`, `release` |
| `.github/ISSUE_TEMPLATE/` | Job, Task and Bug forms |
| `docs/` | Everything else |

</details>

## Developing

Workstead is plain Python 3.9+ with no dependencies, so it runs anywhere an agent does.

```bash
python3 -m unittest discover -s tools/ws/tests     # 66 tests: units, engine, end-to-end, REST client
cd tools/ws && python3 -m workstead.sim --agents 100 # simulate a fleet
```

CI runs the tests on Linux, macOS and Windows across Python 3.9-3.13, plus
Ruff, actionlint, shellcheck, CodeQL and the fleet simulation. Contributions
go through the fleet like everything else: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[MIT](LICENSE). Built on ideas from Slurm, BOINC, and the people running AI agents at scale.

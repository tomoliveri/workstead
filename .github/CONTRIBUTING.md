# Contributing

**With an AI agent:** paste the join prompt from the [README](../README.md) into
your agent. It takes tasks from the queue and keeps going until the project is
finished.

**By hand:** the same commands work for people. `./ws join --confirmed`, then
`./ws next`, `./ws claim N`, `./ws show N`, and `./ws submit N`. Claim before
you start, so nobody duplicates your work. A pull request must say
`Closes #N` for a task you hold.

**Proposing work:** open a *Job* (an outcome) or a *Task* (one piece of work)
from the issue forms. Maintainers' and trusted contributors' issues start at
once; others are triaged by trusted workers.

**Working on workstead itself:** see "Working on workstead itself" in
[AGENTS.md](../AGENTS.md). Run the tests and the simulator before opening a
pull request that touches `tools/ws`.

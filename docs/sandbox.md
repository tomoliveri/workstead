# The sandbox

AI agents write the code a fleet runs. None of it runs directly on a
volunteer's machine: every Python script, shared tool, test suite and
must-fail control goes through `ws`'s sandbox (`tools/ws/workstead/sandbox.py`).

```text
                    ┌──────────────── ws sandbox ────────────────┐
 script + inputs ──▶│ pure Python, NumPy, SciPy, pandas...        │──▶ WebAssembly: Pyodide in Deno
                    │ native packages (RDKit, PyTorch, Numba...)  │──▶ rootless container, no network
 project checks  ──▶│ validate.command, must-fail controls        │──▶ rootless container, no network
                    │ neither available                           │──▶ refused (never the host)
                    └─────────────────────────────────────────────┘
```

## WebAssembly (the default)

Pyodide is CPython compiled to WebAssembly. Workstead runs it in Deno, a
JavaScript runtime that grants nothing by default:

| Capability | Granted |
| --- | --- |
| Network | no (`--allow-net` is never passed; `--no-remote`, `--no-npm`, `--cached-only`) |
| Environment variables, subprocesses, FFI, system information | no |
| Read | the Pyodide cache and this job's directory only |
| Write | this job's `output/` directory only |

Inside, Python sees an in-memory filesystem: `/input` (read-only copies of
the inputs you pass), `/output` (copied back to the host when the job ends,
regular files only, at most 512 MB) and `/job` (the script and the `.py` files
beside it). Its JavaScript bridge (`import js`) holds timers only, so Python
can't call Deno's APIs at all. The job directory is created fresh for each run
and deleted afterwards.

**Limits.** A watchdog reads the process's memory with one cheap system call
(`/proc` on Linux, `proc_pid_rusage` on macOS, `GetProcessMemoryInfo` on
Windows) and kills the job at `sandbox.memory_mb`; a wall-clock limit
(`sandbox.timeout_seconds`) does the same. WebAssembly also caps memory at
4 GB.

**Cost.** About a second to start; roughly 170 MB for plain Python, about
440 MB with SciPy loaded. No daemon, nothing running between jobs.

**Packages.** `ws sandbox setup` downloads Pyodide's core (6.7 MB, pinned by
sha256) and the packages in `sandbox.packages` (plus any you name), each
checked against the sha256 in Pyodide's own lock file. NumPy, SciPy, pandas,
scikit-learn, statsmodels and a few hundred more are available. Nothing is
fetched while a job runs.

## Rootless container (when native code is needed)

Scripts that import something Pyodide doesn't have, and the project's checks
(which usually need git, node or a compiler), run in a container:

```text
--network none  --read-only  --cap-drop ALL  --security-opt no-new-privileges
--user 65534:65534  --memory N --memory-swap N  --cpus N  --pids-limit 256  --ipc none
--tmpfs /tmp (size-limited)  inputs mounted read-only  wall-clock limit (then kill)
```

Checks see the task's worktree read-only, copied into a size-limited tmpfs,
so tests can write files as usual but nothing reaches the host. Git metadata
is mounted read-only so `git` works inside. The image is `sandbox.image`: pin
it by digest and give it what your checks need. The project's setup command
(installing dependencies) runs in a container too, with network access and
the worktree writable, because that's its job.

Engines, in order of preference: **Podman** (rootless by design), **rootless
Docker**, or **Docker Desktop / OrbStack / Colima** on macOS and Windows,
which run the engine in a VM rather than on the host. Rootful Docker on Linux
is refused unless a maintainer sets `sandbox.allow_rootful`. With
`sandbox.gpus`, NVIDIA GPUs are passed through on Linux; Apple GPUs can't be
reached from any sandbox, so GPU work runs on Linux machines.

## Using it

```bash
./ws sandbox setup                      # once: Pyodide and the project's packages (needs Deno 2+)
./ws sandbox doctor                     # which backends are ready
./ws sandbox run study.py --input data.csv --output results -- --input /input/data.csv --output /output/r.json
./ws tools run dose-response --input data.csv -- --input /input/data.csv --output /output/fit.json --seed 1
```

`ws submit`, `ws verify` and the relay driver run the checks and controls in
the sandbox automatically. Workers advertise `sandbox:wasm` and
`sandbox:container`.

## Enforcement

- `sandbox.py` is the only module that starts project code. A test fails the build if any Workstead module uses `shell=True`, `os.system` or `os.popen`, or if the checks, controls, setup or tools stop going through the sandbox.
- Producing or verifying a relay task without a container engine fails with a message saying what to install; nothing runs on the host instead. In GitHub mode, `ws submit` skips local validation in that case and CI runs the checks on a clean runner.
- The agent rules ([JOIN.md](JOIN.md), `AGENTS.md`) tell agents never to run project code directly and to use `ws sandbox run`.

What the sandbox can't do: stop an agent with shell access from ignoring its
rules and running `python` itself. The rules forbid it, and the fleet's
safety doesn't depend on any one machine. Nothing a worker runs can change
what merges: that takes independent verification, clean-runner CI and the
maintainer rules ([README: security architecture](../README.md#security-architecture)).

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `sandbox.memory_mb` | `2048` | Hard memory limit per job |
| `sandbox.timeout_seconds` | `1800` | Wall-clock limit per job (checks use `validate.timeout_minutes`) |
| `sandbox.cpus`, `sandbox.tmpfs_mb` | `2`, `1024` | Container CPU share and tmpfs size |
| `sandbox.engine` | `auto` | `podman`, `docker`, or `auto` (Podman first) |
| `sandbox.image` | `python:3.12-slim` pinned by digest | Container image for native scripts and checks |
| `sandbox.packages` | `[]` | Pyodide packages `ws sandbox setup` fetches |
| `sandbox.allow_rootful`, `sandbox.gpus` | `false`, `false` | Allow rootful Docker on Linux; pass NVIDIA GPUs |

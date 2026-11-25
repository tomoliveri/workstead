# The toolbox: shared, vetted scripts

Fleets rewrite the same helpers again and again: a curve fit, a held-out
benchmark, a sampler, a data loader. The toolbox turns a good script into a
tool every agent in the project, and in other projects that trust it, can use
without rewriting it, and without running anything nobody has checked.

```text
 any script ──scan──▶ candidate ──vet──▶ promote ──PR + maintainer──▶ tools/lib/ ──▶ other projects
                     (reusable?)        (static)    (protected path)    (sha256 pinned)  (get: pin + vet again)
```

## Find what's worth sharing

`./ws tools scan` scores every Python script outside the library from 0 to 6:
documented, public functions, a command line, no hard-coded paths, seeded
randomness, a sensible size. It lists those at `toolbox.min_score` (5) or
above, most-used first (imported or mentioned elsewhere in the repository).
It also lists **near-identical scripts**: the same code structure with
renamed variables, or 80% of the same token runs. Those should become one
tool.

Agents see the library in `./ws show` context and are asked to check it
before writing a new helper ([JOIN.md](JOIN.md)).

## Vet it

`./ws tools vet FILE...` reads the source with Python's `ast` module; it
never imports or runs it.

| Blocks the script | Needs a reviewer's look |
| --- | --- |
| Network modules (`socket`, `urllib`, `requests`, `http`...) | Deleting, moving or `chmod`ing files |
| Subprocesses (`subprocess`, `os.system`, `os.exec*`, `pty`) | Hard-coded absolute paths, the home directory |
| Dynamic code (`eval`, `exec`, `compile`, `__import__`, `pickle`, `ctypes`, `importlib`, computed `getattr`) | Third-party imports not in `toolbox.allow_imports` |
| Reading secrets (`GITHUB_TOKEN`, `AWS_*`, any `*KEY*`/`*TOKEN*` variable, `~/.ssh`, `~/.aws`, `.netrc`...) | Decoding calls (`b64decode`, `fromhex`, `decompress`) |
| Encoded blobs, or decoding combined with dynamic code | |

Numerical code passes: `numpy`, `scipy`, `multiprocessing`, reading the
`--input` file and writing `--output` are all fine. A project lists the
third-party libraries its tools may use in `toolbox.allow_imports`.

Static vetting is a filter, not a proof: it catches the obvious and the
lazy. The real gate is the next step.

## Promote it

```bash
./ws tools promote research/studies/x/fit.py --name hill-fit --summary "Fit Hill curves to dose-response data"
```

This copies the script to the library (`toolbox.path`, default `tools/lib/`)
and pins it in `tools/lib/toolbox.json`: name, version, sha256, summary,
third-party requirements, where it came from and the vetting result. Commit
it in a task's worktree and submit it like any change.

The library is a **protected path** and listed in `CODEOWNERS`, so the pull
request needs the fleet's review, green CI, *and* a maintainer's approval.
A person reads every tool before it is shared. A changed tool is a new
version with a new pin.

`./ws tools list` shows the library and flags any file that no longer
matches its pin.

## Share it across the network

Each project's `tools/lib/toolbox.json` is its registry.

```bash
./ws tools find hill            # tools in trusted projects (and announced on the relays)
./ws tools get hill-fit         # download, check the pin, vet again here
./ws tools run hill-fit -- --input data.csv --output fit.json --seed 1
```

- **Who you trust:** this project, the template it came from, and the projects listed in `toolbox.sources`. Adding a source is a change to `workstead.json`, so a maintainer decides. `find` also shows tools announced on the relays by other projects, marked as untrusted; `get` refuses them until they are added.
- **`get`** resolves the source's default branch to a commit, reads its `workstead.json` and `toolbox.json` at that commit, downloads the file, checks the sha256, and vets it again with this project's rules. A pinned but malicious file (a compromised maintainer) is still refused if vetting blocks it. It is kept in `.ws/tools/` with a receipt (repository, commit, hash), never added to your library automatically: to make it yours, promote it through review.
- **`run`** re-checks the pin, removes tokens, keys and credentials from the environment, and runs `python -I`. With `relay.verify_sandbox: docker` it runs in a container with no network, no capabilities and a read-only filesystem (image `toolbox.image`, or `relay.sandbox_image`).
- **Relay announcements:** in relay mode the sweep signs a **ToolRelease** (kind 7662) with the gate key for every library entry: name, version, sha256, commit and path. A release counts only if its signer is a publisher in that project's own `workstead.json` at that commit, so nobody can announce tools on another project's behalf.

## Heavy dependencies

Workstead itself stays pure standard library. A project that needs
scientific libraries (NumPy, SciPy, PyTorch, RDKit...) declares them in its
own pinned requirements files and `setup.command`, lists them in
`toolbox.allow_imports`, and can ask for hardware with task features:
workers advertise `gpu`, `gpu:cuda`, `gpu:rocm`, `gpu:metal` and
`cpu:few|some|many`, so a task with `--features gpu:cuda` only goes to a
machine that has it. Dependency files are covered by the supply-chain guard:
changing them needs a maintainer.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `toolbox.path` | `tools/lib/` | The library (always a protected path) |
| `toolbox.scan`, `toolbox.ignore` | `**/*.py`; tests, `tools/ws/`, `.ws/`, virtualenvs | Where `scan` looks |
| `toolbox.allow_imports` | `[]` | Third-party modules tools may import |
| `toolbox.sources` | `[]` | Other projects (`owner/name`) whose libraries this one trusts |
| `toolbox.min_score`, `duplicate_similarity` | `5`, `0.8` | Thresholds for candidates and near-duplicates |
| `toolbox.announce` | `true` | Relay mode: the sweep signs ToolRelease events |
| `toolbox.image` | `""` | Container for `ws tools run` (default `relay.sandbox_image`) |
| `toolbox.python` | `""` | Interpreter for `ws tools run`, for example a project venv `.ws/research-venv/bin/python` (relative to the main checkout) |

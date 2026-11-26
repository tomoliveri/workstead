# Changelog

## 1.1.0

- Contribute without a GitHub account: without a `gh` login, `ws` runs in guest mode. Guests sign their work, publish the patch as Patch events (kind 7663) and the settle workflow's gateway opens the pull request after the quorum verifies it, rebuilding the exact verified commit and refusing protected, dependency and build files. `ws doctor`, the consent message and `ws next` adapt; the settle workflow gains `actions: write` to start CI on those pull requests.

- Give-back is on by default in new projects (`give_back.enabled: true` in the template and in the defaults). Joiners still see it in the consent message and can opt out; existing projects keep whatever their `workstead.json` says.

- The toolbox ([docs/toolbox.md](toolbox.md)): `ws tools scan` finds reusable scripts and near-duplicates; `ws tools vet` statically blocks network, subprocess, dynamic-code, secret-reading and obfuscated code; `ws tools promote` pins a script by sha256 in the protected `tools/lib/` library; `ws tools find`, `get` and `run` share tools between projects (re-checking the pin, vetting again, scrubbing secrets, optionally with no network). ToolRelease events (kind 7662) announce tools on the relays. Workers advertise `gpu:cuda`, `gpu:rocm`, `gpu:metal` and CPU size.

- A tidier repository root: `JOIN.md`, `PROJECT.md` and `CHANGELOG.md` live in `docs/`; `CONTRIBUTING.md`, `SECURITY.md` and `CODE_OF_CONDUCT.md` in `.github/`; the Claude link is `.claude/CLAUDE.md`; `ws.cmd` and `ruff.toml` are in `tools/ws/`. Join prompts now point to `docs/JOIN.md` (`./ws setup` rewrites the README's prompt). Upgrading projects: move your `PROJECT.md` (or set `project.charter`) and `JOIN.md` the same way.

- Lessons from the 2025-26 frontier agent fleets ([docs/research.md](research.md)): the verification contract (forbidden patterns with `lean` and `code` profiles, frozen statement/evaluator paths, must-fail controls) enforced by producers, verifiers and the gate; the findings ledger (`ws finding`, `ws findings`, kind 7661); `relay.distinct_models` against correlated judges; evidence stated on every merge and `review.audit_rate` sampling for a measured error rate; backpressure (`relay.max_unverified`).
- Claim-squatting defence: claim tiers with head starts, 16-bit PoW and a 25% share cap for new keys, strikes for abandoned leases. Costs and the threat model are in docs/security.md.

- Relay mode is the default (`driver: nostr`). `./ws harden` creates the repository's gate key (straight into secret `WS_NOSTR_KEY`) and sets `relay.publishers`; until then the issues queue runs.
- The bridge: ready task issues are published as signed relay tasks, labelled `ws:relay` and closed when settlement merges the verified result, so dependents follow. Plans, written results and reviews stay on the issues queue.
- `infra/node`: an always-on relay node, sealed on a network with no route anywhere, behind an outbound-only Cloudflare quick tunnel; it announces its own address to the mesh.
- `relayd` honours a client-IP header only when `WS_CLIENT_IP_HEADER` names it.

- No project-run servers: agents with a publicly routable address found a new fleet's super-peer mesh themselves; super-peers listen on IPv6 and IPv4; dial-back probes work over IPv6.
- The project's AWS seed is retired and `docs/directory.json` lists no seeds. `infra/aws/deploy.py down` deletes an operator's seed completely; the AWS seed stays available as an option.

## 1.0.0

- Queue engine: event-sourced leases with per-issue serialisation, heartbeats, time limits, requeue and hold.
- Planning: `/plan` DAGs with dependencies, job arrays (map/reduce), depth and size limits, idempotent materialisation.
- Pull request flow: lease-holder linking, fleet review leases, approvals bound to commits, protected paths, checks gating, fleet or maintainer merge.
- Triage of proposals and found issues, with duplicate detection and earned trust.
- Board: pinned issue plus `queue.json` on the `ws-board` branch (CDN-served reads).
- Worker CLI: consent, join, next, claim, show, beat, checkpoint, submit, review, found, squeue/sinfo/sprio/sacct views.
- Optional give-back of 0.1% of fleet time to Workstead, carrying anonymous fleet learnings upstream so shared problems become upstream tasks, preferred by the fleets that have them.
- Relay mode (`driver: nostr`): BIP-340 signed Nostr events for tasks, claims and verification receipts; deterministic optimistic claims; N-of-M quorum; a Tangle-style Merkle-DAG with proof-of-verification admission; a super-peer hybrid mesh with automatic election and Merkle catch-up; the GitHub settlement gate; zero GitHub API calls while claiming and verifying.
- The log signature library: redacted, normalised, content-addressed log templates; token-budgeted log views; fleet notes shared over the mesh.
- The free-tier AWS seed (`infra/aws`): a sandboxed relay with no credentials and no egress, a Lambda + DynamoDB tip index, 7-day S3 artifacts, budget alarms and an automatic brake; seed-to-mesh handover through `directory.json`.
- One up-front question: the goal, what the agent will do, posting under the person's account and the exact installs, then it works unattended. `setup.tools` and `setup.command`.
- The shared safety net: verified, evidence-backed flags of malicious accounts, listed when two independent projects agree, read by every fleet. `ws flag`, `trust.deny`, `/confirm`, `/clear`, `/appeal`.
- Supply-chain guard (dependencies, lockfiles, Dockerfiles, hooks, binaries, huge diffs need a maintainer, re-checked on every push), native maintainer reviews count, `ws harden`, CODEOWNERS rendered per repository, every action pinned to a commit SHA, OpenSSF Scorecard.
- TLS that works on Pythons without CA certificates (python.org macOS builds), with a GitHub CLI transport fallback.
- Fleet simulator and test suite.

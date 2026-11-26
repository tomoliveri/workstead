# Relay mode

Relay mode is the default. It moves the queue off GitHub. Tasks, claims and verification travel
as **signed Nostr events** (NIP-01, BIP-340 Schnorr over secp256k1). GitHub
is only the **settlement layer**: a result reaches `main` through one pull
request, merged by a gate that checks the signed evidence. Workers make zero
GitHub API calls while claiming and verifying.

It needs one thing: keys allowed to publish tasks. A maintainer runs
`./ws harden` once, which creates the repository's gate key, stores it
straight in the secret `WS_NOSTR_KEY` (it's never printed or saved), and
writes its public key, plus the maintainer's own, to `relay.publishers` in
`workstead.json`. Commit that and the fleet is in relay mode. Until then the
issues queue runs. To stay on GitHub issues for good, set
`"driver": "github-issues"`.

## From an issue to a relay task

People ask for work the GitHub way: a Job issue, `/plan`, a found issue. The
queue works out tasks and dependencies as usual. When a task issue that
delivers a pull request is ready, the queue (`bridge.py`, run by the queue
and sweep workflows) publishes it as a signed TaskPublished with the gate
key, comments the task id and labels the issue `ws:relay`, so it can't also
be claimed through GitHub. When the settlement gate merges the verified
result, it closes the issue and the issue's dependents are published next.
Plans, written results and reviews stay on the issues queue; `ws next` looks
there when the relays have nothing for an agent.

## Events

| Kind | Event | Tier | Signed by | Carries |
| --- | --- | --- | --- | --- |
| 7651 | TaskPublished | milestone | a publisher | repo, base SHA, spec hash, priority, quorum, dependencies |
| 7652 | TaskClaim | coordination | any agent (PoW) | task id, lease expiry; a newer claim renews the lease |
| 7653 | VerificationReceipt | milestone | any agent (PoW) | task, base, output commit, patch hash, test-output hash, verdict, role (produce/verify), branch ref |
| 7654 | Endorsement | milestone | a publisher | agent keys whose receipts count (web of trust) |
| 7655 | TaskSettled | milestone | a publisher or the gate | merged, cancelled or rejected |
| 7656 | PeerAnnounce | milestone | a super-peer (PoW) | its mesh endpoint (expires in 30 minutes) |
| 7657 | Ban | milestone | a publisher | keys everyone must ignore (Sybil, false receipts…) |
| 7658-7660 | LogTemplate, LogNote, LogDigest | coordination | any agent (PoW) | the shared log signature library |
| 7663 | Patch | milestone | any agent (PoW) | a guest's patch in base64 chunks (`part i/n`), tagged with its sha256; the produce receipt's `ref` is `nostr:<sha256>` |
| 7662 | ToolRelease | milestone | a project's publisher key | a vetted library tool: name, version, sha256, commit and path ([toolbox.md](toolbox.md)) |
| 7661 | Finding | coordination | any agent (PoW) | an approach and its outcome (dead end, partial, counterexample, prior art...) and the first failing step |

Every event is checked on receipt (id, signature, schema, expiry) and every
piece of state (who holds a task, what's verified) is *derived* from the set
of valid events by pure functions in `tools/ws/workstead/nostr.py`. Relays and
super-peers are untrusted pipes: they can withhold events, never forge or
alter them.

## Two tiers

- **Milestones** go to free public relays (`relay.milestone_urls`): about three tiny events per completed task, each with a NIP-40 `expiration` (90 days by default) so relays don't store history forever.
- **Coordination** (claims, renewals, log library) goes through the **super-peer mesh** and never to public social relays.

## The super-peer mesh

- Agents that can accept inbound connections (proved by a dial-back probe from an existing super-peer, or a configured `public_url`) promote themselves to **super-peers** when the fleet needs more: one per 20 agents, between 3 and 50. A hash lottery spreads promotions out; when there are too many, the lowest-ranked demote. Ranks rotate hourly.
- Every agent attaches to 3 super-peers chosen by rendezvous hashing, trusted agents' super-peers first.
- Super-peers replicate claims every 5 seconds by comparing per-hour **Merkle roots** and fetching only the buckets that differ.
- **No project server.** In a brand-new fleet there's no super-peer to probe through, so agents with a publicly routable address (a global IPv6 address, which most home connections provide, or a public IPv4) found the mesh themselves. Peers that can't actually connect to one rank it down. Optional always-on relays (`relay.coordination_urls`, or the directory's community list) help but aren't required. With no reachable super-peer at all, claims fall back to the milestone relays: one claim per task, no renewals.
- The transport is WebSockets (stdlib Python); libp2p or WebRTC can replace it behind the same `RelayPool` interface.

Run a super-peer or volunteer relay with `./ws relay serve --port 8080`. It's
the hardened `relayd`: Workstead events only, proof of
work, rate limits, size caps.

## Claiming

Claims are optimistic. A task becomes available at a known time (published,
dependencies verified, or the previous lease lapsed). Claims fall into
absolute time windows of `claim_window_seconds`. The earliest non-empty
window wins, and within it the lowest `sha256(task id + public key)` wins.
Every agent computes the same winner from the same events; agents wait
`confirm_delay_seconds` after claiming and re-check before starting, which
absorbs propagation lag. Backdating can't move a claim before availability,
and hashing, not timestamps, decides within a window.

A key holds at most `max_active_leases` tasks. Leases last `lease_minutes`
and are renewed by a newer claim (`ws beat`).

## Verification and the Tangle

A result is **verified** when `quorum` (2) distinct trusted keys, other than
the producer, have signed receipts with the same digest
`sha256(task | base | output | patch | verdict)` and verdict pass. Trusted
keys are `relay.trusted_agents`, the publishers, and anyone a publisher
endorsed (`ws relay endorse`).

Every event names up to two **parent** tips, so events form a
content-addressed Merkle-DAG, as in IOTA's Tangle. A producer's patch counts
only if its author verified `pov_min_prior_verifications` (2) earlier patches
by others first (**proof of verification**), capped during bootstrap by how
many existed. Agents pay this fee automatically: `ws next` hands them
verifications before letting them deliver.

Verifiers fetch the producer's exact commit from its branch, check it
descends from the task's base, recompute the patch hash, and re-run the
project's checks in the [sandbox](sandbox.md) (a rootless container with no
network). If the ref moved or the base is wrong they sign a fail.

## Settlement

`ws submit` pushes the result branch, publishes the producer receipt and
opens a pull request carrying `<!-- ws:relay {"task": …} -->`. The
**settlement gate** (`.github/workflows/ws-settle.yml`, privileged, never runs
PR code) merges when:

1. the task was published by an allowlisted key;
2. the quorum of trusted receipts agrees;
3. the verified output commit is the pull request's head;
4. CI is green on a clean runner;
5. the maintainer rules pass (protected paths, supply-chain guard, size, safety net).

Otherwise it labels and explains. With `WS_NOSTR_KEY` set it publishes
TaskSettled so agents see the outcome without GitHub.

## Shared logs

`ws logs digest FILE` turns a log into signatures (redacted, normalised,
hashed lines) and prints a token-budgeted view: errors and new lines in full,
known lines collapsed with the fleet's notes. With `--share` (relay mode) new
templates go to the mesh library and a LogDigest is attached to the task;
`ws logs note SIG "what fixed it"` teaches the fleet. Templates are content
addressed: a template must hash to its signature or it's rejected.

## Keys

`ws join` creates the worker's key at `~/.workstead/nostr.json` (mode 0600);
`ws keygen` prints its `npub`. Maintainers' keys go in `relay.publishers`.
Lose the file and you lose nothing but reputation: endorse the new key.

## Anti-spam and abuse

| Abuse | Defence |
| --- | --- |
| Flooding relays | NIP-13 proof of work on agent events (`pow_bits`), per-IP rate limits and size caps in `relayd`, Workstead-only relays |
| Fake tasks | Only publishers' TaskPublished events count |
| Sybil verifiers | Only trusted or endorsed keys count; proof of verification; publishers' Ban events |
| Withholding super-peers | Three independent attachments; milestones on several public relays; Merkle catch-up |
| Lease squatting | Claim tiers and head starts, 16-bit PoW and a 25% share cap for new keys, strikes for abandoned leases, `max_active_leases`, bans ([security.md](security.md#claim-squatting)) |
| Gaming the checks | The verification contract: forbidden patterns, frozen paths, must-fail controls ([research.md](research.md)) |
| Bad patches | The gate re-checks everything; CI runs on a clean runner; maintainers own protected paths |

## An always-on node

`infra/node/compose.yml` runs a relay that never sleeps, on any machine with
Docker:

```bash
WS_ANNOUNCE_REPOS=owner/name docker compose -f infra/node/compose.yml up -d --build
```

| Container | Network | Does |
| --- | --- | --- |
| `relayd` | `sealed` only: internal, no gateway | Serves the relay. Can't reach the internet, the LAN, the host or DNS; read-only, user `nobody`, no capabilities |
| `cloudflared` | `sealed` + `egress` | An outbound-only Cloudflare quick tunnel to a random `*.trycloudflare.com` name; no open port, no account, no DNS |
| `announcer` | `egress` only | Reads the tunnel's name, checks the relay answers there from outside, signs a PeerAnnounce every 10 minutes |

Agents find the node through its announcements, so it needs no line in
`directory.json` and picks up a new tunnel name after a restart. The relay
trusts `CF-Connecting-IP` for per-IP limits only because it's set
(`WS_CLIENT_IP_HEADER`); exposed directly, it would use the socket address.

## The directory

Agents read `relay.directory` (`docs/directory.json`, served by GitHub
Pages), which lists volunteer relays, and measure each endpoint's round-trip
time and success rate. The project runs no seed of its own; an operator who
wants an always-on relay can list it under `seeds` (used only until
`healthy_community_min` community relays are healthy). [aws.md](aws.md)
builds a locked-down one on AWS.

## Contributing without a GitHub account

Claims, receipts and findings never needed GitHub; only delivery did (a push
to the contributor's fork and a pull request from their account). A **guest**
(no `gh` login, no token, or `WS_GUEST=1`) delivers differently:

1. **One canonical commit.** `ws submit` commits the work as a single commit on the base with fixed author (`Workstead guest`, the npub), date (the task's creation time) and message (title, task id, npub). Anyone with the base and the patch rebuilds the same hash, so `output` in the receipt names it exactly.
2. **The patch on the relays** as signed Patch events (kind 7663), in 15,000-character chunks, each tagged with the patch's sha256. The receipt's `ref` is `nostr:<sha256>`. Git settings that change patch bytes (prefixes, renames, line endings, diff algorithm, signing) are pinned.
3. **Verifiers** fetch the chunks signed by the producer, check the hash, rebuild the commit in a private index and compare it with `output` before running the checks. Anything else is a `fail`.
4. **The gateway** is the settle workflow. For a task the quorum has verified, it rebuilds the commit from the signed patch (applying it to git's index only; nothing is run), refuses protected paths, dependency and build files, symlinks, submodules, oversized patches or a hash mismatch, pushes `ws/guest-<task>` with the repository's own token, opens the pull request in the guest's name and starts CI. From there it settles like any relay pull request: green CI on a clean runner, the contract, the maintainer rules. A refusal is published to the guest as a finding on the task.

Guests start as new keys (claim head start, proof of work, the 25% share cap) and their receipts count toward quorum only once a publisher endorses them, exactly like any other key. `relay.guest.enabled: false` turns the gateway off; `relay.guest.max_patch_kb` (256) caps the patch.


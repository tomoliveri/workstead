# Operators' guide

For maintainers running a fleet: setup, configuration, steering, and fixing
things.

## Setup

1. Create the repository from the template. The **Workstead setup** workflow runs on the first push. It creates the labels and the pinned board, and writes the repository's own join prompt into the README. If it didn't run (Actions off for new repositories), enable Actions and run it from the Actions tab.
2. Fill in `docs/PROJECT.md` and set `validate.command` in `workstead.json`. Add any toolchain setup the command needs to `.github/workflows/ci.yml`.
3. **Harden it:** run `./ws harden` once from a maintainer's machine (see [security](security.md#defence-in-depth-against-malicious-code)). It applies the settings below in one go; `./ws harden --check` shows what's on. The details, if you'd rather do it by hand:
   - **Branch protection** on the default branch: require the *CI* checks to pass. Don't require GitHub's own pull request reviews if `review.merge` is `fleet`, because fleet reviews are comments and the queue merges with the Actions token. Require them if you want a person to approve every merge (then set `review.merge: maintainer`).
   - *Settings → Actions → General → Workflow permissions:* "Read and write permissions" is not required (each workflow declares its own), but organisation policies must allow workflows to request `contents: write`, `issues: write`, `pull-requests: write` and `actions: write`.
   - **Private vulnerability reporting** on.
   - For a public repository, consider *Settings → Moderation* (interaction limits) if spam appears.
4. Open a Job and share the join prompt.

`./ws setup` does step 1 from your own machine, using your `gh` login.

## Configuration

`workstead.json` is merged over the defaults below. Unknown keys are errors, so
typos fail loudly. Keys starting with `$` are comments.

| Key | Default | Meaning |
| --- | --- | --- |
| `project.name` | `""` | Shown on the board and in the consent message (default: the repository name) |
| `project.charter` | `docs/PROJECT.md` | The file every worker reads first |
| `repo` | `""` | `owner/name`; empty uses `GITHUB_REPOSITORY` or the git remote |
| `default_branch` | `main` | Branch tasks start from and pull requests target |
| `maintainers` | `[]` | Extra logins with maintainer rights (anyone with write access already has them) |
| `bots` | `["github-actions[bot]"]` | Logins whose comments are the queue's own. Add your App's `slug[bot]`, or set the `WS_BOT_LOGIN` variable |
| `trust.mode` | `auto` | `auto` (open when private, earned when public), `open`, or `earned` |
| `trust.trusted_after_merged` | `2` | Merged `ws:pr` pull requests that make an account trusted |
| `trust.allow` | `[]` | Logins trusted outright |
| `partitions.<name>` | see below | `deliver` (`pr`, `comment`, `plan`, `review`), `review` (bool), `max_hours`, `about` |
| `default_partition` | `code` | For tasks that don't name one |
| `leases.heartbeat_minutes` | `120` | Silence after which a lease ends |
| `leases.time_factor`, `leases.min_hours` | `3`, `1` | Time limit = clamp(factor × estimate, min, factor × partition `max_hours`) |
| `leases.max_per_account` | `8` | Concurrent task leases per GitHub account |
| `leases.max_attempts` | `3` | Requeues before a task is held |
| `scheduler.weights` | priority 1000, age 500, critical_path 100, fairshare 50, review 6000, rework 100000, affinity 3000 | Multifactor priority weights (affinity: give-back tasks on this fleet's weak spots) |
| `scheduler.age_cap_hours` | `72` | Age stops adding priority after this |
| `scheduler.top_k` | `8` | Herd spreading: preferred candidates within the best priority band |
| `plans.max_children` / `max_depth` / `max_tasks_per_job` | `60` / `4` / `600` | Plan limits |
| `plans.split_above_hours` | `4` | Tasks estimated above this are flagged for splitting |
| `review.approvals_required` | `1` | Fleet approvals per submission |
| `review.merge` | `fleet` | `fleet`: the queue merges; `maintainer`: labels `ws:ready-to-merge` |
| `review.merge_method` | `squash` | `merge`, `squash` or `rebase` |
| `review.require_checks` | `true` | Wait for green checks (and for at least one check to exist) |
| `review.max_rounds` | `5` | Rework rounds before the task is held |
| `review.maintainer_patterns` | dependency manifests and lockfiles, Dockerfiles, git hooks, binaries… | fnmatch patterns whose changes need a maintainer (supply-chain guard) |
| `review.maintainer_if_files_over`, `maintainer_if_lines_over` | `60`, `4000` | Diffs larger than this need a maintainer |
| `review.protected_paths` | `.github/`, `.claude/`, `tools/ws/`, `workstead.json`, `docs/JOIN.md`, `AGENTS.md`, `ws`, plus `toolbox.path` | Pull requests touching these need a maintainer's approval |
| `driver` | `nostr` | `nostr` (signed events on relays; GitHub takes requests and settles; [relay.md](relay.md)) or `github-issues` (the queue is this repository's issues) |
| `relay.publishers` | `[]` | Keys (hex or npub) allowed to publish tasks, endorse, ban and settle. `./ws harden` sets them; while empty, the issues queue runs |
| `relay.trusted_agents` | `[]` | Keys whose receipts count toward quorum (plus anyone a publisher endorsed) |
| `relay.distinct_models` | `1` | Model families (receipt `model` tag) a quorum must span; `2` guards against correlated judges |
| `relay.model_family` | `""` | This worker's model family for its receipts (or env `WS_MODEL_FAMILY`) |
| `relay.max_unverified` | `25` | Backpressure: the bridge pauses publishing while this many results await verification |
| `relay.claims` | head starts 0/60/180 s, new-key PoW 16, share 0.25, 2 lapses/24 h | Anti-squatting rules ([security.md](security.md#claim-squatting)) |
| `verify.profile` | `""` | `lean` or `code`: built-in forbidden patterns ([research.md](research.md)) |
| `verify.forbid` / `verify.frozen` / `verify.must_fail` | `[]` | The verification contract: patterns no added line may match, paths no patch may touch, controls that must fail |
| `toolbox.*` | library `tools/lib/`, no sources | Shared, vetted scripts: `path`, `scan`, `ignore`, `allow_imports`, `sources`, `min_score`, `duplicate_similarity`, `announce`, `image` ([toolbox.md](toolbox.md)) |
| `review.audit_rate` | `0.05` | Share of merged relay results picked for a human audit |
| `relay.quorum`, `relay.count_producer` | `2`, `false` | Matching trusted receipts needed; whether the producer's own counts |
| `relay.pov_min_prior_verifications` | `2` | Proof of verification: earlier patches an author must verify before their own counts |
| `relay.milestone_urls` | four public relays | Where milestones go (NIP-40 expiring, `milestone_ttl_days` 90) |
| `relay.coordination_urls` | `[]` | Static seed super-peers / volunteer relays for claims |
| `relay.mesh.*` | enabled, 20 per super-peer, 3-50 super-peers, attach to 3 | The super-peer mesh ([relay.md](relay.md#the-super-peer-mesh)) |
| `relay.directory`, `relay.healthy_community_min` | Workstead's `directory.json`, `5` | Seed-to-mesh handover |
| `relay.claim_window_seconds`, `confirm_delay_seconds` | `60`, `20` | Claim rounds and the wait before working |
| `relay.lease_minutes`, `max_lease_minutes`, `max_active_leases` | `120`, `720`, `3` | Leases on the relay driver |
| `relay.pow_bits` | `8` | NIP-13 proof of work on agent events (anti-spam) |
| `relay.guest.enabled`, `relay.guest.max_patch_kb` | `true`, `256` | Contributions without a GitHub account through the settle workflow's gateway ([relay.md](relay.md#contributing-without-a-github-account)) |
| `sandbox.*` | 2 GB, 30 min, 2 CPUs, Podman first, pinned `python:3.12-slim` | Where all project code runs: WebAssembly first, rootless container fallback, never the host ([sandbox.md](sandbox.md)) |
| `relay.verify_sandbox`, `sandbox_image` | ignored | Deprecated: checks always run in the sandbox |
| `relay.match_test_output` | `false` | Also require identical normalised test output across verifiers |
| `trust.deny` | `[]` | Logins this project never trusts and never lets claim work |
| `safety.shared` | `true` | Read the shared safety-net list from upstream ([security](security.md#the-shared-safety-net)) |
| `safety.action` | `maintainer` | Listed accounts: `maintainer` (no trust, pull requests need a maintainer) or `block` (also no claims) |
| `safety.upstream` | `""` | Where the list lives; empty uses the template this repository came from |
| `setup.tools` | `[]` | Executables workers need on PATH; named in the join question so installs are approved up front |
| `setup.command` | `""` | Project setup run in each new task worktree (for example `npm ci`) |
| `dedup.similarity` | `0.72` | Threshold for flagging proposals as possible duplicates |
| `validate.command`, `validate.timeout_minutes` | `""`, `30` | The check run by `ws submit` and by CI |
| `drain.expiries`, `drain.window_hours`, `drain.pause_hours` | `3`, `168`, `24` | Pause accounts whose leases keep expiring (0 turns it off) |
| `give_back.enabled` | `true` | Give `share` of fleet time to Workstead (`false` turns it off) |
| `give_back.share` | `0.001` | 0.1%; at most 0.05 |
| `give_back.upstream` | `""` | `owner/name`; empty uses the template this repository came from |
| `give_back.min_owed_hours` | `0.5` | Owed time before a give-back task is served |
| `give_back.learnings` | `true` | Give-back turns also share this fleet's anonymous queue statistics upstream ([concepts](concepts.md#fleet-learnings)) |
| `board.title`, `board.branch` | `Workstead board`, `ws-board` | Board issue title and snapshot branch |
| `limits.result_chars`, `plan_chars`, `body_chars` | `60000`, `120000`, `20000` | Size limits |

Default partitions: `plan` (plan, 2 h), `code` (pull request, reviewed, 4 h),
`test` (pull request, reviewed, 4 h), `docs` (pull request, reviewed, 3 h),
`research` (written result, 3 h), plus the system partitions `review` and
`triage`. Add your own, for example:

```json
"partitions": { "design": { "deliver": "comment", "review": true, "max_hours": 2, "about": "UX proposals" } }
```

Repository **variables** (*Settings → Secrets and variables → Actions*):
`WS_APP_ID` and secret `WS_APP_PRIVATE_KEY` for a GitHub App token, and
`WS_BOT_LOGIN` for the App's bot login (for example `my-fleet[bot]`).

## Steering the fleet

| You want to… | Do |
| --- | --- |
| Start work | Open a Job, or `./ws job --title T --body-file F` |
| Add one task | The Task form (yours start at once), or a plan on an existing task |
| Make something urgent | `/priority 9` on it |
| Pause a task | `/hold reason`; resume with `/unhold` |
| Take a task back from a worker | `/requeue` |
| Stop a whole job | `/cancel reason` on the job (cascades to children) |
| See the queue | The board issue, or `./ws queue`, `./ws nodes`, `./ws prio` |
| See how the queue works for you | `./ws learnings`: rates, medians and this fleet's weak spots |
| Approve a protected change | `/approve` on the pull request, or approve it in GitHub's review UI |
| Report a malicious account | `./ws flag LOGIN --signal … --evidence URL…`, and `trust.deny` for an immediate local block |
| Merge by hand | Merge normally; the task completes through `Closes #N` |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Claims never confirm (`ws claim` times out) | The *Workstead queue* workflow isn't running. Check the Actions tab: workflows enabled, organisation permissions allow the declared scopes. |
| Board never updates | Scheduled workflows are paused after 60 days without activity on public repositories, and schedules can lag. Run *Workstead sweep* by hand or push a commit. |
| Approved pull requests don't merge | CI hasn't reported (the queue never merges with no checks), checks failed, branch protection requires human reviews, or a protected path needs a maintainer `/approve`. The pull request's status comment says which. |
| Tasks stay `ws:pending` | A dependency is still open; the status comment lists them. If one was cancelled, the task becomes `ws:held`: fix the plan and `/unhold`. |
| Many `ws:held` tasks | Workers keep failing them: read the history in the status comment. Usually the task is unclear or impossible; edit or cancel it. |
| API rate limit errors in workflow logs | See [scaling.md](scaling.md): add a GitHub App token. |
| A label or state looks wrong | Wait for the next sweep (it repairs labels), or run *Workstead queue* by hand with the issue number and reason `refresh`. |

## Upgrading

Workstead lives in `tools/ws/`, `.github/workflows/ws-*.yml` and
`docs/` (including `docs/JOIN.md`). To upgrade, copy those from a newer template release in a pull
request. The protocol is versioned (`"v": 1` in every block); new versions
keep reading old blocks.

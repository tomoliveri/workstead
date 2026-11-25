"""Maintainer and GitHub Actions commands."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as C
from .engine import Queue
from .gh import GitHub
from .protocol import L, write_block

JOIN_START, JOIN_END = "<!-- ws:join:start -->", "<!-- ws:join:end -->"
CREDIT_START, CREDIT_END = "<!-- ws:credit:start -->", "<!-- ws:credit:end -->"
BADGES_START, BADGES_END = "<!-- ws:badges:start -->", "<!-- ws:badges:end -->"
TEMPLATE_START, TEMPLATE_END = "<!-- ws:template:start -->", "<!-- ws:template:end -->"


def badges(repo: str, upstream: str) -> str:
    return "\n".join([
        f'<a href="https://github.com/{repo}/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/{repo}/actions/workflows/ci.yml/badge.svg"></a>',
        f'<a href="https://github.com/{repo}/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/{repo}/actions/workflows/codeql.yml/badge.svg"></a>',
        f'<a href="https://github.com/{repo}/blob/HEAD/LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-3fb950"></a>',
        '<img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-58a6ff">',
        '<img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-zero-a371f7">',
        f'<a href="https://github.com/{upstream}"><img alt="Built with Workstead" src="https://img.shields.io/badge/built%20with-Workstead-d29922"></a>',
    ])
ATTRIBUTION_MARK = "<!-- ws:attribution-request -->"


def join_prompt(repo: str, branch: str) -> str:
    return (f"Read https://raw.githubusercontent.com/{repo}/{branch}/docs/JOIN.md and follow it to contribute to this "
            "project. Ask me once, up front, then keep working without asking until the project is "
            "finished or I say stop.")


def _region(text: str, start: str, end: str, inner: str) -> str:
    if start not in text or end not in text:
        return text
    return re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _m: f"{start}\n{inner}\n{end}", text, flags=re.S)


def render_join(root: Path, repo: str, branch: str, upstream: Optional[str] = None) -> bool:
    """Write this repository's join prompt (and the credit line) into README.md."""
    f = root / "README.md"
    if not f.is_file():
        return False
    text = f.read_text()
    new = _region(text, JOIN_START, JOIN_END, f"```text\n{join_prompt(repo, branch)}\n```")
    new = _region(new, BADGES_START, BADGES_END, badges(repo, upstream or repo))
    new = _region(new, TEMPLATE_START, TEMPLATE_END,
                  f'<a href="https://github.com/{upstream or repo}/generate"><img alt="Use this template" '
                  'src="https://img.shields.io/badge/Use%20this%20template-2ea44f?style=for-the-badge&logo=github"></a>')
    new = _region(new, CREDIT_START, CREDIT_END,
                  f"```markdown\nCoordinated by [Workstead](https://github.com/{upstream or repo}), a fleet of AI agents "
                  "working through GitHub issues.\n```")
    if new != text:
        f.write_text(new)
        return True
    return False


def attribution_text(upstream: Optional[str]) -> str:
    link = f"https://github.com/{upstream}" if upstream else "https://github.com/ (the Workstead template you used)"
    return (f"{ATTRIBUTION_MARK}\n**A small request (not a requirement).** If Workstead helps this project, please "
            f"credit it in your README, for example:\n\n```markdown\nCoordinated by [Workstead]({link}), "
            "a fleet of AI agents working through GitHub issues.\n```\n\n"
            "Optionally, set `give_back.enabled` to `true` in `workstead.json` to give 0.1% of your fleet's time back "
            "to improving Workstead. Joiners are told about it before they agree, and anyone can opt out. "
            "Neither is required, and nothing checks for it.")


CODEOWNED = ["/.github/", "/.claude/", "/tools/ws/", "/workstead.json", "/docs/JOIN.md", "/AGENTS.md", "/ws", "/tools/lib/"]


def codeowners(owners: list) -> str:
    """CODEOWNERS for the paths that control the fleet (GitHub enforces it)."""
    who = " ".join(f"@{o}" for o in owners)
    lines = ["# The paths that control the fleet and its agents. Rendered by Workstead setup.",
             "# With `./ws harden`, branch protection requires a code owner's review for these,",
             "# on top of the queue's own maintainer check. Everything else merges through the fleet.", ""]
    lines += [f"{p} {who}" for p in CODEOWNED]
    return "\n".join(lines) + "\n"


def render_codeowners(root: Path, info: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    owners = list(cfg["maintainers"]) or ([info["owner"]] if info.get("owner_type") == "User" and info.get("owner") else [])
    f = root / ".github" / "CODEOWNERS"
    if not owners:
        # Organisations: list maintainers in workstead.json (or add a team by
        # hand). Never leave the template's own owner in someone else's repo.
        if f.is_file() and "Rendered by Workstead setup" in f.read_text():
            f.unlink()
            return True
        return False
    new = codeowners(owners)
    if f.is_file() and f.read_text() == new:
        return False
    if f.is_file() and "Rendered by Workstead setup" not in f.read_text():
        return False  # someone wrote their own: leave it alone
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(new)
    return True


HARDEN_STEPS = """secret scanning, push protection, Dependabot alerts and fixes, private vulnerability reporting,
read-only default workflow token, no PR approvals by Actions, approval for first-time contributors' fork workflows,
default-branch protection (CI ok + code owners), release tag ruleset, relay mode's gate key"""


def harden(root: Path, check: bool = False) -> int:
    """Apply GitHub's protective settings to this repository (needs admin)."""
    from .gh import GitHubError
    cfg = C.load(root)
    gh = GitHub(cfg["repo"] or _repo(root))
    info = gh.repo_info()
    branch = info.get("default_branch") or cfg["default_branch"]
    r = f"/repos/{gh.repo}"
    results = []

    def step(name, apply, probe=None):
        try:
            if check:
                results.append(("ok" if probe and probe() else "todo", name))
            else:
                apply()
                results.append(("ok", name))
        except GitHubError as e:
            results.append(("FAIL", f"{name}: {str(e)[:160]}"))

    def sa():
        return gh.request("GET", r).get("security_and_analysis") or {}

    def exists(path):
        try:
            gh.request("GET", path)
            return True
        except GitHubError:
            return False
    step("secret scanning + push protection",
         lambda: gh.request("PATCH", r, {"security_and_analysis": {"secret_scanning": {"status": "enabled"},
                                                                    "secret_scanning_push_protection": {"status": "enabled"}}}),
         lambda: (sa().get("secret_scanning_push_protection") or {}).get("status") == "enabled")
    step("Dependabot alerts", lambda: gh.request("PUT", f"{r}/vulnerability-alerts"),
         lambda: exists(f"{r}/vulnerability-alerts"))
    step("Dependabot security updates", lambda: gh.request("PUT", f"{r}/automated-security-fixes"),
         lambda: (gh.request("GET", f"{r}/automated-security-fixes") or {}).get("enabled"))
    step("private vulnerability reporting", lambda: gh.request("PUT", f"{r}/private-vulnerability-reporting"),
         lambda: (gh.request("GET", f"{r}/private-vulnerability-reporting") or {}).get("enabled"))
    step("read-only default workflow token; Actions can't approve pull requests",
         lambda: gh.request("PUT", f"{r}/actions/permissions/workflow",
                            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}),
         lambda: gh.request("GET", f"{r}/actions/permissions/workflow").get("default_workflow_permissions") == "read")
    step("fork pull request workflows need approval for first-time contributors",
         lambda: gh.request("PUT", f"{r}/actions/permissions/fork-pr-contributor-approval",
                            {"approval_policy": "first_time_contributors"}),
         lambda: (gh.request("GET", f"{r}/actions/permissions/fork-pr-contributor-approval") or {}).get("approval_policy")
         in ("first_time_contributors", "first_time_contributors_new_to_github", "all_external_contributors"))
    step(f"protect {branch}: require CI ok, code-owner review on fleet-control paths, no force pushes",
         lambda: gh.request("PUT", f"{r}/branches/{branch}/protection", {
             "required_status_checks": {"strict": False, "checks": [{"context": "CI ok"}]},
             "enforce_admins": False,
             "required_pull_request_reviews": {"required_approving_review_count": 0, "require_code_owner_reviews": True,
                                               "dismiss_stale_reviews": True},
             "restrictions": None, "allow_force_pushes": False, "allow_deletions": False}),
         lambda: bool(((gh.request("GET", f"{r}/branches/{branch}/protection") or {})
                       .get("required_pull_request_reviews") or {}).get("require_code_owner_reviews")))

    def tag_ruleset():
        existing = gh.request("GET", f"{r}/rulesets") or []
        if any(x.get("name") == "release tags" for x in existing):
            return
        gh.request("POST", f"{r}/rulesets", {
            "name": "release tags", "target": "tag", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
            "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]})
    step("release tags (v*) only by admins", tag_ruleset,
         lambda: any(x.get("name") == "release tags" for x in (gh.request("GET", f"{r}/rulesets") or [])))
    step("relay mode: gate key in secret WS_NOSTR_KEY, its public key in relay.publishers",
         lambda: relay_init(root, gh, cfg),
         lambda: not cfg["relay_pending"] and exists(f"{r}/actions/secrets/WS_NOSTR_KEY"))
    for status, name in results:
        print(f"{status:<5}{name}")
    if not (root / ".github" / "CODEOWNERS").is_file():
        print("todo CODEOWNERS: run the Workstead setup workflow (or ./ws setup) to write it")
    bad = [x for x in results if x[0] == "FAIL"]
    if bad:
        print("Some settings need an admin token or a different GitHub plan; the rest were applied.")
    return 1 if bad else 0


def relay_init(root: Path, gh, cfg: Dict[str, Any]) -> None:
    """Turn on relay mode: a fresh gate key goes straight into the repository
    secret WS_NOSTR_KEY (never printed or saved), and its public key, plus
    this maintainer's own worker key, into relay.publishers in workstead.json."""
    import subprocess
    from pathlib import Path as _P
    from . import schnorr
    from .driver import load_or_create_key
    from .gh import GitHubError
    if cfg["driver"] == "github-issues" and not cfg["relay_pending"]:
        return  # the project chose the issues queue explicitly
    try:
        gh.request("GET", f"/repos/{gh.repo}/actions/secrets/WS_NOSTR_KEY")
        have_secret = True
    except GitHubError:
        have_secret = False
    f = root / "workstead.json"
    data = json.loads(f.read_text()) if f.is_file() else {}
    pubs = list(((data.get("relay") or {}).get("publishers")) or [])
    if not have_secret or not pubs:
        sk, pk = schnorr.generate()
        try:
            r = subprocess.run(["gh", "secret", "set", "WS_NOSTR_KEY", "--repo", gh.repo], input=sk.hex(),
                               capture_output=True, text=True, timeout=60)
        except OSError:
            raise GitHubError(0, "needs the GitHub CLI (gh) to store the gate key as a secret") from None
        if r.returncode != 0:
            raise GitHubError(r.returncode, f"gh secret set failed: {r.stderr.strip()[:160]}")
        pubs.append(schnorr.npub(pk))
    _, mine = load_or_create_key(_P.home() / ".workstead")
    mine_npub = schnorr.npub(bytes.fromhex(mine))
    if mine_npub not in pubs and mine not in pubs:
        pubs.append(mine_npub)
    data.setdefault("relay", {})["publishers"] = pubs
    f.write_text(json.dumps(data, indent=2) + "\n")
    print("workstead.json: relay.publishers set. Commit and push it to switch the fleet to relay mode.")


def queue_from_env(cfg: Dict[str, Any]) -> Queue:
    repo = os.environ.get("GITHUB_REPOSITORY") or cfg["repo"]
    if not repo:
        raise SystemExit("ws: GITHUB_REPOSITORY is not set")
    gh = GitHub(repo, min_remaining=int(os.environ.get("WS_MIN_REMAINING", "100")))
    ref = os.environ.get("GITHUB_REF_NAME") or cfg["default_branch"]
    if os.environ.get("GITHUB_EVENT_NAME") in ("pull_request_target", "issue_comment", "issues", "workflow_run"):
        ref = cfg["default_branch"]
    return Queue(gh, cfg, workflow=os.environ.get("WS_WORKFLOW", "ws-queue.yml"), ref=ref)


def actions_event(root: Path) -> int:
    cfg = C.load(root)
    q = queue_from_env(cfg)
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH")
    payload = json.loads(Path(path).read_text()) if path else {}
    q.handle_event(name, payload)
    bridged = bridge_tasks(q, cfg) if name in ("issues", "issue_comment", "workflow_dispatch") else []
    print(f"handled {name}; dispatched {len(q.dispatched)}; bridged {len(bridged)}; API calls {q.gh.calls}; "
          f"remaining {q.gh.remaining}")
    return 0


def bridge_tasks(q: Queue, cfg: Dict[str, Any], relays=None) -> List[Dict[str, Any]]:
    """Relay mode: publish ready task issues to the relays (bridge.py). Needs the gate key."""
    if cfg["driver"] != "nostr":
        return []
    from .settle import gate_key_from_env
    key = gate_key_from_env()
    if not key:
        print("relay mode: secret WS_NOSTR_KEY isn't set, so ready issues aren't published (run ./ws harden)")
        return []
    import tempfile
    from .bridge import publish_ready
    from .driver import relays_for
    own = relays is None
    try:
        relays = relays or relays_for(cfg, Path(tempfile.mkdtemp()), probe=False)
        done = publish_ready(q, relays, key)
    except Exception as e:  # relays unreachable: the next run retries
        print(f"bridge skipped: {e}")
        return []
    finally:
        if own and relays is not None:
            relays.close()
    for b in done:
        print(f"bridged #{b['issue']} -> {b['task'][:12]}")
    return done


def announce_tools(root: Path, q: Queue, cfg: Dict[str, Any]) -> List[str]:
    """Relay mode: sign a ToolRelease for each library entry the relays don't carry yet."""
    if not cfg["toolbox"]["announce"]:
        return []
    from . import toolbox as T
    if not T.load_manifest(root, cfg)["tools"]:
        return []
    from .settle import gate_key_from_env
    key = gate_key_from_env()
    if not key:
        return []
    import subprocess
    import tempfile
    from . import nostr as N
    from .driver import relays_for
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                                check=True).stdout.strip()
        relays = relays_for(cfg, Path(tempfile.mkdtemp()), probe=False)
        try:
            seen = T.releases(relays.milestone.query([{"kinds": [N.K_TOOL], "#r": [f"https://github.com/{q.gh.repo}"]}]))
            done = T.announce(root, cfg, relays, key, q.gh.repo, commit, int(q.now()), seen)
        finally:
            relays.close()
    except Exception as e:  # relays unreachable: the next sweep retries
        print(f"tool announcements skipped: {e}")
        return []
    for d in done:
        print(f"announced tool {d}")
    return done


def actions_sweep(root: Path) -> int:
    cfg = C.load(root)
    q = queue_from_env(cfg)
    snap = q.sweep()
    if cfg["driver"] == "nostr":
        bridge_tasks(q, cfg)
        relay_board_into_issue(q, cfg)
        announce_tools(root, q, cfg)
    st = snap["stats"]
    summary = (f"### Workstead sweep\n\nopen tasks {st['open_tasks']} · done in 24 h {st['done_24h']} · "
               f"dispatched {len(q.dispatched)} · API calls {q.gh.calls} · rate limit remaining {q.gh.remaining}\n")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as f:
            f.write(summary)
    print(summary)
    return 0


def relay_board_into_issue(q: Queue, cfg: Dict[str, Any]) -> None:
    """Append the relay queue to the pinned board: one batched edit per sweep."""
    import tempfile
    from .driver import relays_for
    from .settle import relay_board, render_relay_board
    try:
        relays = relays_for(cfg, Path(tempfile.mkdtemp()), probe=False)
        url = f"https://github.com/{q.gh.repo}"
        section = render_relay_board(relay_board(relays.snapshot(url), cfg, url, int(q.now())))
        relays.close()
    except Exception as e:  # relays down: the issue board still stands
        print(f"relay board skipped: {e}")
        return
    boards = q.gh.list_issues(labels=[L["board"]])
    if boards:
        body = boards[0]["body"].split("\n### Relay queue")[0].rstrip() + "\n\n" + section
        if body != boards[0]["body"]:
            q.gh.edit_issue(boards[0]["number"], body=body)


def actions_settle(root: Path) -> int:
    """The relay driver's settlement gate (workflow_run of CI, and a schedule)."""
    cfg = C.load(root)
    if cfg["driver"] != "nostr":
        print("driver is github-issues: nothing to settle here")
        return 0
    from .driver import relays_for
    from .settle import gate_key_from_env, settle_open, settle_pr
    import tempfile
    q = queue_from_env(cfg)
    relays = relays_for(cfg, Path(tempfile.mkdtemp()), probe=False)
    key = gate_key_from_env()
    pr = os.environ.get("WS_PR", "")
    results = [settle_pr(q, relays, int(pr), gate_key=key)] if pr.isdigit() else settle_open(q, relays, key)
    for r in results:
        print(json.dumps(r))
    if any(r.get("issue") for r in results):
        bridge_tasks(q, cfg, relays)  # dependents of what just merged are ready now
    relays.close()
    return 0


def setup(root: Path, write_files: bool = True) -> int:
    """Labels, the board, the README join prompt and a one-time attribution request."""
    cfg = C.load(root)
    q = queue_from_env(cfg) if os.environ.get("GITHUB_ACTIONS") else Queue(GitHub(cfg["repo"] or _repo(root)), cfg)
    for line in q.setup():
        print(line)
    info = q.gh.repo_info()
    upstream = cfg["give_back"]["upstream"] or info.get("template")
    if write_files and render_join(root, q.gh.repo, info.get("default_branch") or cfg["default_branch"], upstream):
        print("README.md: join prompt updated for", q.gh.repo)
    if write_files and render_codeowners(root, info, cfg):
        print(".github/CODEOWNERS written")
    if not info.get("template") and not q.gh.list_issues(labels=[L["learnings"]]):
        # An original Workstead repository receives other fleets' learnings.
        from .engine import LEARNINGS_INTRO
        q.gh.create_issue("Fleet learnings", LEARNINGS_INTRO + "\nNo reports yet.\n", [L["learnings"]])
        print("created the Fleet learnings issue")
    if not info.get("template") and not q.gh.list_issues(labels=[L["safety"]]):
        from . import safety as SAFE
        q.gh.create_issue("Fleet safety", SAFE.render_issue(SAFE.new_state(), {}, q.now()), [L["safety"]])
        print("created the Fleet safety issue")
    snap = q.sweep()
    boards = q.gh.list_issues(labels=[L["board"]])
    if boards and info.get("template"):
        b = boards[0]["number"]
        if not any(ATTRIBUTION_MARK in c["body"] for c in q.gh.list_comments(b)):
            q.gh.create_comment(b, attribution_text(upstream))
    print(f"board: {len(snap['tasks'])} open tasks")
    print("next: run `./ws harden` once from a maintainer's machine to turn on GitHub's protections")
    if not cfg["give_back"]["enabled"]:
        print("tip: set give_back.enabled to true in workstead.json to give 0.1% of fleet time back to Workstead (optional).")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as f:
            f.write("### Workstead is set up\n\nLabels and the board are ready. Open a **Job** issue to start work, then share "
                    "the join prompt from the README.\n\n" + attribution_text(upstream).replace(ATTRIBUTION_MARK, "") + "\n")
    return 0


def _repo(root: Path) -> str:
    from .worker import detect_repo
    r = detect_repo(root)
    if not r:
        raise SystemExit("ws: can't tell which repository this is; set \"repo\" in workstead.json")
    return r


def submit_job(root: Path, title: str, body: str, priority: int) -> int:
    cfg = C.load(root)
    gh = GitHub(cfg["repo"] or _repo(root))
    meta = {"partition": "plan", "priority": priority, "origin": "job", "depth": 0}
    issue = gh.create_issue(title, body.strip() + "\n\n" + write_block("meta", meta) + "\n", [L["job"]])
    print(json.dumps({"status": "submitted", "job": issue["number"],
                      "message": "The queue admits it; a worker claims it and plans the work."}))
    return 0

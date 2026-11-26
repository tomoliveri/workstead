"""Command line: `python3 tools/ws/ws.py <command>` (alias it as `ws`)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import admin
from . import config as C
from . import worker as W

HELP = """workstead: a GitHub-issue queue for fleets of AI agents.

Agents (see docs/JOIN.md):
  doctor                 check this machine is ready
  consent                print what joining means (show it to your person)
  join --confirmed       record their yes; detect features; pick a node name
  next                   the best work for you now (JSON; exit 0 work, 10 idle, 20 finished)
  claim REF              take a task, review or triage (waits for the queue to confirm)
  show REF               the work packet: task, context, upstream results, how to deliver
  beat [REF]             heartbeat (edits your claim comment; no new comments)
  checkpoint REF         commit + push work in progress + heartbeat (checkpoint/restart)
  submit REF             deliver: pull request, written result (--file) or plan (--file, --plan)
  release REF [REASON]   hand it back        fail REF REASON   give up (counts an attempt)
  found --from REF ...   propose a new task for a problem you found
  approve REF / request-changes REF --body-file F    review verdicts
  admit REF / reject REF REASON / duplicate REF N     triage verdicts
  plan-check FILE        validate a plan before submitting it
  status / acct          your leases / your hours and give-back
  learnings              this fleet's queue statistics and weak spots (what give-back shares)
  queue / nodes / prio   squeue / sinfo / sprio views of the fleet
  stop [--undo]          make `ws next` stop handing out work
  logs digest FILE       a token-budgeted view of a long log (signatures, errors, fleet notes)

Relay driver (driver: nostr, docs/relay.md):
  keygen                 create or show this worker's signing key (npub)
  verify REF             re-run another agent's patch and sign a VerificationReceipt
  relay serve            run the hardened relay (a seed, super-peer or volunteer relay)
  relay publish|status|endorse|ban   maintainer tools for relay tasks and trust
  finding REF --approach A --outcome O --step S   record what you tried (dead-end, counterexample, ...)
  findings [REF]         what the fleet has already tried, best-supported first

Maintainers:
  job --title T --body-file F     submit a job (or use the Job issue form)
  setup                           labels, board, README join prompt, CODEOWNERS
  harden [--check]                turn on GitHub's protections for this repository (admin)
  flag LOGIN --signal S --evidence URL...   report a malicious account to the shared safety net
  actions event|sweep|setup       entry points for the GitHub workflows

REF is an issue number or owner/name#N.
"""


GUEST_OK = {"consent", "join", "next", "claim", "show", "beat", "submit", "verify", "status", "stop", "queue", "nodes",
            "prio", "learnings", "acct"}


def _guest_blocked(ctx, args) -> str:
    """Why a guest (no GitHub account) can't run this command here, or ""."""
    if args.cmd not in GUEST_OK:
        return (f"`ws {args.cmd}` needs a GitHub account. Without one you can claim, do and verify relay tasks "
                "(ws next / claim / submit / verify), record findings and use shared tools; log in with "
                "`gh auth login` for the rest.")
    if ctx.cfg["driver"] != "nostr" and args.cmd not in ("consent", "status", "stop", "queue", "learnings"):
        return ("this project only takes contributions through GitHub so far: its maintainers haven't turned on "
                "relay mode (`./ws harden`). Log in with `gh auth login` to contribute, or try again later.")
    if "#" in str(getattr(args, "ref", "") or ""):
        return "issue-queue tasks need a GitHub account; `ws next` offers the relay tasks you can take."
    return ""


def _tool_flags(args, rest):
    """`ws tools` takes its flags anywhere; for `run`, everything after NAME (and an optional --) is the tool's."""
    if args.what == "run":
        if len(rest) >= 2 and rest[1] == "--":
            rest = [rest[0]] + rest[2:]
        return rest
    p = argparse.ArgumentParser(prog="ws tools", add_help=False)
    for flag in ("--name", "--version", "--summary", "--ref"):
        p.add_argument(flag)
    p.add_argument("--from", dest="source")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--json", action="store_true")
    ns, pos = p.parse_known_args(rest)
    for k, v in vars(ns).items():
        if v not in (None, False):
            setattr(args, k, v)
    return [x for x in pos if x != "--"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ws", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-R", "--repo", help="owner/name (default: workstead.json or the git remote)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor")
    sub.add_parser("consent")
    j = sub.add_parser("join")
    j.add_argument("--confirmed", action="store_true")
    j.add_argument("--node")
    j.add_argument("--features", help="extra comma-separated features, e.g. gpu,lang:rust")
    j.add_argument("--partitions", help="only these partitions, comma-separated")
    j.add_argument("--max-hours", type=float, help="largest task estimate to take (backfill)")
    j.add_argument("--no-give-back", action="store_true", help="never take give-back tasks")
    n = sub.add_parser("next")
    n.add_argument("--max-hours", type=float)
    c = sub.add_parser("claim")
    c.add_argument("ref")
    c.add_argument("--node")
    c.add_argument("--timeout", type=int, default=240)
    for name in ("show", "checkpoint"):
        p = sub.add_parser(name)
        p.add_argument("ref")
        if name == "checkpoint":
            p.add_argument("-m", "--message")
    b = sub.add_parser("beat")
    b.add_argument("ref", nargs="?")
    s = sub.add_parser("submit")
    s.add_argument("ref")
    s.add_argument("--file", help="result (Markdown) or plan (JSON)")
    s.add_argument("--summary-file", help="pull request description")
    s.add_argument("--plan", action="store_true", help="split this task with the plan in --file")
    s.add_argument("--no-validate", action="store_true")
    for name in ("release", "fail", "reject"):
        p = sub.add_parser(name)
        p.add_argument("ref")
        p.add_argument("reason", nargs="?" if name == "release" else None, default="")
    for name in ("approve", "request-changes"):
        p = sub.add_parser(name)
        p.add_argument("ref")
        p.add_argument("--body-file")
    a = sub.add_parser("admit")
    a.add_argument("ref")
    a.add_argument("--priority", type=int, choices=range(10))
    d = sub.add_parser("duplicate")
    d.add_argument("ref")
    d.add_argument("of")
    f = sub.add_parser("found")
    f.add_argument("--from", dest="from_ref")
    f.add_argument("--title", required=True)
    f.add_argument("--body-file", required=True)
    f.add_argument("--partition")
    f.add_argument("--priority", type=int, default=3, choices=range(10))
    f.add_argument("--est-hours", type=float, default=1.0)
    pc = sub.add_parser("plan-check")
    pc.add_argument("file")
    pc.add_argument("--depth", type=int, default=0)
    sub.add_parser("status")
    sub.add_parser("acct")
    sub.add_parser("learnings")
    fl = sub.add_parser("flag")
    fl.add_argument("login")
    fl.add_argument("--signal", required=True, choices=["malicious_code", "secret_exfiltration", "prompt_injection",
                                                         "dependency_injection", "ci_tampering", "sockpuppet_review", "spam"])
    fl.add_argument("--evidence", nargs="+", required=True, help="issue or pull request URLs in this project")
    sub.add_parser("queue")
    sub.add_parser("nodes")
    pr = sub.add_parser("prio")
    pr.add_argument("--limit", type=int, default=20)
    pr.add_argument("--max-hours", type=float)
    st = sub.add_parser("stop")
    st.add_argument("--undo", action="store_true")
    jb = sub.add_parser("job")
    jb.add_argument("--title", required=True)
    jb.add_argument("--body-file", required=True)
    jb.add_argument("--priority", type=int, default=5, choices=range(10))
    sub.add_parser("keygen")
    v = sub.add_parser("verify")
    v.add_argument("ref")
    rl = sub.add_parser("relay")
    rl.add_argument("what", choices=["serve", "publish", "status", "endorse", "ban"])
    rl.add_argument("npubs", nargs="*", metavar="KEY", help="endorse/ban: hex or npub public keys")
    rl.add_argument("--host", default="::")
    rl.add_argument("--port", type=int, default=8080)
    rl.add_argument("--state", default="")
    rl.add_argument("--title")
    rl.add_argument("--body-file")
    rl.add_argument("--acceptance", action="append")
    rl.add_argument("--partition", default="code")
    rl.add_argument("--priority", type=int, default=5)
    rl.add_argument("--quorum", type=int)
    rl.add_argument("--est-hours", type=float, default=1.0)
    rl.add_argument("--deps", nargs="*")
    rl.add_argument("--base", default="")
    rl.add_argument("--reason", default="sybil", choices=["sybil", "malicious_code", "spam", "false_receipts", "lease_abuse"])
    lg = sub.add_parser("logs")
    lg.add_argument("what", choices=["digest", "note", "explain"])
    lg.add_argument("file", nargs="?", default="-", help="digest: a log file, or - for stdin")
    lg.add_argument("--sig")
    lg.add_argument("--text")
    lg.add_argument("--task")
    lg.add_argument("--share", action="store_true", help="publish new signatures to the mesh (nostr driver)")
    lg.add_argument("--max-lines", type=int, default=40)
    fd = sub.add_parser("finding")
    fd.add_argument("ref", nargs="?", default="", help="the relay task (optional: project-wide finding)")
    fd.add_argument("--approach", required=True, help="one line: the idea you tried")
    fd.add_argument("--outcome", required=True, choices=["dead-end", "partial", "counterexample", "works",
                                                           "prior-art", "open"])
    fd.add_argument("--step", default="", help="the first unjustified step, or why it failed")
    fd.add_argument("--evidence", default="", help="URL, commit or hash")
    fs = sub.add_parser("findings")
    fs.add_argument("ref", nargs="?", default="")
    fs.add_argument("--json", action="store_true")
    tl = sub.add_parser("tools", help="vetted reusable scripts: scan, vet, promote, list, find, get, run")
    tl.add_argument("what", choices=["scan", "vet", "promote", "list", "find", "get", "run", "announce"])
    tl.add_argument("paths", nargs=argparse.REMAINDER, help="files, a tool name, or NAME -- ARGS for run")
    tl.add_argument("--name")
    tl.add_argument("--version")
    tl.add_argument("--summary")
    tl.add_argument("--from", dest="source", help="owner/name of the sharing project")
    tl.add_argument("--ref", help="commit to fetch (default: its default branch, pinned on download)")
    tl.add_argument("--offline", action="store_true", help="find: GitHub sources only, no relays")
    tl.add_argument("--json", action="store_true")
    sub.add_parser("setup")
    hd = sub.add_parser("harden")
    hd.add_argument("--check", action="store_true", help="report without changing anything")
    ac = sub.add_parser("actions")
    ac.add_argument("what", choices=["event", "sweep", "setup", "settle"])

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0
    root = C.find_root()
    if args.cmd == "actions":
        return {"event": admin.actions_event, "sweep": admin.actions_sweep, "setup": admin.setup,
                "settle": admin.actions_settle}[args.what](root)
    if args.cmd == "setup":
        return admin.setup(root)
    if args.cmd == "harden":
        return admin.harden(root, check=args.check)
    if args.cmd == "job":
        return admin.submit_job(root, args.title, Path(args.body_file).read_text(), args.priority)
    if args.cmd == "plan-check":
        return W.cmd_plan_check(type("X", (), {"cfg": C.load(root)})(), args)
    if args.cmd == "doctor":
        cfg_repo = args.repo or W.detect_repo(root)
        try:
            ctx = W.Ctx(root, repo=args.repo) if cfg_repo or C.load(root)["repo"] else None
        except (SystemExit, Exception):  # doctor reports problems instead of failing on them
            ctx = None
        return W.cmd_doctor(ctx, args, root)

    ctx = W.Ctx(root, repo=args.repo)
    from . import driver as DRV
    if args.cmd == "tools":
        from .toolbox import cmd_tools
        args.paths = _tool_flags(args, list(args.paths))
        return cmd_tools(ctx, args, W.HOME)
    if args.cmd in ("finding", "findings"):
        return {"finding": DRV.cmd_finding, "findings": DRV.cmd_findings}[args.cmd](ctx, args, W.HOME)
    if args.cmd in ("keygen", "relay", "logs"):
        if args.cmd == "logs" and args.what in ("note", "explain"):
            args.sig = args.sig or args.file
        return {"keygen": DRV.cmd_keygen, "relay": DRV.cmd_relay, "logs": DRV.cmd_logs}[args.cmd](ctx, args, W.HOME)
    if getattr(ctx, "guest", False):
        blocked = _guest_blocked(ctx, args)
        if blocked:
            print(f"ws: {blocked}", file=sys.stderr)
            return 4
    github_ref = "#" in str(getattr(args, "ref", "") or "")  # plans and written results stay on the issues queue
    if ctx.cfg["driver"] == "nostr" and args.cmd in ("next", "claim", "show", "beat", "submit", "verify") \
            and not (github_ref and args.cmd != "verify"):
        if args.cmd == "beat" and not args.ref:
            print("ws: with the relay driver, say which task: ws beat REF", file=sys.stderr)
            return 1
        return getattr(DRV, f"cmd_{args.cmd}")(ctx, args, W.HOME)
    if args.cmd == "verify":
        print("ws: verify is part of the relay driver (driver: nostr); use reviews with github-issues", file=sys.stderr)
        return 1
    table = {
        "consent": W.cmd_consent, "join": W.cmd_join, "next": W.cmd_next, "claim": W.cmd_claim,
        "show": W.cmd_show, "beat": W.cmd_beat, "checkpoint": W.cmd_checkpoint, "submit": W.cmd_submit,
        "found": W.cmd_found, "status": W.cmd_status, "acct": W.cmd_acct, "queue": W.cmd_queue,
        "nodes": W.cmd_nodes, "prio": W.cmd_prio, "stop": W.cmd_stop, "learnings": W.cmd_learnings, "flag": W.cmd_flag,
    }
    if args.cmd in table:
        return table[args.cmd](ctx, args)
    return W.cmd_simple(ctx, args, args.cmd)


if __name__ == "__main__":
    sys.exit(main())

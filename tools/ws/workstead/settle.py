"""The GitHub settlement gate for the relay driver, and the batched board.

GitHub is the settlement layer: a relay task's result reaches the default
branch only through a pull request, and only when

1. its task was published by an allowlisted key (``relay.publishers``);
2. ``quorum`` distinct trusted keys signed matching VerificationReceipts
   (same task, base, output commit, patch hash, verdict pass);
3. the verified output commit **is** the pull request's head commit;
4. CI is green on that commit (the clean-runner validation: the unprivileged
   ``pull_request`` CI run applies and tests the patch);
5. the queue's maintainer rules pass (protected paths, supply-chain guard,
   size, safety-net flags).

This module runs privileged (``workflow_run`` and schedule) and never checks
out or runs pull request code. When a result is merged and a gate key is
configured (secret ``WS_NOSTR_KEY``, whose pubkey is a publisher), it
publishes a TaskSettled event so agents see the outcome without GitHub.

The board is rebuilt from relay state by the 15-minute sweep: one batched
edit, never one API call per agent action.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from . import nostr as N
from .engine import Queue
from .protocol import L, now_iso, read_block

MARK = "<!-- ws:settle "


def settle_pr(q: Queue, relays, n: int, events: Optional[List[Dict[str, Any]]] = None,
              gate_key: Optional[bytes] = None) -> Dict[str, Any]:
    cfg, gh = q.cfg, q.gh
    repo_url = f"https://github.com/{gh.repo}"
    pr = gh.get_pr(n)
    meta = read_block(pr["body"], "relay") or {}
    tid = meta.get("task")
    out = {"pr": n, "task": tid, "decision": "ignored", "reason": "not a relay task pull request"}
    if not isinstance(tid, str) or not N.HEX64.match(tid) or pr["state"] != "open":
        return out
    events = events if events is not None else relays.snapshot(repo_url)
    row = N.compute(events, cfg, repo_url, int(q.now())).get(tid)
    if row is None:
        return _flag(q, n, out, "rejected", "its task isn't published by an allowlisted key (or has expired)")
    if row["status"] == "merged":
        return _flag(q, n, out, "rejected", "the task is already settled by another pull request")
    if row["status"] != "verified":
        out.update(decision="waiting", reason=f"quorum not reached ({row['votes']} of {row['quorum']} trusted receipts)")
        return out
    if row["output"] != pr["head_sha"]:
        return _flag(q, n, out, "rejected",
                     f"the verified result is commit {row['output'][:12]}, but this pull request's head is "
                     f"{pr['head_sha'][:12]}: push exactly the verified commit")
    if cfg["review"]["require_checks"]:
        checks = gh.checks_state(pr["head_sha"])
        if checks != "success":
            out.update(decision="waiting" if checks in ("pending", "none") else "needs-maintainer",
                       reason=f"CI is {checks}")
            if checks == "failure":
                return _flag(q, n, out, "needs-maintainer", "verified by the fleet, but CI failed on a clean runner")
            return out
    from . import contract as K
    c = K.contract(cfg, row.get("spec") or {})
    if c["forbid"] or c["frozen"]:
        problems = K.violations(gh.pr_files(n), gh.pr_patches(n), c)
        if problems:
            return _flag(q, n, out, "rejected", "it breaks the verification contract: " + "; ".join(problems[:5]))
    why = q.needs_maintainer(n, pr)
    if why:
        return _flag(q, n, out, "needs-maintainer", why)
    if cfg["review"]["merge"] == "maintainer":
        if L["ready_to_merge"] not in pr["labels"]:
            gh.add_labels(n, [L["ready_to_merge"]])
        out.update(decision="ready", reason="verified, green; a maintainer merges")
        return out
    if not gh.merge_pr(n, pr["head_sha"], cfg["review"]["merge_method"]):
        out.update(decision="waiting", reason="GitHub refused the merge (conflict or branch protection); will retry")
        return out
    out.update(decision="merged", reason=f"quorum {row['votes']}/{row['quorum']}, CI green")
    try:
        _record_evidence(q, n, tid, row, c)
    except Exception as e:  # the merge stands; evidence is a courtesy record
        out["note"] = f"evidence not recorded: {e}"
    issue = (row.get("spec") or {}).get("issue")
    if isinstance(issue, int) and issue != n:
        from .bridge import close_settled
        try:
            close_settled(q, issue, f"{repo_url}/pull/{n}")
            out["issue"] = issue
        except Exception as e:  # the merge stands; the sweep reconciles the issue later
            out["note"] = f"issue #{issue} not closed: {e}"
    if gate_key:
        try:
            relays.publish(N.settled_event(gate_key, tid, repo_url, "merged", int(q.now()), output=pr["head_sha"],
                                           pr=f"{repo_url}/pull/{n}"))
        except Exception as e:  # the merge stands; agents still see quorum
            out["note"] = f"TaskSettled not published: {e}"
    return out


def audit_sampled(task_id: str, rate: float) -> bool:
    """Deterministic, unpredictable before the task exists: hash of the task id."""
    import hashlib
    return int(hashlib.sha256(("audit:" + task_id).encode()).hexdigest()[:8], 16) / 2 ** 32 < float(rate)


def _record_evidence(q: Queue, n: int, tid: str, row: Dict[str, Any], c: Dict[str, List[str]]) -> None:
    """State what this result's verification consists of, claim by claim, and sample it for audit."""
    cfg, gh = q.cfg, q.gh
    lines = [f"- **Reviewed by agents:** {row['votes']} independent trusted verifiers re-ran the checks on this exact "
             f"commit" + (f" (model families: {', '.join(row.get('models') or [])})" if row.get("models") else "")]
    if cfg["review"]["require_checks"]:
        lines.append("- **Machine-checked:** CI passed on a clean runner"
                     + (f" with the `{cfg['verify']['profile']}` contract" if cfg["verify"]["profile"] else ""))
    if c["frozen"] or c["forbid"] or c["must_fail"]:
        lines.append(f"- **Contract:** {len(c['frozen'])} frozen paths untouched, {len(c['forbid'])} forbidden "
                     f"patterns absent, {len(c['must_fail'])} controls still fail")
    sampled = audit_sampled(tid, cfg["review"]["audit_rate"])
    if sampled:
        lines.append(f"- **Audited by sampling:** picked for a human audit ({cfg['review']['audit_rate']:.0%} of "
                     "results). A maintainer checks it, then swaps `ws:audit` for `ws:audit-pass` or `ws:audit-fail` "
                     "(with a comment saying why): the counts are the fleet's measured error rate.")
        gh.add_labels(n, [L["audit"]])
    else:
        lines.append("- **Reviewed by a person:** not yet (not in this audit sample)")
    gh.create_comment(n, "**Settled.** What this result's verification consists of:\n\n" + "\n".join(lines))


def _flag(q: Queue, n: int, out: Dict[str, Any], decision: str, reason: str) -> Dict[str, Any]:
    out.update(decision=decision, reason=reason)
    gh = q.gh
    if L["needs_maintainer"] not in gh.get_pr(n)["labels"] and decision == "needs-maintainer":
        gh.add_labels(n, [L["needs_maintainer"]])
    marker = f"{MARK}{json.dumps({'decision': decision, 'reason': reason})} -->"
    if not any(marker in c["body"] for c in gh.list_comments(n) if q.is_bot(c["author"])):
        gh.create_comment(n, f"**Workstead settlement:** {decision}: {reason}.\n\n{marker}")
    return out


def settle_open(q: Queue, relays, gate_key: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """Every open relay pull request, against one snapshot of the relays."""
    repo_url = f"https://github.com/{q.gh.repo}"
    events = relays.snapshot(repo_url)
    res = []
    for i in q.gh.list_issues(include_prs=True):
        if i["is_pr"] and "ws:relay" in (i["body"] or ""):
            res.append(settle_pr(q, relays, i["number"], events, gate_key))
    return res


def gate_key_from_env() -> Optional[bytes]:
    k = os.environ.get("WS_NOSTR_KEY", "").strip()
    if not k:
        return None
    if k.startswith("nsec1"):
        from . import schnorr
        hrp, data = schnorr.bech32_decode(k)
        return data if hrp == "nsec" else None
    return bytes.fromhex(k)


def relay_board(events: List[Dict[str, Any]], cfg: Dict[str, Any], repo_url: str, now: int) -> Dict[str, Any]:
    from .mesh import live_supers
    state = N.compute(events, cfg, repo_url, now)
    _, trusted = N.trust_sets(events, cfg)
    counts: Dict[str, int] = {}
    for r in state.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    supers = live_supers(events, repo_url, cfg, now, trusted)
    return {"generated_at": now_iso(now), "counts": counts, "super_peers": len(supers),
            "tasks": sorted(state.values(), key=lambda r: (-r["priority"], r["created_at"]))[:200],
            "agents": len({e["pubkey"] for e in events if e["kind"] in (N.K_CLAIM, N.K_RECEIPT)})}


def render_relay_board(b: Dict[str, Any]) -> str:
    c = b["counts"]
    order = ["open", "leased", "submitted", "verified", "merged", "pending", "cancelled", "rejected"]
    line = " · ".join(f"{k} **{c[k]}**" for k in order if c.get(k))
    rows = "\n".join(f"| `{r['id'][:10]}` | {r['spec'].get('title', '')[:60]} | {r['status']} | "
                     f"{r['votes']}/{r['quorum']} |" for r in b["tasks"][:30])
    return (f"### Relay queue (driver: nostr)\n\n{line or 'no relay tasks yet'}\n\n"
            f"{b['agents']} agents seen · {b['super_peers']} super-peers in the mesh\n\n"
            + ("| Task | Title | Status | Quorum |\n|---|---|---|---|\n" + rows if rows else "")
            + f"\n\n<sub>Rebuilt from the relays at {b['generated_at']}.</sub>")

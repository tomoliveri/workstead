"""The bridge from GitHub intake to the relay queue (driver: nostr).

Only tasks delivered as pull requests are bridged; plans (a Job's
breakdown), written results and reviews stay on the issues queue, where
`ws next` also looks when the relays have nothing for an agent.

People still ask for work the GitHub way: a Job issue, ``/plan``, a found
issue, a proposal a maintainer accepts. The queue engine turns those into
task issues and works out dependencies. When a task issue is ready (queued,
dependencies done), the bridge publishes it as a signed TaskPublished event
with the repository's gate key (secret ``WS_NOSTR_KEY``, a publisher) and
notes the task id on the issue. From then on agents claim, produce and
verify over the relays, with no GitHub API calls. When the settlement gate
merges the verified result, it closes the issue, the engine marks it done,
and dependents become ready and are bridged in turn.

Idempotent: an issue carrying the bridge's marker is never published twice.
Runs privileged (the queue and sweep workflows) and never runs pull request
code.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import config as C, nostr as N
from .protocol import L

MARK = "<!-- ws:relay-task "


def task_ref(q, n: int) -> Optional[str]:
    """The relay task id already published for issue n, if any."""
    for c in q.gh.list_comments(n):
        if q.is_bot(c["author"]) and MARK in c["body"]:
            try:
                return json.loads(c["body"].split(MARK, 1)[1].split(" -->", 1)[0])["id"]
            except (ValueError, KeyError, IndexError):
                continue
    return None


def spec_for(issue: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    body = issue["body"] or ""
    body = body.split("<!-- ws:")[0].strip()  # the human part, without machine blocks
    meta = meta or {}
    return {"title": issue["title"], "body": body[:20000], "acceptance": meta.get("acceptance") or [],
            "partition": meta.get("partition") or "default", "est_hours": meta.get("est_hours") or 0,
            "issue": issue["number"]}


def publish_ready(q, relays, gate_key: bytes, limit: int = 50) -> List[Dict[str, Any]]:
    """Publish every ready task issue that isn't on the relays yet."""
    cfg, gh = q.cfg, q.gh
    repo_url = f"https://github.com/{gh.repo}"
    # Backpressure: verification capacity, not generation, is the bottleneck.
    # Don't add work while results pile up unverified.
    try:
        state = N.compute(relays.snapshot(repo_url), cfg, repo_url, int(q.now()))
        waiting = sum(1 for r in state.values() if r["status"] == "submitted")
    except Exception:  # relays unreachable: publishing will fail loudly anyway
        waiting = 0
    room = max(0, int(cfg["relay"]["max_unverified"]) - waiting)
    if room == 0:
        print(f"bridge: {waiting} results await verification; not publishing new tasks yet")
        return []
    limit = min(limit, room)
    ready = [i for i in gh.list_issues(labels=[L["task"], L["ready"]])
             if not i["is_pr"] and L["relay"] not in i["labels"] and not set(i["labels"]) & {L["running"], L["review"]}]
    base = None
    out = []
    for i in sorted(ready, key=lambda i: i["number"])[:limit]:
        n = i["number"]
        meta = q.meta_of(i)
        if meta is None:
            continue  # only tasks the queue admitted (trusted meta) are published
        part = (meta or {}).get("partition") or cfg["default_partition"]
        if C.partition(cfg, part)["deliver"] != "pr":
            continue  # plans, written results and reviews stay on the issues queue
        if task_ref(q, n):
            gh.add_labels(n, [L["relay"]])
            continue
        if base is None:
            base = gh.branch_sha(cfg["default_branch"])
        now = int(q.now())
        ev = N.task_event(gate_key, repo_url, base, spec_for(i, meta), int((meta or {}).get("priority") or 0),
                          int(cfg["relay"]["quorum"]), now, external=f"{repo_url}/issues/{n}",
                          ttl=int(cfg["relay"]["milestone_ttl_days"]) * 86400)
        relays.publish(ev)
        marker = f"{MARK}{json.dumps({'id': ev['id'], 'base': base})} -->"
        gh.create_comment(n, f"**On the relay queue** as task `{ev['id'][:12]}` (base `{base[:12]}`). "
                             "Agents claim and verify it over the relays; the settlement gate merges the verified "
                             f"result and closes this issue.\n\n{marker}")
        gh.add_labels(n, [L["relay"]])
        out.append({"issue": n, "task": ev["id"]})
    return out


def close_settled(q, n: int, pr_url: str) -> None:
    """Close a task issue whose relay result was merged; the engine then marks it done."""
    i = q.gh.get_issue(n)
    if i["state"] != "open" or i["is_pr"] or L["task"] not in i["labels"]:
        return
    q.gh.create_comment(n, f"Done: the verified result was merged in {pr_url}.")
    q.gh.edit_issue(n, state="closed", state_reason="completed")
    q.reconcile(n, "closed")

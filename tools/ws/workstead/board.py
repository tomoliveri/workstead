"""The board: a materialised view of the queue.

The sweep writes ``queue.json`` and ``BOARD.md`` to a single-commit branch
(``ws-board``) and mirrors the Markdown into a pinned issue. Workers read
``queue.json`` from raw.githubusercontent.com, which is served from a CDN and
costs no API quota. That is the read path at fleet scale; comments are the
write path.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .protocol import L, SQUEUE, now_iso, parse_ts, status_from_labels


def build_snapshot(q, items: Dict[int, Dict[str, Any]], cache: Dict[int, Dict[str, Any]], now: float) -> Dict[str, Any]:
    tasks: List[Dict[str, Any]] = []
    reviews: List[Dict[str, Any]] = []
    nodes: Dict[str, Dict[str, Any]] = {}
    for n, i in sorted(items.items()):
        c = cache.get(n) or {}
        if i["is_pr"]:
            if L["needs_review"] in i["labels"] or c.get("review_lease"):
                reviews.append({"number": n, "kind": "pr", "title": i["title"], "author": i["author"],
                                "review_lease": c.get("review_lease"), "created_at": i["created_at"],
                                "labels": i["labels"]})
            continue
        meta = q.meta_of(i) or {}
        status = c.get("status") or status_from_labels(i["labels"]) or "queued"
        row = {"number": n, "title": i["title"], "status": status,
               "code": SQUEUE.get(status, "?"), "reason": c.get("reason", ""),
               "ready": L["ready"] in i["labels"], "author": i["author"],
               "created_at": i["created_at"], "labels": i["labels"], "meta": meta,
               "lease": c.get("lease"), "review_lease": c.get("review_lease"),
               "pr": c.get("pr"), "attempts": c.get("attempts", 0), "children": c.get("children", [])}
        tasks.append(row)
        if status in ("review", "triage") and not c.get("pr"):
            reviews.append({"number": n, "kind": "triage" if status == "triage" else "issue", "title": i["title"],
                            "author": i["author"], "review_lease": c.get("review_lease"),
                            "created_at": i["created_at"], "labels": i["labels"]})
        for key in ("lease", "review_lease"):
            lease = c.get(key)
            if lease:
                nd = nodes.setdefault(lease["login"], {"login": lease["login"], "nodes": [], "leases": 0, "since": lease["since"]})
                nd["leases"] += 1
                if lease.get("node") and lease["node"] not in nd["nodes"]:
                    nd["nodes"].append(lease["node"])
    for r in reviews:
        lease = r.get("review_lease")
        if lease and r["kind"] == "pr":
            nd = nodes.setdefault(lease["login"], {"login": lease["login"], "nodes": [], "leases": 0, "since": lease["since"]})
            nd["leases"] += 1

    try:
        done = q.gh.list_issues(labels=[L["done"]], state="closed", since=now_iso(now - 7 * 86400), limit=2000)
    except Exception:  # the board must never fail the sweep
        done = []
    day = [d for d in done if parse_ts(d.get("closed_at")) >= now - 86400]
    by_status: Dict[str, int] = {}
    by_part: Dict[str, int] = {}
    for t in tasks:
        key = t["status"] if t["status"] != "queued" else ("ready" if t["ready"] else "pending")
        by_status[key] = by_status.get(key, 0) + 1
        p = (t["meta"] or {}).get("partition", "?")
        by_part[p] = by_part.get(p, 0) + 1
    remaining = sum(float((t["meta"] or {}).get("est_hours", 1)) for t in tasks if t["status"] in ("queued", "running", "review", "triage"))
    per_day = len(done) / 7.0
    open_work = [t for t in tasks if t["status"] in ("queued", "running", "review", "split", "triage")]
    held = [t for t in tasks if t["status"] == "held"]
    return {
        "v": 1, "generated_at": now_iso(now), "repo": q.gh.repo,
        "tasks": tasks, "reviews": reviews, "nodes": sorted(nodes.values(), key=lambda x: -x["leases"]),
        "stats": {"by_status": by_status, "by_partition": by_part, "done_24h": len(day), "done_7d": len(done),
                  "tasks_per_day": round(per_day, 1), "remaining_est_hours": round(remaining, 1),
                  "open_tasks": len(open_work), "eta_days": round(len(open_work) / per_day, 1) if per_day else None},
        "finished": not open_work and not held and not reviews,
        "held_only": not open_work and bool(held),
    }


def _age(ts: str, now: float) -> str:
    h = max(0.0, (now - parse_ts(ts)) / 3600)
    return f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} d"


def render_board(snap: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    now = parse_ts(snap["generated_at"])
    st = snap["stats"]
    name = cfg["project"].get("name") or snap["repo"]
    lines = [f"## {name}: fleet board", ""]
    if snap["finished"]:
        lines += ["**The project is finished: no open work.** Workers stop by themselves. Open a new job to start more.", ""]
    elif snap["held_only"]:
        lines += ["**Only held tasks remain.** A maintainer needs to look at them (see below).", ""]
    counts = " · ".join(f"{k} **{v}**" for k, v in sorted(st["by_status"].items())) or "no open tasks"
    eta = f" · ETA about {st['eta_days']} days at the current rate" if st.get("eta_days") else ""
    lines += [f"{counts}", "",
              f"Completed: {st['done_24h']} in the last day, {st['done_7d']} in the last week "
              f"({st['tasks_per_day']}/day). Remaining estimate: {st['remaining_est_hours']} agent-hours{eta}.", ""]
    lines += ["### Join", "", "Paste this into an AI coding agent (Claude Code, Codex, Cursor, Aider…):", "",
              "```text",
              f"Read https://raw.githubusercontent.com/{snap['repo']}/{cfg['default_branch']}/docs/JOIN.md and follow it to "
              "contribute to this project. Ask me once, up front, then keep working without asking until "
              "the project is finished or I say stop.",
              "```", ""]

    def table(rows, head):
        if not rows:
            return ["_none_", ""]
        return [head, "|" + "---|" * (head.count("|") - 1)] + rows + [""]

    tasks = snap["tasks"]
    running = [t for t in tasks if t["status"] == "running"]
    lines += [f"### Running ({len(running)})", ""]
    lines += table([f"| #{t['number']} | {t['title'][:60]} | @{t['lease']['login']} | `{t['lease'].get('node') or '-'}` | "
                    f"{_age(t['lease']['since'], now)} |" for t in running[:40] if t.get("lease")],
                   "| Task | Title | Worker | Node | Age |")
    ready = sorted([t for t in tasks if t["status"] == "queued" and t["ready"]],
                   key=lambda t: (-(t["meta"] or {}).get("priority", 3), t["number"]))
    lines += [f"### Ready ({len(ready)})", ""]
    lines += table([f"| #{t['number']} | {t['title'][:60]} | `{(t['meta'] or {}).get('partition', '?')}` | "
                    f"{(t['meta'] or {}).get('priority', 3)} | {(t['meta'] or {}).get('est_hours', 1)} |" for t in ready[:25]],
                   "| Task | Title | Partition | Priority | Est h |")
    rv = snap["reviews"]
    lines += [f"### Waiting for review or triage ({len(rv)})", ""]
    lines += table([f"| #{r['number']} | {r['kind']} | {r['title'][:60]} | @{r['author']} | "
                    f"{'@' + r['review_lease']['login'] if r.get('review_lease') else 'unclaimed'} |" for r in rv[:25]],
                   "| Item | Kind | Title | Author | Reviewer |")
    held = [t for t in tasks if t["status"] == "held"]
    lines += [f"### Held: needs a maintainer ({len(held)})", ""]
    lines += table([f"| #{t['number']} | {t['title'][:60]} | {t['reason'][:80]} |" for t in held[:25]],
                   "| Task | Title | Reason |")
    lines += [f"### Workers (sinfo, {len(snap['nodes'])} accounts)", ""]
    lines += table([f"| @{n['login']} | {len(n['nodes']) or 1} | {n['leases']} |" for n in snap["nodes"][:40]],
                   "| Account | Nodes | Leases |")
    fl = snap.get("fleet_learnings")
    if fl:
        sig = ", ".join(s["topic"] for s in fl.get("signals", [])) or "none"
        lines += ["### Fleet learnings", "", f"{fl['fleets']} downstream fleets reporting. Shared problems: {sig}.", ""]
    if snap.get("learnings_topics"):
        lines += [f"This fleet's weak spots (from its own statistics): {', '.join(snap['learnings_topics'])}.", ""]
    lines.append(f"<sub>Generated {snap['generated_at']}. Machine-readable: `queue.json` on the "
                 f"`{cfg['board']['branch']}` branch.</sub>")
    return "\n".join(lines)

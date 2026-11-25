"""Fleet learnings: what downstream fleets teach upstream Workstead.

Every fleet measures how the queue works for it: how often leases expire,
how good estimates are, why work comes back for rework, what gets held. When
give-back is on, each give-back turn carries the fleet's latest report to the
upstream repository, which aggregates the reports across fleets and turns
widespread problems into upstream tasks. Give-back workers then prefer the
upstream tasks that address their own fleet's weakest spots.

Privacy is by construction. A report contains only the numbers and fixed
categories in SCHEMA: no repository names, logins, titles, bodies or code.
The project is identified by a random id generated once per fleet. Upstream
rejects anything outside the schema. `ws learnings` prints exactly what a
fleet would send.
"""
from __future__ import annotations

import re
import statistics
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .protocol import now_iso, parse_ts

REPORT_VERSION = 1
WINDOW_DAYS = 28

# metric -> (min, max). Rates are 0..1; counts and hours are bounded.
SCHEMA: Dict[str, Tuple[float, float]] = {
    "tasks_created": (0, 1e6),
    "tasks_completed": (0, 1e6),
    "leases_started": (0, 1e6),
    "requeues": (0, 1e6),
    "expiry_rate": (0, 1),             # requeues / leases started
    "held": (0, 1e6),
    "held_rate": (0, 1),               # held / tasks created
    "rework": (0, 1e6),
    "rework_rate": (0, 1),             # rework rounds / submissions
    "rework_review_share": (0, 1),     # of rework: reviewer asked
    "rework_checks_share": (0, 1),     # of rework: CI failed
    "rework_conflict_share": (0, 1),   # of rework: merge conflicts
    "est_ratio_median": (0, 100),      # actual lease hours / estimate
    "lease_hours_median": (0, 1000),
    "plans": (0, 1e6),
    "plan_children_median": (0, 1000),
    "proposals": (0, 1e6),
    "proposal_duplicate_share": (0, 1),
    "proposal_rejected_share": (0, 1),
    "tasks_per_day": (0, 1e5),
    "accounts_max": (0, 1e6),
    "nodes_max": (0, 1e6),
    "api_calls_per_task": (0, 1e5),
}
HELD_REASONS = ("MaxAttempts", "DependencyNeverSatisfied", "MaxReviewRounds", "ChildNotCompleted", "Manual", "Other")
PARTITION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CONFIG_KEYS = {
    "heartbeat_minutes": (10, 10000), "max_attempts": (1, 100), "approvals_required": (0, 10),
    "time_factor": (1, 100), "top_k": (1, 1000), "max_per_account": (1, 10000),
}
CONFIG_ENUMS = {"merge": ("fleet", "maintainer"), "trust": ("open", "earned"), "validate": ("set", "unset")}

# Fleet-wide signals: when the median across fleets crosses a threshold, the
# upstream queue opens a task on that topic.
SIGNALS = [
    # topic, metric, direction, threshold, title
    ("leases", "expiry_rate", ">", 0.15, "Lease expiry rate is high across fleets"),
    ("estimation", "est_ratio_median", ">", 1.6, "Tasks take much longer than their estimates"),
    ("estimation", "est_ratio_median", "<", 0.4, "Tasks finish far faster than their estimates"),
    ("review", "rework_review_share", ">", 0.6, "Most rework comes from review requests"),
    ("validation", "rework_checks_share", ">", 0.4, "Much rework comes from failing checks"),
    ("conflicts", "rework_conflict_share", ">", 0.2, "Merge conflicts cause frequent rework"),
    ("held", "held_rate", ">", 0.05, "Many tasks end up held for a human"),
    ("triage", "proposal_duplicate_share", ">", 0.3, "Many proposals are duplicates"),
    ("planning", "plan_children_median", ">", 30, "Plans are very large"),
]
TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


# --- downstream: counting ------------------------------------------------------

def new_state() -> Dict[str, Any]:
    return {"project_id": uuid.uuid4().hex[:16], "days": {}, "samples": {"est_ratio": [], "lease_hours": [],
                                                                      "plan_children": []}}


def _bucket(learn: Dict[str, Any], now: float) -> Dict[str, Any]:
    day = now_iso(now)[:10]
    days = learn.setdefault("days", {})
    b = days.setdefault(day, {})
    for old in sorted(days)[:-WINDOW_DAYS]:
        del days[old]
    return b


def _inc(b: Dict[str, Any], key: str, by: float = 1) -> None:
    b[key] = b.get(key, 0) + by


def _sample(learn: Dict[str, Any], key: str, value: float, keep: int = 400) -> None:
    s = learn.setdefault("samples", {}).setdefault(key, [])
    s.append(round(float(value), 3))
    del s[:-keep]


def held_category(reason: str) -> str:
    for r in HELD_REASONS[:-2]:
        if reason.startswith(r):
            return r
    return "Manual" if reason.startswith("Held:") else "Other"


def observe(learn: Dict[str, Any], prev: Dict[int, Dict[str, Any]], cur: Dict[int, Dict[str, Any]],
            items: Dict[int, Dict[str, Any]], metas: Dict[int, Dict[str, Any]], now: float,
            done_since_last: int, nodes: Tuple[int, int]) -> None:
    """Count what changed between two sweeps. Called by the sweep."""
    b = _bucket(learn, now)
    _inc(b, "tasks_completed", done_since_last)
    b["accounts_max"] = max(b.get("accounts_max", 0), nodes[0])
    b["nodes_max"] = max(b.get("nodes_max", 0), nodes[1])
    for n, c in cur.items():
        if items.get(n, {}).get("is_pr"):
            p = prev.get(n) or {}
            reason = c.get("reason") or ""
            if reason != p.get("reason"):
                if reason.startswith("checks failed"):
                    _inc(b, "rework_checks")
                elif reason == "conflict":
                    _inc(b, "rework_conflict")
            continue
        p = prev.get(n)
        st, pst = c.get("status"), (p or {}).get("status")
        meta = metas.get(n) or {}
        if p is None:
            _inc(b, "tasks_created")
            if st == "triage":
                _inc(b, "proposals")
        if st == "running" and pst in (None, "queued", "held"):
            _inc(b, "leases_started")
        if pst == "running" and st == "queued" and c.get("attempts", 0) > (p or {}).get("attempts", 0):
            _inc(b, "requeues")
        if pst == "running" and st in ("review", "split"):
            lease = (p or {}).get("lease") or {}
            if lease.get("since"):
                hours = max(0.0, (now - parse_ts(lease["since"])) / 3600)
                _sample(learn, "lease_hours", hours)
                if meta.get("est_hours"):
                    _sample(learn, "est_ratio", hours / float(meta["est_hours"]))
            _inc(b, "submissions")
        if pst == "review" and st == "running":
            _inc(b, "rework")
        if st == "split" and pst != "split":
            _inc(b, "plans")
            _sample(learn, "plan_children", len(c.get("children") or []))
        if st == "held" and pst != "held":
            _inc(b, "held")
            _inc(b, "held_" + held_category(c.get("reason") or ""))
        if pst == "triage" and st != "triage":
            reason = c.get("reason") or ""
            if reason.startswith("duplicate"):
                _inc(b, "proposal_duplicates")
            elif reason.startswith("rejected"):
                _inc(b, "proposal_rejected")


# --- downstream: the report ---------------------------------------------------

def _median(xs: List[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def _rate(a: float, b: float) -> Optional[float]:
    return round(min(1.0, a / b), 3) if b else None


def report(learn: Dict[str, Any], cfg: Dict[str, Any], version: str, private: bool) -> Dict[str, Any]:
    days = learn.get("days", {})
    tot: Dict[str, float] = {}
    for b in days.values():
        for k, v in b.items():
            if k.endswith("_max"):
                tot[k] = max(tot.get(k, 0), v)
            else:
                tot[k] = tot.get(k, 0) + v
    g = tot.get
    rework = g("rework", 0)
    review_rework = max(0.0, rework - g("rework_checks", 0) - g("rework_conflict", 0))
    s = learn.get("samples", {})
    metrics = {
        "tasks_created": g("tasks_created", 0), "tasks_completed": g("tasks_completed", 0),
        "leases_started": g("leases_started", 0), "requeues": g("requeues", 0),
        "expiry_rate": _rate(g("requeues", 0), g("leases_started", 0)),
        "held": g("held", 0), "held_rate": _rate(g("held", 0), g("tasks_created", 0)),
        "rework": rework, "rework_rate": _rate(rework, g("submissions", 0)),
        "rework_review_share": _rate(review_rework, rework),
        "rework_checks_share": _rate(g("rework_checks", 0), rework),
        "rework_conflict_share": _rate(g("rework_conflict", 0), rework),
        "est_ratio_median": _median(s.get("est_ratio", [])),
        "lease_hours_median": _median(s.get("lease_hours", [])),
        "plans": g("plans", 0), "plan_children_median": _median(s.get("plan_children", [])),
        "proposals": g("proposals", 0),
        "proposal_duplicate_share": _rate(g("proposal_duplicates", 0), g("proposals", 0)),
        "proposal_rejected_share": _rate(g("proposal_rejected", 0), g("proposals", 0)),
        "tasks_per_day": round(g("tasks_completed", 0) / max(1, len(days)), 2),
        "accounts_max": g("accounts_max", 0), "nodes_max": g("nodes_max", 0),
    }
    held = {r: g("held_" + r, 0) for r in HELD_REASONS if g("held_" + r, 0)}
    trust = cfg["trust"]["mode"] if cfg["trust"]["mode"] != "auto" else ("open" if private else "earned")
    out = {
        "v": REPORT_VERSION, "project": learn.get("project_id") or "", "workstead": version,
        "window_days": len(days), "generated_at": now_iso(),
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "held_reasons": held,
        "config": {"heartbeat_minutes": cfg["leases"]["heartbeat_minutes"], "max_attempts": cfg["leases"]["max_attempts"],
                   "approvals_required": cfg["review"]["approvals_required"], "time_factor": cfg["leases"]["time_factor"],
                   "top_k": cfg["scheduler"]["top_k"], "max_per_account": cfg["leases"]["max_per_account"],
                   "merge": cfg["review"]["merge"], "trust": trust,
                   "validate": "set" if cfg["validate"]["command"] else "unset"},
    }
    out["topics"] = [t for t, _ in own_signals(out)]
    cleaned, errors = validate(out)
    if errors:
        raise ValueError("internal: report failed its own schema: " + "; ".join(errors))
    return cleaned


def own_signals(rep: Dict[str, Any]) -> List[Tuple[str, str]]:
    """This fleet's own weak spots, as (topic, title), worst first."""
    m = rep.get("metrics", {})
    out = []
    for topic, metric, op, th, title in SIGNALS:
        v = m.get(metric)
        if v is not None and ((op == ">" and v > th) or (op == "<" and v < th)) and topic not in [t for t, _ in out]:
            out.append((topic, title))
    return out


# --- validation (upstream trusts nothing) ------------------------------------

def validate(obj: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Return (cleaned report, errors). Anything outside the schema is an error."""
    errs: List[str] = []
    if not isinstance(obj, dict):
        return {}, ["the report must be a JSON object"]
    allowed = {"v", "project", "workstead", "window_days", "generated_at", "metrics", "held_reasons", "config", "topics"}
    extra = set(obj) - allowed
    if extra:
        errs.append("unknown fields: " + ", ".join(sorted(extra)))
    if obj.get("v") != REPORT_VERSION:
        errs.append("unsupported version")
    if not isinstance(obj.get("project"), str) or not re.fullmatch(r"[0-9a-f]{16}", obj.get("project", "")):
        errs.append("project must be a 16-character hex id")
    if not isinstance(obj.get("workstead"), str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", obj.get("workstead", "")):
        errs.append("workstead must be a version like 1.0.0")
    wd = obj.get("window_days")
    if not isinstance(wd, int) or isinstance(wd, bool) or not 0 <= wd <= WINDOW_DAYS:
        errs.append("window_days out of range")
    if not isinstance(obj.get("generated_at"), str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", obj.get("generated_at", "")):
        errs.append("generated_at must be an ISO timestamp")
    metrics = obj.get("metrics")
    clean_m: Dict[str, float] = {}
    if not isinstance(metrics, dict):
        errs.append("metrics must be an object")
    else:
        for k, v in metrics.items():
            if k not in SCHEMA:
                errs.append(f"unknown metric {k!r}")
            elif not isinstance(v, (int, float)) or isinstance(v, bool) or not SCHEMA[k][0] <= v <= SCHEMA[k][1]:
                errs.append(f"metric {k} out of range")
            else:
                clean_m[k] = v
    held = obj.get("held_reasons", {})
    if not isinstance(held, dict) or any(k not in HELD_REASONS or not isinstance(v, (int, float)) or isinstance(v, bool)
                                         or not 0 <= v <= 1e6 for k, v in held.items()):
        errs.append("held_reasons must map known reasons to counts")
    conf = obj.get("config", {})
    if not isinstance(conf, dict):
        errs.append("config must be an object")
        conf = {}
    for k, v in conf.items():
        if k in CONFIG_KEYS:
            lo, hi = CONFIG_KEYS[k]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                errs.append(f"config {k} out of range")
        elif k in CONFIG_ENUMS:
            if v not in CONFIG_ENUMS[k]:
                errs.append(f"config {k} must be one of {', '.join(CONFIG_ENUMS[k])}")
        else:
            errs.append(f"unknown config key {k!r}")
    topics = obj.get("topics", [])
    if not isinstance(topics, list) or len(topics) > 20 or not all(isinstance(t, str) and TOPIC_RE.match(t) for t in topics):
        errs.append("topics must be a short list of slugs")
    if errs:
        return {}, errs
    return {"v": REPORT_VERSION, "project": obj["project"], "workstead": obj["workstead"], "window_days": wd,
            "generated_at": obj["generated_at"], "metrics": clean_m, "held_reasons": dict(held),
            "config": dict(conf), "topics": list(topics)}, []


# --- upstream: aggregation ----------------------------------------------------

def aggregate(reports: List[Dict[str, Any]], now: float, max_age_days: int = 90) -> Dict[str, Any]:
    """Latest valid report per project, then per-metric medians across fleets."""
    latest: Dict[str, Dict[str, Any]] = {}
    for r in reports:
        if now - parse_ts(r["generated_at"]) > max_age_days * 86400:
            continue
        cur = latest.get(r["project"])
        if cur is None or r["generated_at"] > cur["generated_at"]:
            latest[r["project"]] = r
    fleets = list(latest.values())
    metrics: Dict[str, Dict[str, float]] = {}
    for k in SCHEMA:
        vals = [f["metrics"][k] for f in fleets if k in f["metrics"]]
        if vals:
            vals.sort()
            metrics[k] = {"n": len(vals), "median": round(statistics.median(vals), 3),
                          "p75": round(vals[min(len(vals) - 1, int(0.75 * len(vals)))], 3)}
    topics: Dict[str, int] = {}
    for f in fleets:
        for t in f.get("topics", []):
            topics[t] = topics.get(t, 0) + 1
    versions: Dict[str, int] = {}
    for f in fleets:
        versions[f["workstead"]] = versions.get(f["workstead"], 0) + 1
    return {"fleets": len(fleets), "metrics": metrics, "topics": topics, "versions": versions,
            "generated_at": now_iso(now)}


def fleet_signals(agg: Dict[str, Any], min_fleets: int = 3) -> List[Dict[str, Any]]:
    """Topics worth an upstream task: the median across enough fleets crosses a threshold."""
    out = []
    for topic, metric, op, th, title in SIGNALS:
        m = agg["metrics"].get(metric)
        if not m or m["n"] < min_fleets:
            continue
        v = m["median"]
        if (op == ">" and v > th) or (op == "<" and v < th):
            if topic not in [o["topic"] for o in out]:
                out.append({"topic": topic, "metric": metric, "median": v, "threshold": th, "op": op,
                            "fleets": m["n"], "title": title})
    return out


def signal_task_body(sig: Dict[str, Any], agg: Dict[str, Any]) -> str:
    rows = "\n".join(f"| `{k}` | {v['median']} | {v['p75']} | {v['n']} |"
                     for k, v in sorted(agg["metrics"].items()))
    return (f"Fleet learnings show a problem many Workstead fleets share: **{sig['title'].lower()}**. "
            f"The median `{sig['metric']}` across {sig['fleets']} fleets is {sig['median']} "
            f"(threshold {sig['op']} {sig['threshold']}).\n\n"
            "Find the causes in the queue, the worker client, the defaults or the docs, and fix what you can. "
            "If the fix is large, split this task with a plan. Explain in the result or pull request which "
            "metric the change should move and why.\n\n"
            f"### Aggregate metrics ({agg['fleets']} fleets, last 90 days)\n\n"
            "| Metric | Median | p75 | Fleets |\n|---|---|---|---|\n" + rows + "\n")

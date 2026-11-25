"""Scheduling: which work a worker should take next.

The pieces map onto Slurm (docs/concepts.md):

* dependencies -> ``--dependency=afterok|afterany``
* partitions and features -> ``--partition`` and ``--constraint``
* licenses -> ``--licenses``: named shared resources, one running holder each,
  used to keep tasks that touch the same files from running at once
* est_hours vs. a worker's time budget -> backfill
* multifactor priority -> ``priority/multifactor`` (priority, age, fairshare,
  plus a critical-path factor for DAGs)

Selection is client side: each worker scores the open queue itself and picks
at random among the best ``top_k``, so that a thousand workers polling at once
do not all race for the same issue.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import config as C
from .plan import dependents_count
from .protocol import parse_ts

# Statuses of a dependency as seen by deps_state: done, failed, cancelled, open.


def deps_state(meta: Dict[str, Any], status_of: Callable[[int], Optional[str]]) -> Tuple[str, List[int]]:
    """('ready'|'waiting'|'never', blocking issue numbers)."""
    waiting, dead = [], []
    for d in meta.get("deps", []):
        s = status_of(d)
        if s == "done":
            continue
        if s in ("failed", "cancelled", "missing"):
            if meta.get("after") == "any" and s != "missing":
                continue
            dead.append(d)
        else:
            waiting.append(d)
    if dead:
        return "never", dead
    return ("waiting", waiting) if waiting else ("ready", [])


def time_limit_hours(meta: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    part = C.partition(cfg, meta.get("partition") or cfg["default_partition"])
    lc = cfg["leases"]
    want = lc["time_factor"] * float(meta.get("est_hours", 1))
    return int(max(lc["min_hours"], min(float(part["max_hours"]) * lc["time_factor"], round(want + 0.49))))


def factors(item: Dict[str, Any], cfg: Dict[str, Any], now: float, crit: Dict[int, int],
            running_by_job: Dict[int, int]) -> Dict[str, float]:
    sc = cfg["scheduler"]
    w = sc["weights"]
    meta = item.get("meta") or {}
    age_h = max(0.0, (now - parse_ts(item.get("created_at"))) / 3600)
    f = {
        "priority": w["priority"] * meta.get("priority", 3) / 9,
        "age": w["age"] * min(age_h, sc["age_cap_hours"]) / sc["age_cap_hours"],
        "critical_path": w["critical_path"] * min(crit.get(item["number"], 0), sc["critical_path_cap"]),
        "fairshare": -w["fairshare"] * running_by_job.get(meta.get("job") or item["number"], 0),
    }
    if item.get("kind") in ("review", "triage"):
        f["review"] = w["review"]
    if item.get("kind") in ("rework", "resume"):
        f["rework"] = w["rework"]
    if item.get("affinity"):
        f["affinity"] = w.get("affinity", 0)
    return f


def build_graph(open_items: Iterable[Dict[str, Any]]) -> Dict[int, List[int]]:
    return {it["number"]: list((it.get("meta") or {}).get("deps", [])) for it in open_items}


def eligible(item: Dict[str, Any], worker: Dict[str, Any], held_licenses: Set[str]) -> Optional[str]:
    """None if the worker can take the item, else a Slurm-style reason."""
    meta = item.get("meta") or {}
    if item.get("kind") in ("review", "triage"):
        if not worker.get("trusted"):
            return "NotTrusted"
        if item.get("author") == worker["login"]:
            return "OwnSubmission"
        return None
    if item.get("kind") in ("rework", "resume"):
        return None
    parts = worker.get("partitions")
    if parts and meta.get("partition") not in parts:
        return "Partition"
    missing = set(meta.get("features", [])) - set(worker.get("features", []))
    if missing:
        return "Constraint(" + ",".join(sorted(missing)) + ")"
    budget = worker.get("max_hours")
    if budget and float(meta.get("est_hours", 1)) > float(budget):
        return "Backfill"
    clash = set(meta.get("licenses", [])) & held_licenses
    if clash:
        return "Licenses(" + ",".join(sorted(clash)) + ")"
    return None


def rank(items: List[Dict[str, Any]], open_items: List[Dict[str, Any]], worker: Dict[str, Any],
         cfg: Dict[str, Any], now: float) -> List[Tuple[float, Dict[str, Any], Dict[str, float]]]:
    """Score every eligible item, best first."""
    crit = dependents_count(build_graph(open_items), cap=cfg["scheduler"]["critical_path_cap"])
    held: Set[str] = set()
    running_by_job: Dict[int, int] = {}
    for it in open_items:
        if it.get("status") == "running":
            meta = it.get("meta") or {}
            held |= set(meta.get("licenses", []))
            job = meta.get("job") or it["number"]
            running_by_job[job] = running_by_job.get(job, 0) + 1
    out = []
    for it in items:
        why = eligible(it, worker, held)
        if why:
            it["reason"] = why
            continue
        f = factors(it, cfg, now, crit, running_by_job)
        out.append((sum(f.values()), it, f))
    out.sort(key=lambda x: (-x[0], x[1]["number"]))
    return out


def choose(ranked: List[Tuple[float, Dict[str, Any], Dict[str, float]]], cfg: Dict[str, Any],
           rng: Optional[random.Random] = None) -> Optional[Dict[str, Any]]:
    """Weighted random pick among the best top_k (rework and resumes always first)."""
    if not ranked:
        return None
    if ranked[0][1].get("kind") in ("rework", "resume"):
        return ranked[0][1]
    rng = rng or random.Random()
    best = ranked[0][0]
    # The band: everything within one priority step of the best. Randomness
    # never trades a high-priority task for a routine one, but a herd of
    # workers spreads across every equally good task instead of colliding.
    band = [t for t in ranked if best - t[0] <= cfg["scheduler"]["weights"]["priority"] / 9 + 1e-9]
    top = band[: max(int(cfg["scheduler"]["top_k"]), len(band))]
    k = max(1, int(cfg["scheduler"]["top_k"]))
    weights = [2.0 if i < k else 1.0 for i in range(len(top))]
    return rng.choices([t[1] for t in top], weights=weights, k=1)[0]

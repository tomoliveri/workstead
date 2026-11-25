"""The findings ledger: what the fleet has tried, and what happened.

In the 2026 agent-fleet results on the zeta zeros and Navier-Stokes, most
agent-hours went into dead ends (in one run, 30 of about 60 subagents), and
the method that worked asked every agent to *name the first unjustified
step* when an approach failed. Without a shared record, a thousand agents
rediscover the same dead end a thousand times.

A Finding (kind 7661, mesh tier) is a small signed note:

* ``approach``: one line describing the idea. Its fingerprint is the
  hash of the normalised text, so the same idea phrased the same way
  collapses into one entry, with a count of who reached which outcome.
* ``outcome``: ``dead-end``, ``partial``, ``counterexample``, ``works``,
  ``prior-art`` (it's already in the literature: say where) or ``open``.
* ``step``: the first unjustified step, or why it failed (one line).
* ``evidence``: a URL, commit or hash, optional.

``ws show`` and ``ws next`` put the known findings for a task in front of
the agent within a small token budget; ``ws finding`` records one;
``ws findings`` lists them. Findings never decide anything (only receipts
do), so they need no quorum; trusted and proven keys are listed first.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from . import nostr as N

OUTCOMES = ("dead-end", "partial", "counterexample", "works", "prior-art", "open")
ORDER = {"counterexample": 0, "prior-art": 1, "dead-end": 2, "partial": 3, "works": 4, "open": 5}


def fingerprint(approach: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", approach.lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def finding_event(seckey: bytes, repo_url: str, approach: str, outcome: str, step: str, created_at: int,
                  task: Optional[str] = None, evidence: str = "", pow_bits: int = 0,
                  ttl: int = 60 * 86400) -> Dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    t = [N.APP_TAG, ["r", repo_url], ["fp", fingerprint(approach)], ["outcome", outcome]]
    if task:
        t.append(["e", task])
    content = N.canonical({"approach": approach.strip()[:300], "step": step.strip()[:500],
                           "evidence": evidence.strip()[:300]})
    return N.build(seckey, N.K_FINDING, t, content, created_at, pow_bits, expires_at=created_at + ttl)


def well_formed(ev: Dict[str, Any]) -> bool:
    try:
        c = json.loads(ev["content"])
    except ValueError:
        return False
    if not isinstance(c, dict) or not isinstance(c.get("approach"), str) or not c["approach"].strip():
        return False
    task = N.tag(ev, "e")
    return (N.tag(ev, "outcome") in OUTCOMES and N.tag(ev, "fp") == fingerprint(c["approach"])
            and (task is None or bool(N.HEX64.match(task))) and len(ev["content"]) <= 1500)


def ledger(events: Iterable[Dict[str, Any]], task: Optional[str] = None,
           rank: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """One row per approach: its outcomes and counts, best-supported first."""
    rows: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev["kind"] != N.K_FINDING or (task and N.tag(ev, "e") not in (task, None)):
            continue
        c = json.loads(ev["content"])
        fp = N.tag(ev, "fp")
        r = rows.setdefault(fp, {"fp": fp, "approach": c["approach"], "outcomes": {}, "steps": [], "keys": set(),
                                 "evidence": [], "ranked": False, "task": N.tag(ev, "e")})
        if ev["pubkey"] in r["keys"]:
            continue
        r["keys"].add(ev["pubkey"])
        o = N.tag(ev, "outcome")
        r["outcomes"][o] = r["outcomes"].get(o, 0) + 1
        r["ranked"] = r["ranked"] or bool(rank and ev["pubkey"] in rank)
        if c.get("step") and c["step"] not in r["steps"]:
            r["steps"].append(c["step"])
        if c.get("evidence"):
            r["evidence"].append(c["evidence"])
    for r in rows.values():
        r["verdict"] = min(r["outcomes"], key=lambda o: (ORDER[o], -r["outcomes"][o]))
        r["keys"] = len(r["keys"])
    return sorted(rows.values(), key=lambda r: (not r["ranked"], ORDER[r["verdict"]], -r["keys"], r["fp"]))


def render(rows: List[Dict[str, Any]], budget_chars: int = 1500) -> str:
    """A compact view for an agent's context: verdict, approach, the failing step."""
    out, used = [], 0
    for r in rows:
        counts = ", ".join(f"{o} x{n}" for o, n in sorted(r["outcomes"].items(), key=lambda x: ORDER[x[0]]))
        line = f"- [{r['verdict']}] {r['approach']} ({counts})"
        if r["steps"]:
            line += f"\n  first failing step: {r['steps'][0][:200]}"
        if used + len(line) > budget_chars:
            out.append(f"- ... {len(rows) - len(out)} more: `ws findings`")
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)

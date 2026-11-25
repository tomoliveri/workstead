"""The wire protocol: labels, states, hidden blocks and comment commands.

Design rules (see docs/protocol.md):

* The comment log is the source of truth. Labels and assignees are a cached
  projection the queue keeps in step with it.
* Machine data lives in HTML comments that GitHub does not render:
  ``<!-- ws:meta {...} -->`` in a task's body (what the task is) and
  ``<!-- ws:state {...} -->`` in the queue's sticky status comment (where the
  task is in its lifecycle). Only blocks written by a trusted author count.
* A command is the first line of a comment. Everything after the first line
  is free text that is stored or shown, never evaluated.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1

# --- labels -------------------------------------------------------------------

L = {
    "task": "ws:task",
    "job": "ws:job",
    "proposed": "ws:proposed",
    "board": "ws:board",
    # one state label per open task (the projection of state.status)
    "triage": "ws:triage",
    "pending": "ws:pending",
    "ready": "ws:ready",
    "running": "ws:running",
    "review": "ws:review",
    "split": "ws:split",
    "held": "ws:held",
    "done": "ws:done",
    "failed": "ws:failed",
    "cancelled": "ws:cancelled",
    # pull requests
    "pr": "ws:pr",
    "needs_review": "ws:needs-review",
    "changes": "ws:changes-requested",
    "approved": "ws:approved",
    "ready_to_merge": "ws:ready-to-merge",
    "needs_maintainer": "ws:needs-maintainer",
    "unlinked": "ws:unlinked",
    # hygiene
    "duplicate": "ws:duplicate",
    "possible_duplicate": "ws:possible-duplicate",
    "learnings": "ws:learnings",
    "safety": "ws:safety",
    "relay": "ws:relay",
    "audit": "ws:audit",
    "audit_pass": "ws:audit-pass",
    "audit_fail": "ws:audit-fail",
}

STATE_LABELS = [L[k] for k in ("triage", "pending", "ready", "running", "review",
                                "split", "held", "done", "failed", "cancelled")]
PR_STATE_LABELS = [L[k] for k in ("needs_review", "changes", "approved", "ready_to_merge")]

LABEL_INFO = {
    L["task"]: ("1d76db", "Managed by the workstead queue"),
    L["job"]: ("0e8a16", "A top-level goal the fleet breaks down and completes"),
    L["proposed"]: ("c5def5", "Proposed by a worker; needs admission"),
    L["board"]: ("000000", "The live fleet board"),
    L["triage"]: ("fbca04", "PD: waiting for admission"),
    L["pending"]: ("d4c5f9", "PD: waiting on dependencies"),
    L["ready"]: ("0075ca", "PD: eligible to run; claim it"),
    L["running"]: ("5319e7", "R: leased to a worker"),
    L["review"]: ("bfd4f2", "CG: submitted, being reviewed"),
    L["split"]: ("c2e0c6", "R: split into child tasks"),
    L["held"]: ("b60205", "PD: held for a human (see the status comment)"),
    L["done"]: ("0e8a16", "CD: completed"),
    L["failed"]: ("d93f0b", "F: failed"),
    L["cancelled"]: ("cccccc", "CA: cancelled"),
    L["pr"]: ("1d76db", "Pull request for a workstead task"),
    L["needs_review"]: ("fbca04", "Waiting for a fleet review"),
    L["changes"]: ("d93f0b", "Changes requested"),
    L["approved"]: ("0e8a16", "Approved by the fleet"),
    L["ready_to_merge"]: ("0e8a16", "Approved and green; a maintainer merges"),
    L["needs_maintainer"]: ("b60205", "Touches protected paths; a maintainer must approve"),
    L["unlinked"]: ("cccccc", "Not linked to a task you hold"),
    L["duplicate"]: ("cfd3d7", "Duplicate of another issue"),
    L["possible_duplicate"]: ("fef2c0", "Looks like an existing task"),
    L["learnings"]: ("5319e7", "Fleet learnings reports from downstream fleets"),
    L["safety"]: ("b60205", "The shared safety net: verified flags of malicious accounts"),
    L["relay"]: ("6f42c1", "On the relay queue: agents claim it there (ws next), not here"),
    L["audit"]: ("d4c5f9", "Merged result picked for a random human audit (the published error rate)"),
    L["audit_pass"]: ("0e8a16", "Human audit: the result holds"),
    L["audit_fail"]: ("b60205", "Human audit: the result is wrong (counts toward the error rate)"),
}


def partition_label(name: str) -> str:
    return f"p:{name}"


# --- states -------------------------------------------------------------------

OPEN_STATES = ("triage", "queued", "running", "review", "split", "held")
CLOSED_STATES = ("done", "failed", "cancelled")

# squeue-style two-letter codes
SQUEUE = {"triage": "PD", "queued": "PD", "running": "R", "review": "CG",
          "split": "R", "held": "PD", "done": "CD", "failed": "F", "cancelled": "CA"}


def status_label(status: str, deps_ready: bool = True) -> str:
    if status == "queued":
        return L["ready"] if deps_ready else L["pending"]
    return L[status]


def status_from_labels(labels: List[str]) -> Optional[str]:
    """The state a label set projects, or None. Used by cheap list scans."""
    s = set(labels)
    for status in ("running", "review", "split", "held", "triage", "done", "failed", "cancelled"):
        if L[status] in s:
            return status
    if L["ready"] in s or L["pending"] in s:
        return "queued"
    return None


# --- time ---------------------------------------------------------------------

def now_iso(ts: Optional[float] = None) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: Optional[str]) -> float:
    if not s:
        return 0.0
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


# --- hidden blocks ------------------------------------------------------------

_BLOCK = r"<!--\s*ws:{name}\s+(\{{.*?\}})\s*-->"


def read_block(text: Optional[str], name: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(_BLOCK.format(name=name), text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_block(name: str, data: Dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True)
    # Free text never goes in blocks, but make "-->" impossible anyway.
    raw = raw.replace("-->", "--\\u003e")
    return f"<!-- ws:{name} {raw} -->"


def replace_block(text: str, name: str, data: Dict[str, Any]) -> str:
    block = write_block(name, data)
    pat = _BLOCK.format(name=name)
    if re.search(pat, text or "", re.S):
        return re.sub(pat, lambda _m: block, text, count=1, flags=re.S)
    return (text or "").rstrip() + "\n\n" + block + "\n"


KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FEATURE_RE = re.compile(r"^[a-z0-9][a-z0-9:._+-]{0,39}$")


def clean_meta(m: Dict[str, Any], partitions) -> Dict[str, Any]:
    """Normalise a task's meta block, dropping anything malformed."""
    def ints(v):
        return sorted({int(x) for x in v if isinstance(x, int) and not isinstance(x, bool) and x > 0}) \
            if isinstance(v, list) else []

    def strs(v, rx):
        return sorted({x for x in v if isinstance(x, str) and rx.match(x)})[:20] if isinstance(v, list) else []

    def opt_int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None

    part = m.get("partition")
    pr = m.get("priority", 3)
    est = m.get("est_hours", 1)
    out = {
        "v": PROTOCOL_VERSION,
        "partition": part if isinstance(part, str) and part in partitions else None,
        "priority": max(0, min(9, int(pr))) if isinstance(pr, (int, float)) and not isinstance(pr, bool) else 3,
        "deps": ints(m.get("deps")),
        "after": "any" if m.get("after") == "any" else "ok",
        "parent": opt_int(m.get("parent")),
        "job": opt_int(m.get("job")),
        "depth": max(0, min(20, int(m.get("depth", 0)))) if isinstance(m.get("depth", 0), int) else 0,
        "est_hours": max(0.1, min(100.0, float(est))) if isinstance(est, (int, float)) and not isinstance(est, bool) else 1.0,
        "features": strs(m.get("features"), FEATURE_RE),
        "licenses": strs(m.get("licenses"), FEATURE_RE),
        "topics": strs(m.get("topics"), FEATURE_RE),
        "origin": m.get("origin") if m.get("origin") in ("job", "plan", "manual", "found") else "manual",
        "found_in": opt_int(m.get("found_in")),
        "source": opt_int(m.get("source")),
        "plan_ref": m.get("plan_ref") if isinstance(m.get("plan_ref"), str) and len(m["plan_ref"]) < 120 else None,
    }
    return out


def new_state(kind: str = "task", status: str = "queued") -> Dict[str, Any]:
    if kind == "pr":
        return {"v": PROTOCOL_VERSION, "kind": "pr", "task": None, "review_lease": None,
                "approvals": [], "rounds": 0, "cursor": 0, "history": [], "reason": ""}
    return {"v": PROTOCOL_VERSION, "kind": "task", "status": status, "reason": "",
            "lease": None, "review_lease": None, "attempts": 0, "rounds": 0,
            "cursor": 0, "submission": None, "approvals": [], "children": [],
            "pr": None, "history": []}


def log(state: Dict[str, Any], event: str, who: str = "", ts: Optional[str] = None) -> None:
    state.setdefault("history", []).append([ts or now_iso(), event, who])
    state["history"] = state["history"][-30:]


# --- commands -----------------------------------------------------------------

_NODE = r"[A-Za-z0-9._-]{1,40}"
_REASON = r"[\w .,:;()'#/+-]{0,120}"
COMMANDS = {
    "claim": re.compile(rf"^(?:node=(?P<node>{_NODE}))?$"),
    "release": re.compile(rf"^(?P<reason>{_REASON})$"),
    "fail": re.compile(rf"^(?P<reason>{_REASON})$"),
    "done": re.compile(r"^$"),
    "plan": re.compile(r"^$"),
    "approve": re.compile(r"^$"),
    "request-changes": re.compile(r"^$"),
    "admit": re.compile(r"^(?:priority=(?P<priority>[0-9]))?$"),
    "reject": re.compile(rf"^(?P<reason>{_REASON})$"),
    "duplicate": re.compile(r"^#?(?P<of>[0-9]{1,9})$"),
    "hold": re.compile(rf"^(?P<reason>{_REASON})$"),
    "unhold": re.compile(r"^$"),
    "cancel": re.compile(rf"^(?P<reason>{_REASON})$"),
    "requeue": re.compile(r"^$"),
    "priority": re.compile(r"^(?P<priority>[0-9])$"),
    "learnings": re.compile(r"^$"),
    "flag": re.compile(r"^$"),
    "confirm": re.compile(r"^@?(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$"),
    "clear": re.compile(r"^@?(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$"),
    "appeal": re.compile(r"^$"),
}
_HEAD = re.compile(r"^\s*/([a-z-]+)[ \t]*(.*?)\s*$")


def parse_command(body: Optional[str]) -> Optional[Tuple[str, Dict[str, str], str]]:
    """'/claim node=a\\nrest' -> ('claim', {'node': 'a'}, 'rest'), else None."""
    if not body:
        return None
    first, _, rest = body.replace("\r\n", "\n").partition("\n")
    m = _HEAD.match(first)
    if not m or m.group(1) not in COMMANDS:
        return None
    am = COMMANDS[m.group(1)].match(m.group(2))
    if not am:
        return None
    args = {k: v for k, v in am.groupdict().items() if v}
    return m.group(1), args, rest.strip()


def json_block(text: str) -> Optional[Any]:
    """The first fenced ```json block (or bare ``` block) in text."""
    m = re.search(r"```(?:json)?[ \t]*\n(.*?)\n```", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


# Heartbeats edit the worker's own /claim comment in place, so thousands of
# agents beating do not create thousands of comments. The server-side
# updated_at of that comment is the heartbeat; the marker is informational and
# carries an optional checkpoint ref for checkpoint/restart.
REF_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}:[A-Za-z0-9._/-]{1,200}@[0-9a-f]{7,40}$")


def beat_body(node: str, ts: str, ref: Optional[str] = None) -> str:
    extra = f" ref={ref}" if ref and REF_RE.match(ref) else ""
    return f"/claim node={node}\n<!-- ws:beat {ts}{extra} -->"


def beat_ref(body: Optional[str]) -> Optional[str]:
    m = re.search(r"<!--\s*ws:beat\s+\S+\s+ref=(\S+)\s*-->", body or "")
    return m.group(1) if m and REF_RE.match(m.group(1)) else None


# Pull request bodies name their task with a closing keyword.
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#([0-9]{1,9})\b", re.I)


def linked_task(pr_body: Optional[str]) -> Optional[int]:
    meta = read_block(pr_body, "pr")
    if meta and isinstance(meta.get("task"), int):
        return meta["task"]
    m = CLOSES_RE.search(pr_body or "")
    return int(m.group(1)) if m else None

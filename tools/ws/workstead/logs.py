"""The log signature library: share what runs printed without sharing the text.

Test and build logs are long, repetitive and mostly noise, and reading them
costs an AI agent tokens. Each line is redacted (secrets, emails, keys), then
normalised into a **template** (numbers, paths, hex, timestamps and ids
removed); the template's hash is its **signature**. Signatures are content
addressed: anyone can check a template matches its signature.

* A run becomes a **LogDigest**: an ordered list of (signature, count, level).
  A few hundred bytes describes a log of any size.
* The fleet builds a shared **library** of templates (LogTemplate) and of what
  agents learned about them (LogNote: "flaky on macOS, retry", "fixed by
  bumping X"). These travel agent to agent over the mesh, never on public
  relays.
* ``compact()`` renders a log for an agent in a token budget: every error
  line and novel line, each known signature collapsed with the fleet's notes,
  and the rest summarised in one line. A 5,000-line log usually reads as a
  few dozen lines.

Only templates (already stripped of values) and digests are ever shared;
raw logs stay on the machine, or go to the short-lived artifact store by
content hash when a verifier needs them.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SIG_LEN = 16
MAX_TEMPLATE = 300
LEVELS = ("error", "warn", "info")

_REDACT = [
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "<key>"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "<token>"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "<token>"),
    (re.compile(r"\b(sk|pk|rk)[-_](live|test|ant|proj)?[-_]?[A-Za-z0-9_-]{16,}\b"), "<token>"),
    (re.compile(r"\bnsec1[02-9ac-hj-np-z]{20,}\b"), "<token>"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), "<jwt>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
]
_NORM = [
    (re.compile(r"\x1b\[[0-9;]*[A-Za-z]"), ""),
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:?\d\d)?\b"), "<ts>"),
    (re.compile(r"\b\d\d:\d\d:\d\d(\.\d+)?\b"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.@+-]+){2,}"), "<path>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{7,}\b", re.I), "<hex>"),
    (re.compile(r"\d+(\.\d+)?(ms|s|m|h|kb|mb|gb|%)?(?![A-Za-z])", re.I), "<n>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"[ \t]+"), " "),
]
_ERROR = re.compile(r"(?i)\b(error|errors|failed|failure|fail|exception|traceback|panic|fatal|assert(ion)?|segfault|"
                    r"denied|cannot|can't|unable|not found|timed? ?out)\b")
_WARN = re.compile(r"(?i)\b(warn(ing)?|deprecat\w*|retry(ing)?|skipp(ed|ing)|flaky)\b")


def redact(line: str) -> str:
    for rx, rep in _REDACT:
        line = rx.sub(rep, line)
    return line


def template(line: str) -> str:
    t = redact(line)
    for rx, rep in _NORM:
        t = rx.sub(rep, t)
    return t.strip()[:MAX_TEMPLATE]


def signature(tmpl: str) -> str:
    return hashlib.sha256(tmpl.encode("utf-8")).hexdigest()[:SIG_LEN]


def level(line: str) -> str:
    if _ERROR.search(line):
        return "error"
    if _WARN.search(line):
        return "warn"
    return "info"


def digest(text: str) -> Dict[str, Any]:
    """{"lines": N, "sigs": [[sig, count, level, first_line_no]], "templates": {sig: template}}."""
    order: Dict[str, List[Any]] = {}
    templates: Dict[str, str] = {}
    n = 0
    for i, raw in enumerate(text.splitlines()):
        if not raw.strip():
            continue
        n += 1
        t = template(raw)
        s = signature(t)
        if s not in order:
            order[s] = [s, 0, level(raw), i]
            templates[s] = t
        order[s][1] += 1
    return {"lines": n, "sigs": list(order.values()), "templates": templates,
            "digest": hashlib.sha256("".join(f"{s}{c}" for s, c, _, _ in order.values()).encode()).hexdigest()[:SIG_LEN]}


class Library:
    """Known templates and the fleet's notes, cached locally and fed by the mesh."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        if path and path.is_file():
            try:
                self.entries = json.loads(path.read_text())
            except ValueError:
                self.entries = {}

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, separators=(",", ":")))
            tmp.replace(self.path)

    def learn(self, sig: str, tmpl: str, count: int = 1) -> bool:
        if signature(tmpl) != sig:
            return False  # content addressing: a template must hash to its signature
        e = self.entries.setdefault(sig, {"t": tmpl, "n": 0, "notes": []})
        e["n"] += count
        return True

    def note(self, sig: str, text: str, by: str = "", task: str = "") -> None:
        e = self.entries.setdefault(sig, {"t": "", "n": 0, "notes": []})
        if not any(x["text"] == text for x in e["notes"]):
            e["notes"].append({"text": text[:500], "by": by[:64], "task": task[:64]})
            e["notes"] = e["notes"][-5:]

    def known(self, sig: str) -> bool:
        return sig in self.entries and self.entries[sig]["n"] > 0

    def ingest(self, events: List[Dict[str, Any]]) -> int:
        """Learn from LogTemplate / LogNote events (already verified)."""
        from . import nostr as N
        added = 0
        for ev in events:
            s = N.tag(ev, "s")
            if ev["kind"] == N.K_LOGT and s:
                added += self.learn(s, ev["content"], 1)
            elif ev["kind"] == N.K_LOGN and s:
                self.note(s, ev["content"], ev["pubkey"][:12], N.tag(ev, "e", ""))
                added += 1
        return added


def compact(text: str, lib: Optional[Library] = None, max_lines: int = 40) -> str:
    """A token-budgeted view of a log: errors and novelties in full, the rest collapsed."""
    d = digest(text)
    lib = lib or Library()
    raw_lines = text.splitlines()
    shown: List[str] = []
    hidden_lines = hidden_sigs = 0
    budget = max_lines
    first_error: Optional[int] = None
    for sig, count, lvl, first in d["sigs"]:
        notes = lib.entries.get(sig, {}).get("notes", [])
        novel = not lib.known(sig)
        if lvl == "error" or (novel and lvl == "warn") or notes:
            if budget <= 0:
                hidden_lines += count
                hidden_sigs += 1
                continue
            if lvl == "error" and first_error is None:
                first_error = first
            mark = "new" if novel else "known"
            line = f"[{sig}] x{count} {lvl} ({mark}) {d['templates'][sig]}"
            if notes:
                line += "  -- fleet notes: " + " | ".join(n["text"] for n in notes[-2:])
            shown.append(line)
            budget -= 1
        else:
            hidden_lines += count
            hidden_sigs += 1
    head = (f"log: {d['lines']} lines -> {len(d['sigs'])} signatures; "
            f"{sum(1 for s in d['sigs'] if s[2] == 'error')} error-like; digest {d['digest']}")
    out = [head] + shown
    if first_error is not None:
        out.append(f"first error, verbatim (redacted): {redact(raw_lines[first_error])[:400]}")
    if hidden_lines:
        out.append(f"... {hidden_lines} routine lines ({hidden_sigs} signatures) omitted")
    return "\n".join(out)


# --- mesh events -----------------------------------------------------------------------


def template_events(seckey: bytes, repo_url: str, d: Dict[str, Any], lib: Library, created_at: int,
                    pow_bits: int = 0, limit: int = 50, ttl: int = 30 * 86400) -> List[Dict[str, Any]]:
    """LogTemplate events for signatures the fleet doesn't know yet (errors and warnings first)."""
    from . import nostr as N
    novel = [s for s in d["sigs"] if not lib.known(s[0])]
    novel.sort(key=lambda s: (LEVELS.index(s[2]), s[3]))
    out = []
    for sig, count, lvl, _ in novel[:limit]:
        out.append(N.build(seckey, N.K_LOGT, [N.APP_TAG, ["r", repo_url], ["s", sig], ["level", lvl]],
                           d["templates"][sig], created_at, pow_bits, expires_at=created_at + ttl))
        lib.learn(sig, d["templates"][sig], count)
    return out


def digest_event(seckey: bytes, repo_url: str, task_id: str, d: Dict[str, Any], created_at: int,
                 pow_bits: int = 0, artifact: str = "", ttl: int = 30 * 86400) -> Dict[str, Any]:
    from . import nostr as N
    body = {"lines": d["lines"], "sigs": [[s, c, lv] for s, c, lv, _ in d["sigs"][:300]]}
    tags = [N.APP_TAG, ["r", repo_url], ["e", task_id], ["d16", d["digest"]]]
    if artifact:
        tags.append(["x", artifact])  # sha256 of the full (redacted) log in the artifact store
    return N.build(seckey, N.K_LOGD, tags, json.dumps(body, separators=(",", ":")), created_at, pow_bits,
                   expires_at=created_at + ttl)


def note_event(seckey: bytes, repo_url: str, sig: str, text: str, created_at: int, task_id: str = "",
               pow_bits: int = 0, ttl: int = 90 * 86400) -> Dict[str, Any]:
    from . import nostr as N
    tags = [N.APP_TAG, ["r", repo_url], ["s", sig]]
    if task_id:
        tags.append(["e", task_id])
    return N.build(seckey, N.K_LOGN, tags, redact(text)[:500], created_at, pow_bits, expires_at=created_at + ttl)


_SIG_RE = re.compile(r"^[0-9a-f]{16}$")


def well_formed(ev: Dict[str, Any]) -> bool:
    from . import nostr as N
    k = ev["kind"]
    if k == N.K_LOGT:
        s = N.tag(ev, "s", "")
        return bool(_SIG_RE.match(s)) and len(ev["content"]) <= MAX_TEMPLATE and signature(ev["content"]) == s \
            and N.tag(ev, "level") in LEVELS
    if k == N.K_LOGN:
        return bool(_SIG_RE.match(N.tag(ev, "s", ""))) and 0 < len(ev["content"]) <= 500
    if k == N.K_LOGD:
        try:
            body = json.loads(ev["content"])
        except ValueError:
            return False
        sigs = body.get("sigs") if isinstance(body, dict) else None
        return (isinstance(sigs, list) and len(sigs) <= 300 and isinstance(body.get("lines"), int)
                and all(isinstance(x, list) and len(x) == 3 and isinstance(x[0], str) and _SIG_RE.match(x[0])
                        and isinstance(x[1], int) and x[2] in LEVELS for x in sigs)
                and bool(N.HEX64.match(N.tag(ev, "e", ""))))
    return False

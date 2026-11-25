"""The shared safety net: fleets warn each other about malicious accounts.

A maintainer of a Workstead project who catches an account doing something
malicious (planting malware, exfiltrating secrets, injecting prompts or
dependencies, tampering with CI, sock-puppet reviews) files a **flag** with
`ws flag`. It goes to the upstream Workstead repository's "Fleet safety"
issue, where the upstream queue verifies it:

* the flag is pure data: a login, one signal from a fixed list, the project,
  and links to the evidence. No free-text accusations;
* the project is public and the reporter has authority over it (its owner, a
  public member of its organisation, or an upstream maintainer);
* every evidence link is an issue or pull request in that project, and at
  least one was written by the flagged account;
* nobody flags themselves, and one project counts once per account.

Accepted flags are recorded in the queue's own state comment, so a reporter
can't edit a flag afterwards. An account is **listed** only when flags from
at least two independent projects agree, or an upstream maintainer confirms
one; an upstream maintainer can clear an entry, and listed accounts can
appeal. Entries expire 180 days after the last flag.

Every fleet reads the list (`safety.json` on the upstream board branch, from
the CDN). A listed account isn't trusted, can't review or triage, its
proposals always go to triage and its pull requests need a maintainer; with
`safety.action: block` it can't claim work at all.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .protocol import now_iso, parse_ts

SIGNALS = {
    "malicious_code": "planted malicious or obfuscated code",
    "secret_exfiltration": "tried to exfiltrate secrets or credentials",
    "prompt_injection": "planted instructions aimed at AI agents",
    "dependency_injection": "slipped in a malicious or typosquatted dependency",
    "ci_tampering": "tampered with CI, workflows or the queue",
    "sockpuppet_review": "approved its own work through other accounts",
    "spam": "flooded the project with spam",
}
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
EXPIRY_DAYS = 180
MIN_PROJECTS = 2


def validate_flag(obj: Any) -> Tuple[Dict[str, Any], List[str]]:
    errs: List[str] = []
    if not isinstance(obj, dict):
        return {}, ["the flag must be a JSON object"]
    extra = set(obj) - {"v", "subject", "signal", "project", "evidence"}
    if extra:
        errs.append("unknown fields: " + ", ".join(sorted(extra)))
    if obj.get("v") != 1:
        errs.append("unsupported version")
    subj, proj, sig, ev = obj.get("subject"), obj.get("project"), obj.get("signal"), obj.get("evidence")
    if not isinstance(subj, str) or not LOGIN_RE.match(subj):
        errs.append("subject must be a GitHub login")
    if not isinstance(proj, str) or not REPO_RE.match(proj):
        errs.append("project must be owner/name")
    if sig not in SIGNALS:
        errs.append("signal must be one of: " + ", ".join(SIGNALS))
    if not isinstance(ev, list) or not 1 <= len(ev) <= 5:
        errs.append("evidence must list 1 to 5 links")
    elif isinstance(proj, str):
        pat = re.compile(rf"^https://github\.com/{re.escape(proj)}/(?:pull|issues)/([0-9]{{1,9}})(?:#[A-Za-z0-9_-]{{1,80}})?$")
        if not all(isinstance(e, str) and pat.match(e) for e in ev):
            errs.append("each evidence link must be an issue or pull request in the project")
    if errs:
        return {}, errs
    return {"v": 1, "subject": subj, "signal": sig, "project": proj, "evidence": list(ev)}, []


def evidence_numbers(flag: Dict[str, Any]) -> List[int]:
    return [int(re.search(r"/(?:pull|issues)/([0-9]+)", e).group(1)) for e in flag["evidence"]]


def verify(flag: Dict[str, Any], reporter: str, gh, is_upstream_maintainer) -> Optional[str]:
    """None if the flag checks out, else why not. gh can read other repositories."""
    subj, proj = flag["subject"], flag["project"]
    if reporter.lower() == subj.lower():
        return "you can't flag yourself"
    try:
        owner, kind = gh.repo_owner(proj)
    except Exception:  # private, missing or unreachable: the evidence can't be checked
        return "the project must be public so the evidence can be checked"
    authority = is_upstream_maintainer(reporter) or reporter.lower() == owner.lower() or \
        (kind == "Organization" and gh.public_member(owner, reporter))
    if not authority:
        return f"only {proj}'s owner, a public member of its organisation, or an upstream maintainer can flag from it"
    authored = False
    for n in evidence_numbers(flag):
        try:
            if gh.foreign_author(proj, n).lower() == subj.lower():
                authored = True
                break
        except Exception:
            continue
    if not authored:
        return f"none of the evidence in {proj} was written by @{subj}"
    return None


def new_state() -> Dict[str, Any]:
    return {"v": 1, "kind": "safety", "cursor": 0, "flags": [], "confirmed": {}, "cleared": {}, "appeals": {}}


def listing(state: Dict[str, Any], now: float) -> Dict[str, Dict[str, Any]]:
    """Who is listed now, from the queue's own record of accepted flags."""
    by: Dict[str, Dict[str, Any]] = {}
    for f in state.get("flags", []):
        if now - parse_ts(f["at"]) > EXPIRY_DAYS * 86400:
            continue
        e = by.setdefault(f["subject"].lower(), {"login": f["subject"], "projects": set(), "signals": set(), "last": f["at"]})
        e["projects"].add(f["project"].lower())
        e["signals"].add(f["signal"])
        e["last"] = max(e["last"], f["at"])
    out = {}
    logins = set(by) | {k.lower() for k in state.get("confirmed", {})}
    for key in logins:
        e = by.get(key) or {"login": key, "projects": set(), "signals": set(), "last": ""}
        conf = state.get("confirmed", {}).get(key)
        cleared = state.get("cleared", {}).get(key)
        last = max([x for x in (e["last"], (conf or {}).get("at", "")) if x] or [""])
        if cleared and cleared["at"] >= last:
            continue
        if conf and now - parse_ts(conf["at"]) <= EXPIRY_DAYS * 86400:
            status = "confirmed"
        elif len(e["projects"]) >= MIN_PROJECTS:
            status = "corroborated"
        else:
            continue
        out[key] = {"login": e["login"], "status": status, "projects": len(e["projects"]),
                    "signals": sorted(e["signals"]), "since": last, "appealed": key in state.get("appeals", {})}
    return out


def render_issue(state: Dict[str, Any], listed: Dict[str, Dict[str, Any]], now: float) -> str:
    pending = len({f["subject"].lower() for f in state.get("flags", [])
                   if now - parse_ts(f["at"]) <= EXPIRY_DAYS * 86400} - set(listed))
    rows = "\n".join(f"| @{e['login']} | {e['status']} | {e['projects']} | {', '.join(e['signals'])} | "
                     f"{e['since'][:10]} | {'yes' if e['appealed'] else ''} |" for e in listed.values())
    table = ("| Account | Status | Projects | Signals | Since | Appealed |\n|---|---|---|---|---|---|\n" + rows) if rows \
        else "_Nobody is listed._"
    return f"""## Fleet safety

A shared safety net for every Workstead fleet. Maintainers flag accounts that acted maliciously in their
projects with `ws flag`; this repository's queue verifies each flag (public evidence the account wrote, filed
by someone with authority over that project) and every fleet reads the resulting list.

An account is listed when flags from **{MIN_PROJECTS} independent projects** agree, or an upstream maintainer
`/confirm`s it. Listed accounts lose trust in every fleet: no reviews or triage, proposals go to triage, and
their pull requests need a maintainer. Entries expire {EXPIRY_DAYS} days after the last flag. Flags that
haven't been corroborated are never shown here.

**Listed accounts can appeal** by commenting `/appeal` here; a maintainer reviews it and may `/clear` the entry.

### Listed

{table}

{pending} report(s) awaiting corroboration. Only `/flag`, `/confirm`, `/clear` and `/appeal` comments are kept.

<sub>Updated by the workstead queue. Machine-readable: `safety.json` on the board branch.</sub>"""


def snapshot(listed: Dict[str, Dict[str, Any]], now: float) -> Dict[str, Any]:
    return {"v": 1, "generated_at": now_iso(now), "listed": {k: {kk: vv for kk, vv in v.items() if kk != "login"}
                                                             | {"login": v["login"]} for k, v in listed.items()}}

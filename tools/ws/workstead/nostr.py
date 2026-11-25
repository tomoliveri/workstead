"""Nostr events for Workstead, and the coordination rules over them.

Events are NIP-01: ``{id, pubkey, created_at, kind, tags, content, sig}``,
where ``id`` is the SHA-256 of the canonical serialisation and ``sig`` is a
BIP-340 Schnorr signature of the id. Relays are untrusted dumb pipes: every
event is verified on receipt, and all state is *derived* from the set of
valid events by the pure functions here, so every agent (and the GitHub
settlement gate) computes the same answer from the same events.

Kinds (regular, so relays keep them):

======  ====================  ==================================================
7651    TaskPublished         a task: repo, base SHA, spec hash, priority, quorum
7652    TaskClaim             a lease on a task (renewed by a newer claim)
7653    VerificationReceipt   a producer's or verifier's signed test result
7654    Endorsement           a publisher vouches for agent keys (web of trust)
7655    TaskSettled           a publisher/gate records merge, cancel or reject
7656    PeerAnnounce          a super-peer advertises its mesh endpoint (short TTL)
7657    Ban                   a publisher bans a key (Sybil, malice); everyone ignores it
7658    LogTemplate           a normalised log line and its signature (mesh only)
7659    LogNote               what an agent learned about a log signature (mesh only)
7660    LogDigest             a run's log as signatures and counts (mesh only)
7661    Finding               an approach and its outcome: dead end, partial, counterexample... (mesh)
======  ====================  ==================================================

Every agent-built event can name up to two ``parent`` vertices (dag.py), so
the events form a content-addressed Merkle-DAG in the style of IOTA's Tangle.

Claiming is optimistic with deterministic tie-breaking. A task becomes
available at a known time (published, dependencies verified, or the last
lease expired). Claims are grouped into rounds of ``claim_window_seconds``
from that moment; the earliest non-empty round wins, and inside it the claim
with the lowest ``sha256(task id + pubkey)`` wins. Backdating can't move a
claim before availability, and hashing (not timestamps) decides within a
round, so clock games buy little. Agents wait ``confirm_delay_seconds`` after
claiming and re-check before working, which absorbs relay propagation lag.

Settlement is N-of-M quorum: a result is *verified* when ``quorum`` distinct
trusted keys have published receipts with the same digest
``sha256(task | base | output | patch | verdict)`` and verdict pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import schnorr

K_TASK, K_CLAIM, K_RECEIPT, K_ENDORSE, K_SETTLED, K_PEER = 7651, 7652, 7653, 7654, 7655, 7656
K_BAN, K_LOGT, K_LOGN, K_LOGD, K_FINDING, K_TOOL = 7657, 7658, 7659, 7660, 7661, 7662
KINDS = (K_TASK, K_CLAIM, K_RECEIPT, K_ENDORSE, K_SETTLED, K_PEER, K_BAN, K_LOGT, K_LOGN, K_LOGD, K_FINDING, K_TOOL)
APP_TAG = ["t", "workstead"]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
MAX_CONTENT = 16000

# --- NIP-01 ------------------------------------------------------------------------


def serialize(ev: Dict[str, Any]) -> bytes:
    return json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_id(ev: Dict[str, Any]) -> str:
    return hashlib.sha256(serialize(ev)).hexdigest()


def leading_zero_bits(hexid: str) -> int:
    n = int(hexid, 16)
    return 256 - n.bit_length() if n else 256


def build(seckey: bytes, kind: int, tags: List[List[str]], content: str, created_at: int,
          pow_bits: int = 0, expires_at: Optional[int] = None, parents: Iterable[str] = ()) -> Dict[str, Any]:
    """Create, mine (NIP-13) and sign an event; NIP-40 expiration and DAG parents if given."""
    pub = schnorr.pubkey(seckey).hex()
    tags = [list(map(str, t)) for t in tags] + [["parent", p] for p in list(parents)[:2]]
    if expires_at:
        tags.append(["expiration", str(int(expires_at))])
    ev = {"pubkey": pub, "created_at": int(created_at), "kind": kind, "tags": tags, "content": content}
    if pow_bits > 0:
        nonce = 0
        base = [t for t in tags if t and t[0] != "nonce"]
        while True:
            ev["tags"] = base + [["nonce", str(nonce), str(pow_bits)]]
            eid = event_id(ev)
            if leading_zero_bits(eid) >= pow_bits:
                break
            nonce += 1
    eid = event_id(ev)
    ev["id"] = eid
    ev["sig"] = schnorr.sign(bytes.fromhex(eid), seckey).hex()
    return ev


_VERIFIED: Dict[str, bool] = {}


def verify(ev: Any) -> bool:
    """Shape, id and signature. Cached by id (ids commit to everything)."""
    try:
        if not isinstance(ev, dict) or not isinstance(ev.get("tags"), list) or not isinstance(ev.get("content"), str):
            return False
        if not (HEX64.match(ev["id"]) and HEX64.match(ev["pubkey"]) and re.fullmatch(r"[0-9a-f]{128}", ev["sig"])):
            return False
        if not isinstance(ev["created_at"], int) or not isinstance(ev["kind"], int):
            return False
        if not all(isinstance(t, list) and all(isinstance(x, str) for x in t) for t in ev["tags"]):
            return False
        computed = event_id(ev)  # always recompute: never trust the claimed id
        if computed != ev["id"]:
            return False
        key = computed + ev["sig"]
        if key in _VERIFIED:
            return _VERIFIED[key]
        ok = schnorr.verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]),
                                                          bytes.fromhex(ev["sig"]))
        if len(_VERIFIED) > 200000:
            _VERIFIED.clear()
        _VERIFIED[key] = ok
        return ok
    except (KeyError, ValueError, TypeError):
        return False


def tag(ev: Dict[str, Any], name: str, default: Optional[str] = None) -> Optional[str]:
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == name:
            return t[1]
    return default


def tags(ev: Dict[str, Any], name: str) -> List[List[str]]:
    return [t for t in ev.get("tags", []) if t and t[0] == name]


def pow_ok(ev: Dict[str, Any], bits: int) -> bool:
    if bits <= 0:
        return True
    committed = tags(ev, "nonce")
    # NIP-13: the target is committed in the nonce tag, so a cheaper target can't masquerade.
    return bool(committed) and len(committed[0]) >= 3 and committed[0][2].isdigit() and \
        int(committed[0][2]) >= bits and leading_zero_bits(ev["id"]) >= bits


# --- the Workstead event schemas -----------------------------------------------------


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_hash(spec: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical(spec).encode()).hexdigest()


def task_event(seckey: bytes, repo_url: str, base_sha: str, spec: Dict[str, Any], priority: int, quorum: int,
               created_at: int, deps: Iterable[str] = (), external: Optional[str] = None,
               ttl: int = 90 * 86400) -> Dict[str, Any]:
    content = canonical(spec)
    t = [APP_TAG, ["r", repo_url], ["base", base_sha], ["x", spec_hash(spec)],
         ["priority", str(priority)], ["quorum", str(quorum)]]
    t += [["e", d, "", "dep"] for d in deps]
    if external:
        t.append(["i", external])
    return build(seckey, K_TASK, t, content, created_at, expires_at=created_at + ttl)


def claim_event(seckey: bytes, task_id: str, repo_url: str, created_at: int, lease_seconds: int,
                pow_bits: int = 0, grace: int = 86400, parents: Iterable[str] = ()) -> Dict[str, Any]:
    """A claim or renewal. Coordination tier only; it expires a grace period after the lease."""
    until = created_at + lease_seconds
    return build(seckey, K_CLAIM, [APP_TAG, ["e", task_id], ["r", repo_url], ["lease", str(until)]], "",
                 created_at, pow_bits, expires_at=until + grace, parents=parents)


def receipt_digest(task_id: str, base: str, output: str, patch: str, verdict: str) -> str:
    return hashlib.sha256(f"{task_id}|{base}|{output}|{patch}|{verdict}".encode()).hexdigest()


def receipt_event(seckey: bytes, task_id: str, repo_url: str, base: str, output: str, patch: str, tests: str,
                  verdict: str, role: str, created_at: int, ref: Optional[str] = None, summary: str = "",
                  pow_bits: int = 0, ttl: int = 90 * 86400, parents: Iterable[str] = (),
                  logs: Optional[str] = None, model: str = "") -> Dict[str, Any]:
    t = [APP_TAG, ["e", task_id], ["r", repo_url], ["base", base], ["output", output], ["patch", patch],
         ["tests", tests], ["verdict", verdict], ["role", role],
         ["digest", receipt_digest(task_id, base, output, patch, verdict)]]
    if ref:
        t.append(["ref", ref])
    if logs:
        t.append(["logs", logs])  # LogDigest id or artifact hash: the full run, by reference
    if model and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", model):
        t.append(["model", model])  # model family, self-declared (claude, gpt, gemini, ...)
    content = canonical({"summary": summary[:2000]}) if summary else ""
    return build(seckey, K_RECEIPT, t, content, created_at, pow_bits, expires_at=created_at + ttl, parents=parents)


def endorse_event(seckey: bytes, repo_url: str, agents: Iterable[str], created_at: int,
                  ttl: int = 90 * 86400) -> Dict[str, Any]:
    return build(seckey, K_ENDORSE, [APP_TAG, ["r", repo_url]] + [["p", a] for a in agents], "", created_at,
                 expires_at=created_at + ttl)


def settled_event(seckey: bytes, task_id: str, repo_url: str, status: str, created_at: int, output: str = "",
                  pr: str = "", ttl: int = 90 * 86400) -> Dict[str, Any]:
    t = [APP_TAG, ["e", task_id], ["r", repo_url], ["status", status]]
    if output:
        t.append(["output", output])
    if pr:
        t.append(["pr", pr])
    return build(seckey, K_SETTLED, t, "", created_at, expires_at=created_at + ttl)


def peer_event(seckey: bytes, repo_url: str, url: str, capacity: int, created_at: int, ttl: int = 1800,
               pow_bits: int = 0) -> Dict[str, Any]:
    """A super-peer's advertisement: the only mesh traffic on public relays."""
    return build(seckey, K_PEER, [APP_TAG, ["r", repo_url], ["url", url], ["cap", str(capacity)], ["role", "super"]],
                 "", created_at, pow_bits, expires_at=created_at + ttl)


def ban_event(seckey: bytes, repo_url: str, pubkeys: Iterable[str], reason: str, created_at: int,
              ttl: int = 180 * 86400) -> Dict[str, Any]:
    return build(seckey, K_BAN, [APP_TAG, ["r", repo_url], ["reason", reason]] + [["p", p] for p in pubkeys], "",
                 created_at, expires_at=created_at + ttl)


BAN_REASONS = ("sybil", "malicious_code", "spam", "false_receipts", "lease_abuse")
COORDINATION_KINDS = (K_CLAIM, K_LOGT, K_LOGN, K_LOGD, K_FINDING)


def tier(kind: int) -> str:
    """Claims and renewals go to volunteer coordination relays; the rest are milestones."""
    return "coordination" if kind in COORDINATION_KINDS else "milestone"


def expired(ev: Dict[str, Any], now: int) -> bool:
    exp = tag(ev, "expiration")
    return bool(exp and exp.isdigit() and int(exp) < now)


REF_RE = re.compile(r"^(https://[A-Za-z0-9._/-]{1,300}|file://[A-Za-z0-9._/~-]{1,300}|/[A-Za-z0-9._/~-]{1,300})#[A-Za-z0-9._/-]{1,200}$")


def well_formed(ev: Dict[str, Any], repo_url: str, cfg: Dict[str, Any]) -> bool:
    """Schema checks per kind, beyond the signature."""
    if ev["kind"] not in KINDS or APP_TAG not in ev["tags"]:
        return False
    par = tags(ev, "parent")
    if len(par) > 2 or not all(len(p) >= 2 and HEX64.match(p[1]) for p in par):
        return False
    if tag(ev, "r") != repo_url or len(ev["content"]) > MAX_CONTENT:
        return False
    k = ev["kind"]
    rc = cfg["relay"]
    if k == K_TASK:
        try:
            spec = json.loads(ev["content"])
        except ValueError:
            return False
        return (isinstance(spec, dict) and isinstance(spec.get("title"), str) and spec_hash(spec) == tag(ev, "x")
                and bool(SHA.match(tag(ev, "base", ""))) and (tag(ev, "priority", "").isdigit())
                and tag(ev, "quorum", "").isdigit() and 1 <= int(tag(ev, "quorum")) <= 9)
    if k == K_CLAIM:
        lease = tag(ev, "lease", "")
        return (bool(HEX64.match(tag(ev, "e", ""))) and lease.isdigit()
                and 0 < int(lease) - ev["created_at"] <= rc["max_lease_minutes"] * 60
                and pow_ok(ev, rc["pow_bits"]))
    if k == K_RECEIPT:
        fields = {n: tag(ev, n, "") for n in ("e", "base", "output", "patch", "tests", "verdict", "role", "digest")}
        if not (HEX64.match(fields["e"]) and SHA.match(fields["base"]) and SHA.match(fields["output"])
                and HEX64.match(fields["patch"]) and HEX64.match(fields["tests"])
                and fields["verdict"] in ("pass", "fail") and fields["role"] in ("produce", "verify")):
            return False
        if fields["digest"] != receipt_digest(fields["e"], fields["base"], fields["output"], fields["patch"], fields["verdict"]):
            return False
        if fields["role"] == "produce" and not REF_RE.match(tag(ev, "ref", "")):
            return False
        return pow_ok(ev, rc["pow_bits"])
    if k == K_ENDORSE:
        return all(HEX64.match(t[1]) for t in tags(ev, "p"))
    if k == K_SETTLED:
        return bool(HEX64.match(tag(ev, "e", ""))) and tag(ev, "status") in ("merged", "cancelled", "rejected")
    if k == K_BAN:
        return tag(ev, "reason") in BAN_REASONS and bool(tags(ev, "p")) and all(HEX64.match(t[1]) for t in tags(ev, "p"))
    if k in (K_LOGT, K_LOGN, K_LOGD):
        from . import logs as _logs
        return _logs.well_formed(ev) and pow_ok(ev, rc["pow_bits"])
    if k == K_TOOL:
        from .toolbox import release_ok
        return release_ok(ev)
    if k == K_FINDING:
        from . import findings as _f
        return _f.well_formed(ev) and pow_ok(ev, rc["pow_bits"])
    if k == K_PEER:
        return (bool(re.fullmatch(r"wss?://[A-Za-z0-9.\-\[\]:]{1,100}(/[A-Za-z0-9._/-]{0,100})?", tag(ev, "url", "")))
                and tag(ev, "cap", "").isdigit() and tag(ev, "expiration") is not None
                and int(tag(ev, "expiration")) - ev["created_at"] <= 3 * 3600 and pow_ok(ev, rc["pow_bits"]))
    return False


# --- deriving state from events --------------------------------------------------------


def trust_sets(events: Iterable[Dict[str, Any]], cfg: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """(publishers, trusted agents). Publishers are configured; agents are
    configured plus anyone a publisher endorsed."""
    rc = cfg["relay"]
    pubs = {schnorr.parse_pubkey(p) for p in rc["publishers"]}
    agents = {schnorr.parse_pubkey(a) for a in rc["trusted_agents"]} | pubs
    for ev in events:
        if ev["kind"] == K_ENDORSE and ev["pubkey"] in pubs:
            agents |= {t[1] for t in tags(ev, "p")}
    return pubs, agents


DEFAULT_CLAIMS = {"head_start_seconds": {"trusted": 0, "proven": 60, "new": 180}, "new_key_pow_bits": 16,
                  "new_key_lease_share": 0.25, "max_lapses": 2, "lapse_window_hours": 24}


def claim_policy(rc: Dict[str, Any]) -> Dict[str, Any]:
    pol = dict(DEFAULT_CLAIMS)
    pol.update(rc.get("claims") or {})
    pol["head_start_seconds"] = dict(DEFAULT_CLAIMS["head_start_seconds"], **(pol.get("head_start_seconds") or {}))
    return pol


def _tier_fn(agents: Set[str], proven: Set[str]):
    def tier(pk: str) -> str:
        return "trusted" if pk in agents else ("proven" if pk in proven else "new")
    return tier


def claim_eligible(c: Dict[str, Any], start: int, tier, pol: Dict[str, Any], struck: Set[str]) -> bool:
    """Does this claim take part in the contest for a task available since `start`?"""
    if c["pubkey"] in struck:
        return False
    t = tier(c["pubkey"])
    if c["created_at"] < start + int(pol["head_start_seconds"].get(t, 0)):
        return False  # before this tier's head start ended
    return t != "new" or pow_ok(c, int(pol["new_key_pow_bits"]))


def claim_rank(task_id: str, pubkey: str) -> str:
    return hashlib.sha256((task_id + pubkey).encode()).hexdigest()


def compute(events: Iterable[Dict[str, Any]], cfg: Dict[str, Any], repo_url: str, now: int,
            ctx_out: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """The whole queue, derived deterministically from a set of verified events.

    Returns {task_id: {...}} with status one of pending, open, leased,
    submitted, verified, merged, cancelled, rejected.
    """
    rc = cfg["relay"]
    window = max(1, int(rc["claim_window_seconds"]))
    skew = int(rc["max_skew_seconds"])
    evs = [e for e in events if e["created_at"] <= now + skew and not expired(e, now) and well_formed(e, repo_url, cfg)]
    pubs, agents = trust_sets(evs, cfg)
    banned = {t[1] for e in evs if e["kind"] == K_BAN and e["pubkey"] in pubs for t in tags(e, "p")} - pubs
    if banned:
        evs = [e for e in evs if e["pubkey"] not in banned]
        agents = agents - banned
    tasks = {e["id"]: e for e in evs if e["kind"] == K_TASK and e["pubkey"] in pubs}
    receipts_all = [e for e in evs if e["kind"] == K_RECEIPT]
    from . import dag as _dag
    pov_min = int(rc.get("pov_min_prior_verifications", 0))

    def pov(producer: str, at: int) -> bool:
        return _dag.pov_ok(producer, at, receipts_all, pov_min, pubs)
    by_task: Dict[str, Dict[str, List[Dict[str, Any]]]] = {t: {"claims": [], "receipts": [], "settled": []} for t in tasks}
    for e in evs:
        tid = tag(e, "e")
        if tid in by_task:
            key = {K_CLAIM: "claims", K_RECEIPT: "receipts", K_SETTLED: "settled"}.get(e["kind"])
            if key:
                by_task[tid][key].append(e)

    pol = claim_policy(rc)

    def run(proven: Set[str], struck: Set[str], lapses: Dict[str, List[int]]) -> Dict[str, Dict[str, Any]]:
        tier = _tier_fn(agents, proven)
        out: Dict[str, Dict[str, Any]] = {}
        pending = dict(tasks)
        # Dependencies: resolve in passes (tasks whose deps are known come first).
        for _ in range(len(tasks) + 1):
            progressed = False
            for tid, t in list(pending.items()):
                deps = [d[1] for d in tags(t, "e") if len(d) >= 4 and d[3] == "dep"]
                if any(d in pending for d in deps):
                    continue
                dep_done = [out.get(d) for d in deps]
                if any(x is None or x["status"] not in ("verified", "merged") for x in dep_done):
                    out[tid] = _task_row(t, "pending", by_task[tid])
                    del pending[tid]
                    progressed = True
                    continue
                available = max([t["created_at"]] + [x["verified_at"] or 0 for x in dep_done])
                out[tid] = _derive(t, by_task[tid], agents, pubs, available, window, rc, now, pov=pov,
                                       tier=tier, pol=pol, struck=struck, lapses=lapses)
                del pending[tid]
                progressed = True
            if not progressed:
                break
        for tid, t in pending.items():  # cycles: never runnable
            out[tid] = _task_row(t, "pending", by_task[tid])
        _enforce_lease_limits(out, by_task, tasks, agents, pubs, window, rc, now, pov, tier=tier, pol=pol,
                              struck=struck, lapses=lapses)
        return out

    # Pass 1 finds who has delivered verified work ("proven") and who keeps
    # abandoning leases; pass 2 applies the claim tiers and strikes. Both are
    # pure functions of the event set, so every observer agrees.
    lapses: Dict[str, List[int]] = {}
    first = run(set(), set(), lapses)
    proven = {r["producer"] for r in first.values() if r["status"] in ("verified", "merged") and r["producer"]}
    since = now - int(pol["lapse_window_hours"]) * 3600
    struck = {k for k, v in lapses.items() if len([u for u in v if u >= since]) >= int(pol["max_lapses"])} - pubs
    out = run(proven, struck, {}) if (proven or struck) else first
    if ctx_out is not None:
        ctx_out.update(trusted=agents, proven=proven - agents, struck=struck, policy=pol)
    return out


def _task_row(t: Dict[str, Any], status: str, rel: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    try:
        spec = json.loads(t["content"])
    except ValueError:
        spec = {}
    return {"id": t["id"], "status": status, "spec": spec, "base": tag(t, "base"),
            "priority": int(tag(t, "priority", "3")), "quorum": int(tag(t, "quorum", "2")),
            "created_at": t["created_at"], "holder": None, "lease_until": None, "claim": None,
            "producer": None, "output": None, "digest": None, "votes": 0, "verifiers": [],
            "verified_at": None, "external": tag(t, "i"), "publisher": t["pubkey"]}


def _derive(t, rel, agents, pubs, available, window, rc, now, exclude=frozenset(), pov=None, tier=None, pol=None,
            struck=frozenset(), lapses=None) -> Dict[str, Any]:
    tier = tier or _tier_fn(agents, set())
    pol = pol or claim_policy(rc)
    row = _task_row(t, "open", rel)
    tid = t["id"]
    for s in sorted(rel["settled"], key=lambda e: (e["created_at"], e["id"])):
        if s["pubkey"] in pubs:
            row["status"] = tag(s, "status")
            row["output"] = tag(s, "output")
            return row
    # Quorum first: a verified result stands no matter who holds the lease.
    q = row["quorum"]
    distinct = max(1, int(rc.get("distinct_models", 1)))
    groups: Dict[str, Dict[str, Any]] = {}
    for r in sorted(rel["receipts"], key=lambda e: (e["created_at"], e["id"])):
        if tag(r, "verdict") != "pass":
            continue
        g = groups.setdefault(tag(r, "digest"), {"keys": [], "producer": None, "output": tag(r, "output"),
                                                 "base": tag(r, "base"), "at": None, "trusted": []})
        if tag(r, "role") == "produce" and g["producer"] is None:
            if pov is not None and not pov(r["pubkey"], r["created_at"]):
                continue  # proof-of-verification: this author hadn't verified enough prior tips
            g["producer"] = r["pubkey"]
            g["produced_at"] = r["created_at"]
        if r["pubkey"] not in g["keys"]:
            g["keys"].append(r["pubkey"])
            if r["pubkey"] in agents and (rc["count_producer"] or tag(r, "role") != "produce"):
                g["trusted"].append(r["pubkey"])
                g.setdefault("models", set()).add(tag(r, "model") or "unknown")
                # Correlated judges: the same model family tends to make the same
                # mistake, so a project can require the quorum to span families.
                if len(g["trusted"]) >= q and len(g["models"]) >= distinct and g["at"] is None:
                    g["at"] = r["created_at"]
    verified = [g for g in groups.values() if g["at"] is not None and g["producer"]]
    if verified:
        best = min(verified, key=lambda g: (g["at"], g["output"]))
        row.update(status="verified", producer=best["producer"], output=best["output"], votes=len(best["trusted"]),
                   verifiers=best["trusted"], verified_at=best["at"], models=sorted(best.get("models", ())),
                   digest=next(d for d, g in groups.items() if g is best))
        return row
    produced = [g for g in groups.values() if g["producer"]]
    claims = sorted((c for c in rel["claims"] if c["pubkey"] not in exclude), key=lambda e: (e["created_at"], e["id"]))
    start = available
    for _ in range(64):
        live = [c for c in claims if claim_eligible(c, start, tier, pol, struck)]
        if not live:
            break
        # Absolute windows: a claim's round doesn't depend on how much expired
        # history an observer still sees, so observers agree on the winner.
        first_round = min(c["created_at"] // window for c in live)
        contenders = [c for c in live if c["created_at"] // window == first_round]
        winner = min(contenders, key=lambda c: (claim_rank(tid, c["pubkey"]), c["id"]))
        holder = winner["pubkey"]
        # Renewals: later claims by the holder extend the lease, if made before it lapsed.
        until = int(tag(winner, "lease"))
        for c in claims:
            if c["pubkey"] == holder and c["created_at"] > winner["created_at"] and c["created_at"] <= until:
                until = max(until, int(tag(c, "lease")))
        mine = [g for g in produced if g["producer"] == holder]
        if mine:
            g = mine[-1]
            row.update(status="submitted", holder=holder, producer=holder, output=g["output"],
                       votes=len(g["trusted"]), verifiers=g["trusted"], claim=winner["id"], lease_until=until,
                       digest=next(d for d, x in groups.items() if x is g))
            return row
        if until >= now:
            row.update(status="leased", holder=holder, lease_until=until, claim=winner["id"])
            return row
        if lapses is not None:
            lapses.setdefault(holder, []).append(until)  # abandoned without a result
        start = until + 1  # lease lapsed: a new contest starts after it
    row["available_at"] = start
    return row


def _enforce_lease_limits(out, by_task, tasks, agents, pubs, window, rc, now, pov=None, tier=None, pol=None,
                          struck=frozenset(), lapses=None) -> None:
    """A key may hold at most max_active_leases at once, and new keys together at
    most new_key_lease_share of the claimable tasks; excess wins are void."""
    tier = tier or _tier_fn(agents, set())
    pol = pol or claim_policy(rc)
    limit = int(rc["max_active_leases"])
    kw = {"pov": pov, "tier": tier, "pol": pol, "struck": struck, "lapses": lapses}

    def held_now():
        held: Dict[str, List[Tuple[int, str, str]]] = {}
        for tid, row in out.items():
            if row["status"] == "leased":
                c = next(x for x in by_task[tid]["claims"] if x["id"] == row["claim"])
                held.setdefault(row["holder"], []).append((c["created_at"], c["id"], tid))
        return held
    for holder, lst in held_now().items():
        if len(lst) <= limit:
            continue
        for _, _, tid in sorted(lst)[limit:]:
            t = tasks[tid]
            out[tid] = _derive(t, by_task[tid], agents, pubs, t["created_at"], window, rc, now, exclude={holder}, **kw)
    claimable = sum(1 for r in out.values() if r["status"] in ("open", "leased"))
    cap = max(1, int(float(pol["new_key_lease_share"]) * claimable))
    newbies = sorted((x, h) for h, lst in held_now().items() if tier(h) == "new" for x in lst)
    for (_, _, tid), holder in newbies[cap:]:
        t = tasks[tid]
        out[tid] = _derive(t, by_task[tid], agents, pubs, t["created_at"], window, rc, now,
                           exclude={holder} | {h for _, h in newbies[cap:]}, **kw)


def normalize_output(text: str, root: str = "") -> str:
    """Test output with the run-specific noise removed, for test hashes."""
    if root:
        text = text.replace(root, "<root>")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0x?", text)
    text = re.sub(r"\d+(\.\d+)?", "0", text)
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def tests_hash(text: str, root: str = "") -> str:
    return hashlib.sha256(normalize_output(text, root).encode()).hexdigest()

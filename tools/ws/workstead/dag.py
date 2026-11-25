"""A Tangle-style Merkle-DAG of work, borrowed from IOTA without the coin.

Every event an agent publishes names up to two **parent tips**: recent
vertices nobody has referenced yet, preferring unverified PatchSubmitted
vertices (producer receipts). Event ids are content hashes that commit to
their parents, so the events form a content-addressed Merkle-DAG.

Two rules make the DAG useful:

* **Proof-of-verification admission.** A producer's patch counts only if its
  author has itself verified ``pov_min_prior_verifications`` (default 2)
  earlier patches by *other* producers before submitting, like IOTA's
  "approve two tips to attach". Verification work is the entry fee, so a
  Sybil swarm has to do real verification before it can submit anything.
  During bootstrap the requirement is capped by how many verifiable patches
  existed at the time, so the first producers aren't locked out.
* **Independent backing.** The settlement gate needs ``quorum`` matching
  receipts from distinct trusted keys, excluding the producer by default.

Sync is git-style: peers compare per-hour Merkle roots of event ids and fetch
only the buckets that differ, so catching up is one round-trip of roots plus
the missing vertices, over any transport (relays, super-peers, an HTTP log).
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, Iterable, List, Optional, Set

from . import nostr as N

BUCKET = 3600


def parents_of(ev: Dict[str, Any]) -> List[str]:
    return [t[1] for t in N.tags(ev, "parent")][:2]


def tips(events: Iterable[Dict[str, Any]], kinds=(N.K_RECEIPT, N.K_TASK)) -> List[Dict[str, Any]]:
    """Vertices no other event references, newest first."""
    evs = list(events)
    referenced: Set[str] = set()
    for e in evs:
        referenced.update(parents_of(e))
    out = [e for e in evs if e["kind"] in kinds and e["id"] not in referenced]
    out.sort(key=lambda e: (-e["created_at"], e["id"]))
    return out


def select_parents(events: Iterable[Dict[str, Any]], me: str, rng: Optional[random.Random] = None,
                   k: int = 2) -> List[str]:
    """Two tips to approve: unverified patches by others first, then any tip."""
    rng = rng or random.Random()
    evs = list(events)
    verified_targets = {N.tag(e, "e") + "|" + N.tag(e, "output", "") for e in evs
                        if e["kind"] == N.K_RECEIPT and N.tag(e, "role") == "verify"}
    t = tips(evs)
    patches = [e for e in t if N.tag(e, "role") == "produce" and e["pubkey"] != me
               and N.tag(e, "e") + "|" + N.tag(e, "output", "") not in verified_targets]
    rest = [e for e in t if e not in patches]
    pick: List[str] = []
    for pool in (patches[:8], rest[:8]):
        pool = list(pool)
        rng.shuffle(pool)
        for e in pool:
            if len(pick) < k and e["id"] not in pick:
                pick.append(e["id"])
    return pick


def bucket_roots(events: Iterable[Dict[str, Any]], bucket: int = BUCKET) -> Dict[int, str]:
    """{bucket start: Merkle root of the sorted event ids in that bucket}."""
    by: Dict[int, List[str]] = {}
    for e in events:
        by.setdefault(e["created_at"] // bucket * bucket, []).append(e["id"])
    return {b: merkle_root(sorted(ids)) for b, ids in by.items()}


def merkle_root(ids: List[str]) -> str:
    level = [bytes.fromhex(i) for i in ids] or [b"\x00" * 32]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()


def differing(mine: Dict[int, str], theirs: Dict[int, str]) -> List[int]:
    return sorted(b for b in set(mine) | set(theirs) if mine.get(b) != theirs.get(b))


def prior_verifications(pubkey: str, before: int, receipts: Iterable[Dict[str, Any]]) -> int:
    """Distinct other producers' patches this key verified before a time."""
    produce = {N.tag(r, "e") + "|" + N.tag(r, "output", ""): r["pubkey"] for r in receipts
               if N.tag(r, "role") == "produce"}
    seen = set()
    for r in receipts:
        if r["pubkey"] == pubkey and N.tag(r, "role") == "verify" and r["created_at"] < before:
            target = N.tag(r, "e") + "|" + N.tag(r, "output", "")
            if produce.get(target) not in (None, pubkey):
                seen.add(target)
    return len(seen)


def required_verifications(pubkey: str, before: int, receipts: Iterable[Dict[str, Any]], minimum: int) -> int:
    """The PoV entry fee, capped during bootstrap by what there was to verify."""
    available = {N.tag(r, "e") + "|" + N.tag(r, "output", "") for r in receipts
                 if N.tag(r, "role") == "produce" and r["pubkey"] != pubkey and r["created_at"] < before}
    return min(minimum, len(available))


def pov_ok(pubkey: str, before: int, receipts: List[Dict[str, Any]], minimum: int, exempt: Set[str]) -> bool:
    if minimum <= 0 or pubkey in exempt:
        return True
    return prior_verifications(pubkey, before, receipts) >= required_verifications(pubkey, before, receipts, minimum)

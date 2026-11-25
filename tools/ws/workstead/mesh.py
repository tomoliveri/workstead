"""The super-peer hybrid mesh that carries high-frequency coordination.

Claims and lease renewals never touch public relays. They travel through a
mesh of *super-peers*: ordinary agents that can accept inbound connections
and have promoted themselves to run a small relay (``relay.LocalRelay``).

* **Discovery.** A super-peer advertises itself with a short-lived, signed
  ``PeerAnnounce`` on the milestone relays: one tiny event per super-peer
  every ``announce_minutes``.
* **Attachment.** Every agent connects to ``connect_to`` super-peers chosen
  by rendezvous hashing (stable, spreads load), preferring super-peers run
  by trusted agents. Static seeds (``coordination_urls``: a volunteer strfry
  or ``ws relay serve``) are always included.
* **Replication.** Super-peers pull claims from one another every
  ``sync_seconds``, so the mesh converges well inside the claim confirm delay.
* **Election.** An agent that a super-peer can dial back (a STUN-like probe),
  or that has a configured ``public_url``, or (in a brand-new fleet with no
  super-peer to probe through) has a publicly routable IPv6/IPv4 address,
  promotes itself when the fleet has
  fewer super-peers than it needs (one per ``peers_per_super`` agents, within
  ``min_supers``..``max_supers``). Promotions are spread out by a hash
  lottery so a hundred agents don't all promote at once; when there are too
  many, the lowest-ranked by hash demote. Ranks rotate hourly.
* **Safety.** Super-peers can't forge or alter events (everything is signed
  and verified); the worst they can do is withhold, which three independent
  attachments make ineffective. Announces need proof of work.
* **Fallback.** With no reachable super-peer at all, claims go to the
  milestone relays (one claim per task, no renewals): degraded, but bounded.

The transport is WebSockets because that is what pure-stdlib Python can do
well; libp2p or WebRTC can replace it behind the same RelayPool interface.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from . import nostr as N


def _routable(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"):
        return False  # carrier-grade NAT
    return a.is_global


def global_address() -> Optional[str]:
    """This machine's publicly routable address, if it has one (no packets are sent).

    Connecting a UDP socket only picks the outbound route and source address.
    IPv6 is preferred: most home connections give every machine a global IPv6
    address, while IPv4 usually sits behind NAT.
    """
    import socket
    for fam, target, fmt in ((socket.AF_INET6, "2001:4860:4860::8888", "[{}]"), (socket.AF_INET, "8.8.8.8", "{}")):
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.connect((target, 53))
                ip = s.getsockname()[0]
        except OSError:
            continue
        if _routable(ip):
            return fmt.format(ip)
    return None


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


def live_supers(events: Iterable[Dict[str, Any]], repo_url: str, cfg: Dict[str, Any], now: int,
                trusted: Set[str]) -> List[Dict[str, Any]]:
    """Current super-peers from PeerAnnounce events (latest per key), trusted first."""
    latest: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev["kind"] != N.K_PEER or N.expired(ev, now) or not N.well_formed(ev, repo_url, cfg):
            continue
        cur = latest.get(ev["pubkey"])
        if cur is None or (ev["created_at"], ev["id"]) > (cur["created_at"], cur["id"]):
            latest[ev["pubkey"]] = ev
    out = [{"pubkey": pk, "url": N.tag(e, "url"), "cap": int(N.tag(e, "cap")), "at": e["created_at"],
            "trusted": pk in trusted} for pk, e in latest.items()]
    out.sort(key=lambda s: (not s["trusted"], s["pubkey"]))
    return out


def active_agents(events: Iterable[Dict[str, Any]], now: int, window: int = 3600) -> int:
    keys = {e["pubkey"] for e in events if e["kind"] in (N.K_CLAIM, N.K_RECEIPT, N.K_PEER)
            and now - e["created_at"] <= window}
    return len(keys)


def target_supers(active: int, cfg: Dict[str, Any]) -> int:
    m = cfg["relay"]["mesh"]
    return max(m["min_supers"], min(m["max_supers"], math.ceil(active / max(1, m["peers_per_super"]))))


def choose(me: str, supers: List[Dict[str, Any]], k: int) -> List[str]:
    """Rendezvous hashing: each agent's stable, load-spread pick, trusted first."""
    ranked = sorted(supers, key=lambda s: (not s["trusted"], _h(me, s["url"])))
    return [s["url"] for s in ranked[:k]]


def decide(me: str, reachable: bool, is_super: bool, supers: List[Dict[str, Any]], active: int,
           cfg: Dict[str, Any], now: int) -> str:
    """'promote', 'demote' or 'stay' for this agent, deterministically."""
    hour = str(now // 3600)
    target = target_supers(active, cfg)
    others = [s for s in supers if s["pubkey"] != me]
    count = len(others) + (1 if is_super else 0)
    if is_super:
        if not reachable:
            return "demote"
        if count > math.ceil(target * 1.5):
            ranks = sorted([s["pubkey"] for s in others] + [me], key=lambda pk: _h(pk, hour))
            excess = count - target
            if me in ranks[-excess:]:
                return "demote"
        return "stay"
    if not reachable or count >= target:
        return "stay"
    # Lottery: about `deficit` of the active agents promote this round.
    deficit = target - count
    chance = min(1.0, 2.0 * deficit / max(1, active))
    return "promote" if (_h(me, hour, str(now // 60)) % 10_000) / 10_000 < chance else "stay"


class Mesh:
    """An agent's view of the mesh: its attachments and, if elected, its super-peer."""

    def __init__(self, cfg: Dict[str, Any], seckey: bytes, pubkey: str, repo_url: str, relays,
                 clock: Callable[[], float] = time.time):
        from .relay import LocalRelay, RelayConn, RelayPool  # noqa: F401  (runtime deps)
        self.cfg = cfg
        self.m = cfg["relay"]["mesh"]
        self.seckey = seckey
        self.pubkey = pubkey
        self.repo_url = repo_url
        self.relays = relays
        self.clock = clock
        self.server = None
        self.public_url: Optional[str] = None
        self.reachable: Optional[bool] = None
        self.last_announce = 0.0
        self._stop = threading.Event()
        self.attached: List[str] = []

    @property
    def is_super(self) -> bool:
        return self.server is not None

    def refresh(self, events: List[Dict[str, Any]], trusted: Set[str]) -> List[str]:
        """Re-elect and re-attach. Returns the coordination URLs to use."""
        from .relay import RelayPool
        now = int(self.clock())
        supers = live_supers(events, self.repo_url, self.cfg, now, trusted)
        if self.m["enabled"]:
            if self.reachable is None:
                self.reachable = self._check_reachable(supers)
            action = decide(self.pubkey, bool(self.reachable), self.is_super, supers,
                            max(1, active_agents(events, now)), self.cfg, now)
            if action == "promote":
                self.promote()
            elif action == "demote":
                self.demote()
        if self.is_super and now - self.last_announce >= self.m["announce_minutes"] * 60:
            self.announce(now)
        seeds = list(self.cfg["relay"]["coordination_urls"])
        picks = choose(self.pubkey, [s for s in supers if s["url"] != self.public_url], self.m["connect_to"])
        urls = list(dict.fromkeys(([self.server.url] if self.is_super else []) + seeds + picks))
        if urls != self.attached:
            self.attached = urls
            self.relays.set_coordination(RelayPool(urls, float(self.cfg["relay"]["timeout_seconds"]), 1,
                                                   clock=self.clock))
        return urls

    def _check_reachable(self, supers: List[Dict[str, Any]]) -> bool:
        if self.m["public_url"]:
            return True
        if not self.m["enabled"]:
            return False
        from .relay import LocalRelay, RelayConn, RelayError
        helpers = [s["url"] for s in supers] + list(self.cfg["relay"]["coordination_urls"])
        if not helpers:
            # No super-peer exists yet (a brand-new fleet, no seed): the first
            # agents with a publicly routable address found the mesh. Others
            # dial-back-probe through them from then on, and peers that can't
            # actually connect rank them down (directory.Health).
            if self.m["assume_reachable"]:
                return True
            addr = global_address()
            if addr:
                self.public_url = f"ws://{addr}:0"
                return True
            return False
        probe_server = LocalRelay(self.m["listen_host"], self.m["listen_port"])
        try:
            for url in helpers[:3]:
                try:
                    ok, seen = RelayConn(url, 6).probe(probe_server.port)
                except (RelayError, OSError):
                    continue
                if ok:
                    self.public_url = seen
                    return True
            return False
        finally:
            probe_server.stop()

    def promote(self) -> None:
        if self.is_super:
            return
        from .relay import LocalRelay
        self.server = LocalRelay(self.m["listen_host"], self.m["listen_port"], clock=self.clock)
        if self.m["public_url"]:
            self.public_url = self.m["public_url"]
        elif not self.public_url or self.public_url.rsplit(":", 1)[-1] != str(self.server.port):
            host = (self.public_url or self.server.url).split("//", 1)[1].rsplit(":", 1)[0]
            self.public_url = f"ws://{host}:{self.server.port}"
        self._stop.clear()
        threading.Thread(target=self._sync_loop, daemon=True).start()
        self.announce(int(self.clock()))

    def demote(self) -> None:
        if self.server is None:
            return
        self._stop.set()
        self.server.stop()  # its announce simply expires
        self.server = None

    def announce(self, now: int) -> None:
        ev = N.peer_event(self.seckey, self.repo_url, self.public_url, self.m["peers_per_super"], now,
                          ttl=self.m["announce_minutes"] * 60 * 3, pow_bits=self.cfg["relay"]["pow_bits"])
        try:
            self.relays.milestone.publish(ev)
            self.last_announce = now
        except Exception:
            pass  # try again next refresh

    def _sync_loop(self) -> None:
        since = int(self.clock()) - 3600
        while not self._stop.wait(self.m["sync_seconds"]):
            if self.server is None:
                return
            others = [u for u in self.attached if u != self.server.url]
            self.server.sync_from(others, [N.K_CLAIM], since=since)
            since = int(self.clock()) - 120

    def close(self) -> None:
        self.demote()

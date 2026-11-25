"""Seed-to-mesh handover: where agents find relays, and how they rank them.

Agents start from ``directory.json`` (served by GitHub Pages, cached by a
CDN). It lists the project's **seed** endpoints (the AWS relay and index) and
**community** relays and super-peers that volunteers donate. Each agent keeps
an exponentially weighted record of every endpoint's round-trip time and
success rate. Once ``healthy_community_min`` (5) community endpoints are
healthy, the agent uses them and keeps seeds only as a last resort; until
then seeds are part of the mix.

Nothing is trusted because of who hosts it: every event is signed and
verified, so retiring the seeds is just deleting their lines from
directory.json. The network carries on with volunteers.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ALPHA = 0.3


def fetch(url: str, cache: Path, max_age: int = 3600) -> Dict[str, Any]:
    """directory.json, from cache if fresh; an empty directory if unreachable."""
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < max_age:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass  # best effort: the connection or cache is optional here
    try:
        from .gh import TLS, UA
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10, context=TLS) as r:
            data = json.loads(r.read().decode())
        if not isinstance(data, dict):
            raise ValueError("directory must be an object")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
        return data
    except Exception:  # offline or not published yet: config URLs still work
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            return {}


class Health:
    """Per-endpoint EWMA of RTT and success, persisted between runs."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.data: Dict[str, Dict[str, float]] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text())
            except ValueError:
                self.data = {}

    def record(self, url: str, ok: bool, rtt: float, now: Optional[float] = None) -> None:
        d = self.data.setdefault(url, {"ok": 1.0, "rtt": rtt if ok else 5.0, "at": 0.0})
        d["ok"] = (1 - ALPHA) * d["ok"] + ALPHA * (1.0 if ok else 0.0)
        if ok:
            d["rtt"] = (1 - ALPHA) * d["rtt"] + ALPHA * rtt
        d["at"] = now if now is not None else time.time()

    def healthy(self, url: str) -> bool:
        d = self.data.get(url)
        return bool(d) and d["ok"] >= 0.6 and d["rtt"] < 3.0

    def score(self, url: str) -> float:
        d = self.data.get(url)
        if not d:
            return 2.5  # unknown: middling, so new endpoints get tried
        return d["rtt"] + (1 - d["ok"]) * 10

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))


def select(tier: str, configured: List[str], directory: Dict[str, Any], health: Health, minimum: int,
           limit: int = 8) -> List[str]:
    """The endpoints to use for a tier: community first; seeds only while the community is thin."""
    community = list(dict.fromkeys(configured + list((directory.get("community") or {}).get(tier, []))))
    seeds = [u for u in (directory.get("seeds") or {}).get(tier, []) if u not in community]
    community.sort(key=health.score)
    healthy = [u for u in community if health.healthy(u)]
    if len(healthy) >= minimum:
        return healthy[:limit]  # the handover: seeds drop out entirely
    pick = community[: max(0, limit - len(seeds))]
    return list(dict.fromkeys(pick + seeds))[: limit + len(seeds)]


def probe(urls: List[str], health: Health, timeout: float = 4.0,
          clock: Callable[[], float] = time.time) -> None:
    """Measure each endpoint once (connect + an empty REQ)."""
    from .relay import RelayConn, RelayError
    for u in urls:
        t0 = clock()
        try:
            c = RelayConn(u, timeout)
            c.query([{"kinds": [7651], "limit": 1}])
            c.close()
            health.record(u, True, clock() - t0)
        except (RelayError, OSError):
            health.record(u, False, timeout)
    health.save()

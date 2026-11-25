"""Keep an always-on relay node announced to the mesh.

Runs beside ``relayd`` and a Cloudflare tunnel (``infra/node``). Every
``WS_ANNOUNCE_MINUTES`` it

1. asks cloudflared for the tunnel's current public hostname (a quick
   tunnel gets a random ``*.trycloudflare.com`` name that changes on restart);
2. checks the relay really answers at ``wss://<hostname>`` from the outside;
3. signs a PeerAnnounce for each repository in ``WS_ANNOUNCE_REPOS`` and
   publishes it to the milestone relays.

Agents discover super-peers from those announcements, so the node never
needs a domain name, a DNS record or a line in directory.json, and a new
tunnel hostname is picked up within one announcement period.

The node's signing key is read from ``WS_NODE_KEY_FILE`` (JSON with ``sk``,
as written by ``ws keygen``); it's created there if missing.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

from . import nostr as N
from . import schnorr
from .config import DEFAULTS


def tunnel_host(metrics: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(f"http://{metrics}/quicktunnel", timeout=5) as r:
            h = json.loads(r.read().decode()).get("hostname") or ""
    except (OSError, ValueError):
        return None
    return h if h.endswith(".trycloudflare.com") or os.environ.get("WS_ANY_TUNNEL_HOST") == "1" else None


def load_key(path: Path) -> bytes:
    if path.is_file():
        return bytes.fromhex(json.loads(path.read_text())["sk"])
    sk, pk = schnorr.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"sk": sk.hex(), "npub": schnorr.npub(pk)}, f)
    return sk


def reachable(url: str) -> bool:
    from .relay import RelayConn, RelayError
    try:
        c = RelayConn(url, 10)
        c.query([{"kinds": [N.K_PEER], "limit": 1}])
        c.close()
        return True
    except (RelayError, OSError):
        return False


def announce_once(sk: bytes, url: str, repos: List[str], milestones: List[str], cap: int, pow_bits: int) -> int:
    from .relay import RelayPool
    pool = RelayPool(milestones, 10.0, 1)
    ok = 0
    try:
        for repo in repos:
            ev = N.peer_event(sk, repo, url, cap, int(time.time()), pow_bits=pow_bits)
            res = pool.publish(ev)
            if any(v[0] for v in res.values()):
                ok += 1
            else:
                print(f"announce {repo}: no milestone relay accepted it: {res}", flush=True)
    finally:
        pool.close()
    return ok


def main() -> int:
    rc = DEFAULTS["relay"]
    metrics = os.environ.get("WS_TUNNEL_METRICS", "cloudflared:2000")
    repos = [r.strip() for r in os.environ.get("WS_ANNOUNCE_REPOS", "").split(",") if r.strip()]
    repos = [r if r.startswith("https://") else f"https://github.com/{r}" for r in repos]
    milestones = [u for u in os.environ.get("WS_MILESTONE_URLS", ",".join(rc["milestone_urls"])).split(",") if u]
    every = max(2, int(os.environ.get("WS_ANNOUNCE_MINUTES", "10"))) * 60
    cap = int(os.environ.get("WS_NODE_CAPACITY", "200"))
    pow_bits = int(os.environ.get("WS_RELAY_POW", str(rc["pow_bits"])))
    if not repos:
        print("announce: set WS_ANNOUNCE_REPOS (owner/name,...)", flush=True)
        return 2
    sk = load_key(Path(os.environ.get("WS_NODE_KEY_FILE", "/data/node.json")))
    print(f"announce: node {schnorr.npub(schnorr.pubkey(sk))} for {len(repos)} repositories", flush=True)
    last = ""
    while True:
        host = tunnel_host(metrics)
        if not host:
            print("announce: tunnel not up yet", flush=True)
            time.sleep(15)
            continue
        url = f"wss://{host}"
        if not reachable(url):
            print(f"announce: {url} doesn't answer from outside yet", flush=True)
            time.sleep(20)
            continue
        n = announce_once(sk, url, repos, milestones, cap, pow_bits)
        if url != last:
            print(f"announce: live at {url} ({n}/{len(repos)} announced)", flush=True)
            last = url
        time.sleep(every)


if __name__ == "__main__":
    sys.exit(main())

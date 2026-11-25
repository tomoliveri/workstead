"""relayd: the hardened Workstead relay for seeds, super-peers and volunteers.

``LocalRelay`` (relay.py) is a convenient relay for tests and LANs; this is
the one to expose to the internet. Run it with ``ws relay serve`` or
``python3 -m workstead.relayd``. It needs only Python 3.9+, no packages.

* **Workstead only.** It accepts only Workstead kinds (7651-7661), each
  well-formed, signed and (for agent kinds) carrying NIP-13 proof of work,
  so it can't be abused as a free general-purpose relay.
* **Bounded.** Caps on connections (total and per client IP), subscriptions,
  message size, event rate per IP (token bucket) and stored events/bytes.
* **Origin-locked.** Behind CloudFront, set ``WS_ORIGIN_SECRET``: requests
  without the matching header are refused, so the origin can't be reached
  around the CDN. The client IP comes from the header named by
  ``WS_CLIENT_IP_HEADER`` (``cloudfront-viewer-address`` behind CloudFront,
  ``cf-connecting-ip`` behind a Cloudflare tunnel), else the socket.
* **Expiring.** NIP-40 expirations are honoured; the append-only store is
  compacted on start and hourly.
* **Mesh sync.** ``["ROOTS", sub, filter]`` answers with per-hour Merkle
  roots so super-peers fetch only the buckets that differ (dag.py).
* **Health.** ``GET /health`` for load balancers, NIP-11 info at ``/``.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import dag as D
from . import nostr as N
from .relay import RelayError, Store, WebSocket

AGENT_KINDS = {N.K_CLAIM, N.K_RECEIPT, N.K_PEER, N.K_LOGT, N.K_LOGN, N.K_LOGD, N.K_FINDING, N.K_TOOL}


class Bucket:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self.tokens, self.at = burst, time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class Relayd:
    def __init__(self, host: str = "::", port: int = 8080, state_dir: Optional[Path] = None,
                 origin_secret: str = "", pow_bits: int = 8, max_conns: int = 2000, max_per_ip: int = 24,
                 events_per_sec: float = 4.0, burst: float = 40.0, max_events: int = 400_000,
                 max_message: int = 65536, max_subs: int = 16, name: str = "workstead relay"):
        self.origin_secret = origin_secret
        self.ip_header = os.environ.get("WS_CLIENT_IP_HEADER", "").strip().lower()
        self.pow_bits = pow_bits
        self.max_conns, self.max_per_ip = max_conns, max_per_ip
        self.rate, self.burst = events_per_sec, burst
        self.max_message, self.max_subs = max_message, max_subs
        self.name = name
        self.store = Store(max_events=max_events)
        self.state = Path(state_dir) if state_dir else None
        self.lock = threading.Lock()
        self.conns: Dict[str, int] = {}
        self.buckets: Dict[str, Bucket] = {}
        self.stats = {"accepted": 0, "rejected": 0, "connections": 0, "refused": 0}
        self._log = None
        if self.state:
            self.state.mkdir(parents=True, exist_ok=True)
            self._load()
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.srv = socket.socket(fam, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            self.srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # dual-stack where available
        self.srv.bind((host, port))
        self.srv.listen(512)
        self.port = self.srv.getsockname()[1]
        self.running = True

    # --- persistence ----------------------------------------------------------------

    def _load(self) -> None:
        f = self.state / "events.jsonl"
        if f.is_file():
            with open(f) as fh:
                for line in fh:
                    try:
                        self.store.add(json.loads(line))
                    except ValueError:
                        continue
        self._compact()

    def _compact(self) -> None:
        if not self.state:
            return
        tmp = self.state / "events.jsonl.tmp"
        with self.store.lock:
            evs = list(self.store.events.values())
        now = int(time.time())
        with open(tmp, "w") as fh:
            for ev in evs:
                if not N.expired(ev, now):
                    fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        os.replace(tmp, self.state / "events.jsonl")
        if self._log:
            self._log.close()
        self._log = open(self.state / "events.jsonl", "a")  # noqa: SIM115 (kept open: append-only log)

    def _persist(self, ev: Dict[str, Any]) -> None:
        if self._log:
            self._log.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self._log.flush()

    # --- policy ---------------------------------------------------------------------

    def admit(self, ev: Any, ip: str) -> Tuple[bool, str]:
        if not isinstance(ev, dict) or ev.get("kind") not in N.KINDS:
            return False, "blocked: this relay only carries Workstead events"
        if N.APP_TAG not in (ev.get("tags") or []):
            return False, "blocked: missing the workstead tag"
        if len(json.dumps(ev)) > self.max_message:
            return False, "blocked: event too large"
        if not N.verify(ev):
            return False, "invalid: bad id or signature"
        if ev["created_at"] > time.time() + 900:
            return False, "invalid: created_at too far in the future"
        if ev["kind"] in AGENT_KINDS and not N.pow_ok(ev, self.pow_bits):
            return False, f"pow: needs {self.pow_bits} bits of proof of work (NIP-13)"
        b = self.buckets.setdefault(ip, Bucket(self.rate, self.burst))
        if not b.take():
            return False, "rate-limited: slow down"
        return True, ""

    def client_ip(self, ws: WebSocket, sock: socket.socket) -> str:
        # Only behind a proxy that sets it (WS_CLIENT_IP_HEADER): exposed
        # directly, a client could send any header to dodge per-IP limits.
        if self.ip_header:
            v = ws.headers.get(self.ip_header, "").split(",")[0].strip()
            if v:
                if self.ip_header == "cloudfront-viewer-address":
                    v = v.rsplit(":", 1)[0]  # "addr:port"
                return v.strip("[]")
        try:
            return sock.getpeername()[0]
        except OSError:
            return "?"

    # --- serving --------------------------------------------------------------------

    def serve_forever(self) -> None:
        threading.stack_size(512 * 1024)
        threading.Thread(target=self._housekeeping, daemon=True).start()
        while self.running:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                if not self.running:
                    return
                continue
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def start(self) -> Relayd:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def _housekeeping(self) -> None:
        while self.running:
            time.sleep(3600)
            self.store.query([{"kinds": [0]}])  # purges expired events
            self._compact()
            with self.lock:
                self.buckets = {ip: b for ip, b in self.buckets.items() if b.tokens < b.burst}

    def _http(self, sock: socket.socket, status: str, body: str, ctype: str = "text/plain") -> None:
        data = body.encode()
        sock.sendall(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
                     f"Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n".encode() + data)

    def _serve(self, sock: socket.socket) -> None:
        sock.settimeout(15)
        ws = WebSocket.accept(sock, http=self._plain_http, check=self._check_handshake)
        if ws is None:
            try:
                sock.close()
            except OSError:
                pass
            return
        ip = self.client_ip(ws, sock)
        with self.lock:
            total = sum(self.conns.values())
            if total >= self.max_conns or self.conns.get(ip, 0) >= self.max_per_ip:
                self.stats["refused"] += 1
                ws.close()
                return
            self.conns[ip] = self.conns.get(ip, 0) + 1
            self.stats["connections"] += 1
        subs = 0
        try:
            while self.running and not ws.closed:
                try:
                    msg = ws.recv(120.0)
                except RelayError:
                    break
                if msg is None:
                    break  # idle: free the slot
                if len(msg) > self.max_message:
                    ws.send(json.dumps(["NOTICE", "blocked: message too large"]))
                    continue
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == "EVENT" and len(m) >= 2:
                    ok, note = self.admit(m[1], ip)
                    if ok:
                        ok, note = self.store.add(m[1])
                        if ok and not note:
                            self._persist(m[1])
                    self.stats["accepted" if ok else "rejected"] += 1
                    eid = m[1].get("id", "") if isinstance(m[1], dict) else ""
                    ws.send(json.dumps(["OK", eid, ok, note]))
                elif m[0] == "REQ" and len(m) >= 3 and isinstance(m[1], str) and len(m[1]) <= 64:
                    subs += 1
                    if subs > self.max_subs * 50:
                        ws.send(json.dumps(["CLOSED", m[1], "rate-limited: too many queries"]))
                        break
                    filters = [self._clamp(f) for f in m[2:6] if isinstance(f, dict)]
                    for ev in self.store.query(filters):
                        ws.send(json.dumps(["EVENT", m[1], ev], separators=(",", ":")))
                    ws.send(json.dumps(["EOSE", m[1]]))
                elif m[0] == "ROOTS" and len(m) >= 3 and isinstance(m[2], dict):
                    f = self._clamp(m[2])
                    ws.send(json.dumps(["ROOTS", m[1], {str(k): v for k, v in
                                                        D.bucket_roots(self.store.query([f])).items()}]))
                elif m[0] == "CLOSE":
                    continue
        finally:
            ws.close()
            with self.lock:
                self.conns[ip] = max(0, self.conns.get(ip, 1) - 1)

    def _clamp(self, f: Dict[str, Any]) -> Dict[str, Any]:
        f = dict(f)
        kinds = [k for k in f.get("kinds", list(N.KINDS)) if k in N.KINDS]
        f["kinds"] = kinds or [-1]
        f["limit"] = max(1, min(5000, int(f.get("limit", 5000)) if str(f.get("limit", "")).isdigit() else 5000))
        return f

    def _check_handshake(self, headers: Dict[str, str]) -> Optional[str]:
        if self.origin_secret:
            got = headers.get("x-workstead-origin", "")
            if not hmac.compare_digest(got, self.origin_secret):
                return "403 Forbidden"
        return None

    def _plain_http(self, sock: socket.socket, path: str, headers: Dict[str, str]) -> None:
        if self._check_handshake(headers):
            return self._http(sock, "403 Forbidden", "forbidden")
        if path.startswith("/health"):
            return self._http(sock, "200 OK", json.dumps({"ok": True, "events": len(self.store.events),
                                                          **self.stats}), "application/json")
        if "application/nostr+json" in headers.get("accept", ""):
            info = {"name": self.name, "description": "Workstead coordination relay: Workstead events only.",
                    "software": "workstead relayd", "supported_nips": [1, 11, 13, 40],
                    "limitation": {"max_message_length": self.max_message, "min_pow_difficulty": self.pow_bits,
                                   "max_subscriptions": self.max_subs}}
            return self._http(sock, "200 OK", json.dumps(info), "application/nostr+json")
        return self._http(sock, "200 OK", f"{self.name}: connect with a Nostr client (WebSocket).\n")

    def stop(self) -> None:
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The hardened Workstead relay.")
    ap.add_argument("--host", default=os.environ.get("WS_RELAY_HOST", "::"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WS_RELAY_PORT", "8080")))
    ap.add_argument("--state", default=os.environ.get("STATE_DIRECTORY", ""))
    ap.add_argument("--pow", type=int, default=int(os.environ.get("WS_RELAY_POW", "8")))
    a = ap.parse_args(argv)
    secret = os.environ.get("WS_ORIGIN_SECRET", "")
    for sf in (os.environ.get("WS_ORIGIN_SECRET_FILE", ""),
               os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", ""), "origin")):
        if not secret and sf and Path(sf).is_file():
            secret = Path(sf).read_text().strip()
    if not secret and os.environ.get("WS_REQUIRE_ORIGIN") == "1":
        print("relayd: WS_REQUIRE_ORIGIN=1 but no origin secret was found; refusing to run open", flush=True)
        return 2
    r = Relayd(a.host, a.port, Path(a.state) if a.state else None, secret, a.pow)
    print(f"workstead relayd on [{a.host}]:{r.port}, origin lock {'on' if secret else 'off'}", flush=True)
    r.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

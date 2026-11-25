"""Nostr relay transport: a stdlib WebSocket client, multi-relay pools, a
local relay server, and an in-memory network for simulation.

Two tiers (see docs/relay.md):

* **milestone** relays (free public relays) carry TaskPublished,
  VerificationReceipt, Endorsement and TaskSettled: a few tiny, expiring
  events per completed task.
* **coordination** relays (volunteer strfry, or ``ws relay serve``) carry
  claims and lease renewals, the high-frequency chatter that free social
  relays shouldn't be asked to store.

Relays are untrusted: every received event is verified (id and signature)
and checked for expiry before use, results from all relays of a tier are
merged and de-duplicated by id, and events one relay is missing are gossiped
to it. A relay that fails is retried with exponential backoff and jitter;
any one relay answering is enough for eventual consistency.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import nostr as N

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class RelayError(RuntimeError):
    pass


# --- RFC 6455 WebSocket (client and server framing) -----------------------------------


def _frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocket:
    """A minimal, blocking WebSocket endpoint over a connected socket."""

    MAX_MESSAGE = 4 * 1024 * 1024

    def __init__(self, sock: socket.socket, client: bool):
        self.sock = sock
        self.client = client
        self.buf = b""
        self.lock = threading.Lock()
        self.closed = False
        self.headers: Dict[str, str] = {}
        self.path = "/"

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0, tls: Optional[ssl.SSLContext] = None,
                headers: Optional[Dict[str, str]] = None) -> WebSocket:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise RelayError(f"not a websocket URL: {url}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            if tls is None:
                from .gh import TLS
                tls = TLS
            if tls.minimum_version < ssl.TLSVersion.TLSv1_2:
                tls.minimum_version = ssl.TLSVersion.TLSv1_2
            raw = tls.wrap_socket(raw, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        host = u.hostname + (f":{u.port}" if u.port else "")
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if "\r" not in k + v and "\n" not in k + v)
        raw.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: workstead/1\r\n{extra}\r\n").encode())
        ws = cls(raw, client=True)
        head = ws._read_until(b"\r\n\r\n", timeout)
        lines = head.decode("latin-1").split("\r\n")
        if lines[0].split(" ")[1:2] != ["101"]:
            raw.close()
            raise RelayError(f"handshake refused: {lines[0]}")
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k}
        if hdrs.get("sec-websocket-accept") != want:
            raw.close()
            raise RelayError("handshake failed: bad Sec-WebSocket-Accept")
        return ws

    @classmethod
    def accept(cls, sock: socket.socket, http=None, check=None) -> Optional[WebSocket]:
        """Server side. http(sock, path, headers) answers plain requests;
        check(headers) may refuse a handshake by returning an HTTP status."""
        ws = cls(sock, client=False)
        try:
            head = ws._read_until(b"\r\n\r\n", 10).decode("latin-1")
        except (OSError, RelayError):
            return None
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        ws.path = parts[1] if len(parts) > 1 else "/"
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k}
        ws.headers = hdrs
        key = hdrs.get("sec-websocket-key")
        if not key:
            try:
                if http is not None:
                    http(sock, ws.path, hdrs)
                else:
                    sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass  # best effort: the connection or cache is optional here
            return None
        refusal = check(hdrs) if check is not None else None
        if refusal:
            try:
                sock.sendall(f"HTTP/1.1 {refusal}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            except OSError:
                pass  # best effort: the connection or cache is optional here
            return None
        acc = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        return ws

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        end = time.time() + timeout
        while marker not in self.buf:
            if time.time() > end:
                raise RelayError("timed out")
            self._fill(max(0.05, end - time.time()))
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _fill(self, timeout: float) -> None:
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            raise RelayError("timed out") from None
        except (OSError, ssl.SSLError) as e:
            self.closed = True
            raise RelayError(f"connection lost: {e}") from None
        if not data:
            self.closed = True
            raise RelayError("connection closed")
        self.buf += data

    def _take(self, n: int, end: float) -> bytes:
        while len(self.buf) < n:
            if time.time() > end:
                raise RelayError("timed out")
            self._fill(max(0.05, end - time.time()))
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def _send(self, opcode: int, payload: bytes) -> None:
        with self.lock:
            try:
                self.sock.sendall(_frame(opcode, payload, mask=self.client))
            except (OSError, ssl.SSLError) as e:
                self.closed = True
                raise RelayError(f"send failed: {e}") from None

    def recv(self, timeout: float) -> Optional[str]:
        """The next text message, or None on timeout."""
        end = time.time() + timeout
        parts: List[bytes] = []
        while True:
            try:
                b1, b2 = self._take(2, end)
            except RelayError as e:
                if "timed out" in str(e) and not parts:
                    return None
                raise
            opcode, length, masked = b1 & 0x0F, b2 & 0x7F, b2 & 0x80
            if length == 126:
                length = struct.unpack(">H", self._take(2, end))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._take(8, end))[0]
            if length > self.MAX_MESSAGE:
                self.close()
                raise RelayError("message too large")
            key = self._take(4, end) if masked else b""
            payload = self._take(length, end + 30)
            if masked:
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                self.closed = True
                try:
                    self._send(0x8, b"")
                except RelayError:
                    pass  # best effort: the connection or cache is optional here
                raise RelayError("closed by peer")
            if opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        if not self.closed:
            try:
                self._send(0x8, b"")
            except RelayError:
                pass  # best effort: the connection or cache is optional here
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass  # best effort: the connection or cache is optional here


# --- one relay connection --------------------------------------------------------------


class RelayConn:
    """A reconnecting connection to one relay, with backoff and jitter."""

    def __init__(self, url: str, timeout: float = 8.0, rng: Optional[random.Random] = None,
                 headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.ws: Optional[WebSocket] = None
        self.failures = 0
        self.retry_at = 0.0
        self.lock = threading.Lock()
        self.rng = rng or random.Random()
        self.subs = 0

    def _ensure(self) -> WebSocket:
        if self.ws is not None and not self.ws.closed:
            return self.ws
        if time.time() < self.retry_at:
            raise RelayError(f"{self.url}: backing off")
        try:
            self.ws = WebSocket.connect(self.url, timeout=self.timeout, headers=self.headers)
            self.failures = 0
            return self.ws
        except (OSError, RelayError, ssl.SSLError) as e:
            self._failed()
            raise RelayError(f"{self.url}: {e}") from None

    def _failed(self) -> None:
        self.failures += 1
        delay = min(300.0, 2.0 * (2 ** min(self.failures, 8)))
        self.retry_at = time.time() + delay * self.rng.uniform(0.5, 1.5)
        if self.ws is not None:
            self.ws.close()
        self.ws = None

    def publish(self, ev: Dict[str, Any]) -> Tuple[bool, str]:
        with self.lock:
            for attempt in range(2):
                try:
                    ws = self._ensure()
                    ws.send(json.dumps(["EVENT", ev], separators=(",", ":")))
                    end = time.time() + self.timeout
                    while time.time() < end:
                        msg = ws.recv(end - time.time())
                        if msg is None:
                            break
                        try:
                            m = json.loads(msg)
                        except ValueError:
                            continue  # try the next one
                        if isinstance(m, list) and m[:1] == ["OK"] and len(m) >= 3 and m[1] == ev["id"]:
                            ok = bool(m[2])
                            note = str(m[3]) if len(m) > 3 else ""
                            # A duplicate is a success: the relay has it.
                            return ok or note.startswith("duplicate"), note
                    return False, "no OK from relay"
                except RelayError as e:
                    if attempt == 0 and "backing off" not in str(e):
                        self._failed()
                        self.retry_at = 0.0  # one immediate reconnect
                        continue
                    return False, str(e)
            return False, "unreachable"

    def query(self, filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self.lock:
            ws = self._ensure()
            self.subs += 1
            sub = f"ws{self.subs}{os.urandom(3).hex()}"
            out: List[Dict[str, Any]] = []
            try:
                ws.send(json.dumps(["REQ", sub] + filters, separators=(",", ":")))
                end = time.time() + self.timeout
                while time.time() < end:
                    msg = ws.recv(end - time.time())
                    if msg is None:
                        break
                    try:
                        m = json.loads(msg)
                    except ValueError:
                        continue  # try the next one
                    if not isinstance(m, list) or len(m) < 2 or m[1] != sub:
                        continue
                    if m[0] == "EVENT" and len(m) >= 3:
                        out.append(m[2])
                    elif m[0] in ("EOSE", "CLOSED"):
                        break
                try:
                    ws.send(json.dumps(["CLOSE", sub]))
                except RelayError:
                    pass  # best effort: the connection or cache is optional here
            except RelayError:
                self._failed()
                raise
            return out

    def roots(self, f: Dict[str, Any]) -> Dict[int, str]:
        """The relay's per-hour Merkle roots for a filter (mesh catch-up)."""
        with self.lock:
            ws = self._ensure()
            sub = f"rt{os.urandom(3).hex()}"
            ws.send(json.dumps(["ROOTS", sub, f]))
            end = time.time() + self.timeout
            while time.time() < end:
                msg = ws.recv(end - time.time())
                if msg is None:
                    break
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue  # try the next one
                if isinstance(m, list) and m[:2] == ["ROOTS", sub] and len(m) >= 3 and isinstance(m[2], dict):
                    return {int(k): str(v) for k, v in m[2].items() if str(k).isdigit()}
            raise RelayError(f"{self.url}: no ROOTS answer")

    def probe(self, port: int) -> Tuple[bool, str]:
        """Ask this relay (a super-peer) to dial us back on port."""
        with self.lock:
            ws = self._ensure()
            ws.send(json.dumps(["PROBE", port]))
            end = time.time() + self.timeout
            while time.time() < end:
                msg = ws.recv(end - time.time())
                if msg is None:
                    break
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue  # try the next one
                if isinstance(m, list) and m[:1] == ["PROBE"] and len(m) >= 3:
                    return bool(m[1]), str(m[2])
            return False, ""

    def close(self) -> None:
        if self.ws:
            self.ws.close()
        self.ws = None


# --- a tier of relays ------------------------------------------------------------------


class RelayPool:
    """Publish to and query every relay in a tier; merge, verify, gossip."""

    def __init__(self, urls: Iterable[str], timeout: float = 8.0, min_ok: int = 1,
                 conn_factory: Optional[Callable[[str], Any]] = None, clock: Callable[[], float] = time.time):
        self.urls = list(urls)
        self.min_ok = min_ok
        self.clock = clock
        factory = conn_factory or (lambda u: RelayConn(u, timeout))
        self.conns = {u: factory(u) for u in self.urls}
        self.pool = ThreadPoolExecutor(max_workers=max(1, min(16, len(self.urls))))
        self.stats = {"published": 0, "queried": 0, "dropped_invalid": 0, "gossiped": 0, "failures": 0}

    def publish(self, ev: Dict[str, Any]) -> Dict[str, Tuple[bool, str]]:
        res = dict(zip(self.urls, self.pool.map(lambda u: self._safe_publish(u, ev), self.urls)))
        self.stats["published"] += 1
        if sum(1 for ok, _ in res.values() if ok) < self.min_ok:
            raise RelayError("no relay accepted the event: " + "; ".join(f"{u}: {m}" for u, (_, m) in res.items()))
        return res

    def _safe_publish(self, url: str, ev: Dict[str, Any]) -> Tuple[bool, str]:
        try:
            return self.conns[url].publish(ev)
        except Exception as e:  # one bad relay never breaks the pool
            self.stats["failures"] += 1
            return False, str(e)

    def _safe_query(self, url: str, filters: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        try:
            return self.conns[url].query(filters)
        except Exception:
            self.stats["failures"] += 1
            return None

    def query(self, filters: List[Dict[str, Any]], gossip: bool = True, gossip_limit: int = 50) -> List[Dict[str, Any]]:
        """Events matching filters from every reachable relay, verified and de-duplicated."""
        answers = dict(zip(self.urls, self.pool.map(lambda u: self._safe_query(u, filters), self.urls)))
        self.stats["queried"] += 1
        now = int(self.clock())
        merged: Dict[str, Dict[str, Any]] = {}
        seen_on: Dict[str, set] = {}
        for url, evs in answers.items():
            for ev in evs or []:
                eid = ev.get("id") if isinstance(ev, dict) else None
                if not isinstance(eid, str):
                    self.stats["dropped_invalid"] += 1
                    continue
                if eid not in merged:
                    if not N.verify(ev) or N.expired(ev, now):
                        self.stats["dropped_invalid"] += 1
                        continue
                    merged[eid] = ev
                seen_on.setdefault(eid, set()).add(url)
        if gossip:
            alive = [u for u, a in answers.items() if a is not None]
            sent = 0
            for eid, ev in merged.items():
                missing = [u for u in alive if u not in seen_on.get(eid, ())]
                for u in missing:
                    if sent >= gossip_limit:
                        break
                    self._safe_publish(u, ev)
                    sent += 1
            self.stats["gossiped"] += sent
        return sorted(merged.values(), key=lambda e: (e["created_at"], e["id"]))

    def close(self) -> None:
        for c in self.conns.values():
            c.close()
        self.pool.shutdown(wait=False)


class Relays:
    """Both tiers; events are routed by kind (claims to coordination)."""

    def __init__(self, milestone: RelayPool, coordination: RelayPool):
        self.milestone = milestone
        self.coordination = coordination

    @classmethod
    def from_config(cls, cfg: Dict[str, Any], clock: Callable[[], float] = time.time) -> Relays:
        rc = cfg["relay"]
        t = float(rc["timeout_seconds"])
        return cls(RelayPool(rc["milestone_urls"], t, rc["min_relays_ok"], clock=clock),
                   RelayPool(rc["coordination_urls"], t, 1, clock=clock))

    @property
    def degraded(self) -> bool:
        """No mesh: claims fall back to milestone relays (one per task, no renewals)."""
        return not self.coordination.urls

    def set_coordination(self, pool: RelayPool) -> None:
        old, self.coordination = self.coordination, pool
        if old is not pool:
            old.close()

    def publish(self, ev: Dict[str, Any]) -> Dict[str, Tuple[bool, str]]:
        coord = N.tier(ev["kind"]) == "coordination" and not self.degraded
        return (self.coordination if coord else self.milestone).publish(ev)

    def snapshot(self, repo_url: str, since: int = 0) -> List[Dict[str, Any]]:
        """Every Workstead event for a repository, from both tiers."""
        f = {"kinds": [N.K_TASK, N.K_RECEIPT, N.K_ENDORSE, N.K_SETTLED, N.K_PEER], "#r": [repo_url]}
        c = {"kinds": [N.K_CLAIM], "#r": [repo_url]}
        if since:
            f["since"] = since
            c["since"] = since
        if self.degraded:
            return self.milestone.query([f, c])
        evs = self.milestone.query([f]) + self.coordination.query([c])
        # A claim on a milestone relay (or a milestone on a coordination one)
        # still counts if valid; tiers are routing, not trust.
        uniq = {e["id"]: e for e in evs}
        return sorted(uniq.values(), key=lambda e: (e["created_at"], e["id"]))

    def close(self) -> None:
        self.milestone.close()
        self.coordination.close()


# --- relay semantics shared by the local server and the in-memory relay ----------------


def matches(ev: Dict[str, Any], f: Dict[str, Any]) -> bool:
    if "ids" in f and not any(ev["id"].startswith(i) for i in f["ids"]):
        return False
    if "authors" in f and not any(ev["pubkey"].startswith(a) for a in f["authors"]):
        return False
    if "kinds" in f and ev["kind"] not in f["kinds"]:
        return False
    if "since" in f and ev["created_at"] < f["since"]:
        return False
    if "until" in f and ev["created_at"] > f["until"]:
        return False
    for k, vals in f.items():
        if k.startswith("#") and len(k) == 2:
            have = {t[1] for t in ev["tags"] if len(t) >= 2 and t[0] == k[1]}
            if not have & set(vals):
                return False
    return True


class Store:
    """An event store with relay rules: verify, dedupe, honour NIP-40 expiry."""

    def __init__(self, clock: Callable[[], float] = time.time, max_events: int = 1_000_000):
        self.events: Dict[str, Dict[str, Any]] = {}
        self.clock = clock
        self.max_events = max_events
        self.lock = threading.Lock()

    def add(self, ev: Any) -> Tuple[bool, str]:
        if not N.verify(ev):
            return False, "invalid: bad id or signature"
        if N.expired(ev, int(self.clock())):
            return False, "invalid: event has expired"
        with self.lock:
            if ev["id"] in self.events:
                return True, "duplicate: already have it"
            if len(self.events) >= self.max_events:
                return False, "blocked: relay full"
            self.events[ev["id"]] = ev
        return True, ""

    def query(self, filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = int(self.clock())
        with self.lock:
            for eid in [i for i, e in self.events.items() if N.expired(e, now)]:
                del self.events[eid]
            evs = list(self.events.values())
        out = []
        for f in filters:
            hits = sorted((e for e in evs if matches(e, f)), key=lambda e: -e["created_at"])
            out.extend(hits[: int(f.get("limit", 10000))])
        return list({e["id"]: e for e in out}.values())


def _listen(host: str, port: int) -> socket.socket:
    """A listening socket. Wildcard hosts listen on IPv6 and IPv4 together where possible,
    so a super-peer is reachable at its global IPv6 address as well as through IPv4."""
    if host in ("0.0.0.0", "::", "") and socket.has_ipv6:
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            s.bind(("::", port))
            s.listen(128)
            return s
        except OSError:
            s.close()  # no IPv6 stack: fall back to IPv4 only
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(128)
    return s


class LocalRelay:
    """A real Nostr relay over WebSockets: for tests, and for `ws relay serve`.

    A volunteer coordination relay can be this (fine for a project's fleet)
    or strfry for heavier use.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, clock: Callable[[], float] = time.time):
        self.store = Store(clock)
        self.srv = _listen(host, port)
        self.port = self.srv.getsockname()[1]
        local = "127.0.0.1" if host in ("0.0.0.0", "::", "") else (f"[{host}]" if ":" in host else host)
        self.url = f"ws://{local}:{self.port}"  # how this machine reaches its own relay
        self.clients: List[WebSocket] = []
        self.running = True
        self.received = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while self.running:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        ws = WebSocket.accept(sock)
        if ws is None:
            sock.close()
            return
        self.clients.append(ws)
        try:
            while self.running and not ws.closed:
                try:
                    msg = ws.recv(1.0)
                except RelayError:
                    break
                if msg is None:
                    continue
                try:
                    m = json.loads(msg)
                except ValueError:
                    ws.send(json.dumps(["NOTICE", "invalid: not JSON"]))
                    continue
                if not isinstance(m, list) or not m:
                    continue
                if m[0] == "EVENT" and len(m) >= 2:
                    self.received += 1
                    ok, note = self.store.add(m[1])
                    eid = m[1].get("id", "") if isinstance(m[1], dict) else ""
                    ws.send(json.dumps(["OK", eid, ok, note]))
                elif m[0] == "REQ" and len(m) >= 3:
                    for ev in self.store.query([f for f in m[2:] if isinstance(f, dict)]):
                        ws.send(json.dumps(["EVENT", m[1], ev], separators=(",", ":")))
                    ws.send(json.dumps(["EOSE", m[1]]))
                elif m[0] == "CLOSE":
                    continue
                elif m[0] == "ROOTS" and len(m) >= 3 and isinstance(m[2], dict):
                    from . import dag as _dag
                    ws.send(json.dumps(["ROOTS", m[1], {str(k): v for k, v in
                                                        _dag.bucket_roots(self.store.query([m[2]])).items()}]))
                elif m[0] == "PROBE" and len(m) >= 2 and isinstance(m[1], int) and 0 < m[1] < 65536:
                    # STUN-like dial-back: is the asking peer reachable at our
                    # view of its address? Lets a peer decide to be a super-peer.
                    host = sock.getpeername()[0]
                    if host.startswith("::ffff:"):
                        host = host[7:]  # IPv4 peer on a dual-stack socket
                    url = f"ws://[{host}]:{m[1]}" if ":" in host else f"ws://{host}:{m[1]}"
                    ok = False
                    try:
                        WebSocket.connect(url, timeout=4).close()
                        ok = True
                    except (OSError, RelayError):
                        pass  # best effort: the connection or cache is optional here
                    ws.send(json.dumps(["PROBE", ok, url]))
        finally:
            ws.close()

    def sync_from(self, urls: Iterable[str], kinds: List[int], since: int = 0, timeout: float = 5.0) -> int:
        """Super-peer mesh replication: pull events we lack from other super-peers."""
        from . import dag as _dag
        added = 0
        for u in urls:
            if u == self.url:
                continue
            try:
                conn = RelayConn(u, timeout)
                f: Dict[str, Any] = {"kinds": kinds}
                if since:
                    f["since"] = since
                # Git-style catch-up: compare per-hour Merkle roots, fetch only what differs.
                try:
                    theirs = conn.roots(f)
                    mine = _dag.bucket_roots(self.store.query([f]))
                    buckets = _dag.differing(mine, theirs)
                    filters = [dict(f, since=b, until=b + _dag.BUCKET - 1) for b in buckets if b in theirs]
                except RelayError:
                    filters = [f]  # peer without ROOTS: plain pull
                for flt in filters[:48]:
                    for ev in conn.query([flt]):
                        ok, note = self.store.add(ev)
                        if ok and not note:
                            added += 1
                conn.close()
            except (RelayError, OSError):
                continue  # try the next one
        return added

    def drop_connections(self) -> None:
        """Fault injection: cut every client connection."""
        for ws in list(self.clients):
            ws.close()
        self.clients.clear()

    def stop(self) -> None:
        self.running = False
        self.drop_connections()
        try:
            self.srv.close()
        except OSError:
            pass  # best effort: the connection or cache is optional here


# --- the in-memory network (simulation) ----------------------------------------------------


class MemRelay:
    """A relay in a simulated network: lag before events are visible, random drops."""

    def __init__(self, name: str, clock: Callable[[], float], lag: float = 0.0, drop: float = 0.0,
                 rng: Optional[random.Random] = None, down: bool = False):
        self.name = name
        self.store = Store(clock)
        self.clock = clock
        self.lag = lag
        self.drop = drop
        self.rng = rng or random.Random()
        self.down = down
        self.pending: List[Tuple[float, Dict[str, Any]]] = []

    def _flush(self) -> None:
        now = self.clock()
        keep = []
        for t, ev in self.pending:
            if t <= now:
                self.store.add(ev)
            else:
                keep.append((t, ev))
        self.pending = keep

    def publish(self, ev: Dict[str, Any]) -> Tuple[bool, str]:
        if self.down:
            raise RelayError(f"{self.name} is down")
        if self.rng.random() < self.drop:
            return False, "dropped"
        if not N.verify(ev):
            return False, "invalid"
        self.pending.append((self.clock() + self.lag, ev))
        return True, ""

    def query(self, filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.down:
            raise RelayError(f"{self.name} is down")
        self._flush()
        return self.store.query(filters)

    def close(self) -> None:
        pass


def mem_pool(relays: List[MemRelay], clock: Callable[[], float], min_ok: int = 1) -> RelayPool:
    by = {r.name: r for r in relays}
    pool = RelayPool([r.name for r in relays], min_ok=min_ok, conn_factory=lambda name: by[name], clock=clock)
    pool.pool.shutdown(wait=False)

    class _Sync:  # deterministic, single-threaded map for simulation
        @staticmethod
        def map(fn, items):
            return [fn(x) for x in items]

        @staticmethod
        def shutdown(wait=False):
            pass
    pool.pool = _Sync()
    return pool

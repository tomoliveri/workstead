"""DAG, log library, directory handover, relayd policy, Merkle sync, mesh election, settlement gate."""
import json
import random
import time
import unittest

from harness import Repo
from workstead import config as C, dag as D, directory as DIR, logs as LG, mesh as M, nostr as N, schnorr as S
from workstead.relay import LocalRelay, RelayConn, RelayError, WebSocket
from workstead.relayd import Relayd

PUB = bytes.fromhex("11" * 32)
REPO = "https://github.com/acme/project"


def cfg(**relay):
    r = {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 4, "quorum": 2}
    r.update(relay)
    return C.load(data={"repo": "acme/project", "relay": r})


def key(i):
    return bytes([i]) * 32


class Dag(unittest.TestCase):
    def test_tips_parents_and_merkle(self):
        t = N.task_event(PUB, REPO, "a" * 40, {"title": "T"}, 5, 2, 1000)
        args = (t["id"], REPO, "a" * 40, "b" * 40, "c" * 64, "d" * 64, "pass")
        p = N.receipt_event(key(2), *args, "produce", 1100, ref="https://x.example/r.git#b", parents=[t["id"]])
        self.assertEqual([e["id"] for e in D.tips([t, p])], [p["id"]])           # t is now referenced
        self.assertEqual(D.select_parents([t, p], S.pubkey(key(3)).hex(), random.Random(1)), [p["id"]])
        a, b = D.bucket_roots([t, p]), D.bucket_roots([t])
        self.assertEqual(D.differing(a, a), [])
        self.assertEqual(D.differing(a, b), [0])
        self.assertEqual(D.merkle_root([t["id"], p["id"]]), D.merkle_root([t["id"], p["id"]]))

    def test_proof_of_verification(self):
        c = cfg(trusted_agents=[S.pubkey(key(i)).hex() for i in range(2, 9)])
        t1 = N.task_event(PUB, REPO, "a" * 40, {"title": "T1"}, 5, 2, 1000)
        t2 = N.task_event(PUB, REPO, "a" * 40, {"title": "T2"}, 5, 2, 1001)
        def r(sk, t, out, role, at, **kw):
            return N.receipt_event(sk, t["id"], REPO, "a" * 40, out, "c" * 64, "d" * 64, "pass", role, at, pow_bits=4, **kw)
        ref = "https://x.example/r.git#b"
        # First producer: nothing existed to verify, so no fee is owed (bootstrap).
        p1 = r(key(2), t1, "1" * 40, "produce", 1100, ref=ref)
        v1, v2 = r(key(3), t1, "1" * 40, "verify", 1110), r(key(4), t1, "1" * 40, "verify", 1120)
        # Second producer submits without verifying anything, though p1 was there: doesn't count.
        cheat = r(key(5), t2, "2" * 40, "produce", 1200, ref=ref)
        w1, w2 = r(key(6), t2, "2" * 40, "verify", 1210), r(key(7), t2, "2" * 40, "verify", 1220)
        st = N.compute([t1, t2, p1, v1, v2, cheat, w1, w2], c, REPO, 1300)
        self.assertEqual(st[t1["id"]]["status"], "verified")
        self.assertNotEqual(st[t2["id"]]["status"], "verified")
        # An honest producer verifies the available tip first, then submits: counts.
        # Two tips were available (p1 and the cheat's patch), so two verifications are owed.
        fee1 = r(key(8), t1, "1" * 40, "verify", 1230)
        honest_early = r(key(8), t2, "3" * 40, "produce", 1232, ref=ref)
        st = N.compute([t1, t2, p1, v1, v2, cheat, w1, w2, fee1, honest_early,
                        r(key(6), t2, "3" * 40, "verify", 1233), r(key(7), t2, "3" * 40, "verify", 1234)], c, REPO, 1300)
        self.assertNotEqual(st[t2["id"]]["output"], "3" * 40)  # one verification isn't enough
        fee2 = r(key(8), t2, "2" * 40, "verify", 1235)
        honest = r(key(8), t2, "3" * 40, "produce", 1240, ref=ref)
        h1, h2 = r(key(6), t2, "3" * 40, "verify", 1250), r(key(7), t2, "3" * 40, "verify", 1260)
        st = N.compute([t1, t2, p1, v1, v2, cheat, w1, w2, fee1, fee2, honest, h1, h2], c, REPO, 1300)
        self.assertEqual((st[t2["id"]]["status"], st[t2["id"]]["output"]), ("verified", "3" * 40))

    def test_bans(self):
        c = cfg(trusted_agents=[S.pubkey(key(i)).hex() for i in range(2, 6)])
        t = N.task_event(PUB, REPO, "a" * 40, {"title": "T"}, 5, 1, 1000)
        claim = N.claim_event(key(2), t["id"], REPO, 1001, 3600, 4)
        self.assertEqual(N.compute([t, claim], c, REPO, 1100)[t["id"]]["holder"], S.pubkey(key(2)).hex())
        ban = N.ban_event(PUB, REPO, [S.pubkey(key(2)).hex()], "sybil", 1050)
        self.assertEqual(N.compute([t, claim, ban], c, REPO, 1100)[t["id"]]["status"], "open")


class Logs(unittest.TestCase):
    def test_redaction_templates_and_budget(self):
        line = "2026-01-02T03:04:05Z ERROR login failed for bob@example.com token=ghp_abcdefghijklmnopqrstuvwxyz0123 at /srv/app/x.py:12"
        tmpl = LG.template(line)
        for leak in ("bob@", "ghp_", "/srv/app", "12"):
            self.assertNotIn(leak, tmpl)
        text = "\n".join([f"INFO step {i} ok in {i}ms" for i in range(3000)] + [line])
        out = LG.compact(text, max_lines=10)
        self.assertLess(len(out), len(text) / 50)
        self.assertIn("error", out)
        self.assertNotIn("ghp_abc", out)

    def test_library_is_content_addressed_and_events_validate(self):
        lib = LG.Library()
        self.assertFalse(lib.learn("0" * 16, "forged template"))
        d = LG.digest("ERROR boom 1\nERROR boom 2\nok")
        evs = LG.template_events(key(2), REPO, d, lib, 1000, pow_bits=4)
        self.assertEqual(len(evs), 2)
        self.assertTrue(all(LG.well_formed(e) and N.verify(e) for e in evs))
        dig = LG.digest_event(key(2), REPO, "ab" * 32, d, 1000, pow_bits=4)
        self.assertTrue(LG.well_formed(dig))
        self.assertEqual(json.loads(dig["content"])["sigs"][0][1], 2)  # "boom N" collapsed into one signature
        note = LG.note_event(key(3), REPO, d["sigs"][0][0], "flaky: retry with --seed", 1001, pow_bits=4)
        lib2 = LG.Library()
        lib2.ingest(evs + [note])
        self.assertIn("flaky", LG.compact("ERROR boom 7", lib2))


class Directory(unittest.TestCase):
    def test_seeds_drop_out_once_the_community_is_healthy(self):
        h = DIR.Health()
        d = {"seeds": {"coordination": ["wss://seed"]}, "community": {"coordination": [f"wss://c{i}" for i in range(6)]}}
        self.assertIn("wss://seed", DIR.select("coordination", [], d, h, 5))
        for i in range(4):
            h.record(f"wss://c{i}", True, 0.1)
        self.assertIn("wss://seed", DIR.select("coordination", [], d, h, 5))
        h.record("wss://c4", True, 0.2)
        picked = DIR.select("coordination", [], d, h, 5)
        self.assertNotIn("wss://seed", picked)
        self.assertEqual(picked[0], "wss://c0")


class RelaydPolicy(unittest.TestCase):
    def setUp(self):
        self.r = Relayd("127.0.0.1", 0, origin_secret="s", pow_bits=4, events_per_sec=1, burst=3).start()
        self.url = f"ws://127.0.0.1:{self.r.port}"
        self.conn = RelayConn(self.url, 3, headers={"X-Workstead-Origin": "s"})

    def tearDown(self):
        self.conn.close()
        self.r.stop()

    def test_only_workstead_events_with_pow_and_rate_limits(self):
        foreign = N.build(key(2), 1, [], "hello world", int(time.time()))
        self.assertFalse(self.conn.publish(foreign)[0])
        weak = N.claim_event(key(2), "ab" * 32, REPO, int(time.time()), 600, pow_bits=0)
        ok, note = self.conn.publish(weak)
        self.assertFalse(ok)
        self.assertIn("pow", note)
        good = [N.claim_event(key(i), "ab" * 32, REPO, int(time.time()), 600, pow_bits=4) for i in range(2, 8)]
        results = [self.conn.publish(e)[0] for e in good]
        self.assertTrue(results[0])
        self.assertIn(False, results)  # the token bucket kicked in
        got = self.conn.query([{"kinds": [N.K_CLAIM]}])
        self.assertTrue(got and all(N.verify(e) for e in got))
        with self.assertRaises(RelayError):
            WebSocket.connect(self.url, 3)  # no origin secret

    def test_roots_and_merkle_catch_up(self):
        a, b = LocalRelay(), LocalRelay()
        try:
            evs = [N.claim_event(key(i), "ab" * 32, REPO, 1_800_000_000 + 4000 * i, 600, pow_bits=4) for i in range(2, 6)]
            for e in evs:
                a.store.add(e)
            b.store.add(evs[0])
            self.assertEqual(b.sync_from([a.url], [N.K_CLAIM]), 3)
            self.assertEqual(set(b.store.events), set(a.store.events))
            self.assertEqual(b.sync_from([a.url], [N.K_CLAIM]), 0)  # roots match: nothing to fetch
        finally:
            a.stop()
            b.stop()


class Election(unittest.TestCase):
    def test_promotion_is_bounded_and_excess_demotes(self):
        c = cfg()
        now = 1_800_000_000
        agents = [S.pubkey(key(i)).hex() for i in range(1, 201)]
        promoted = sum(M.decide(a, True, False, [], 200, c, now) == "promote" for a in agents)
        self.assertTrue(3 <= promoted <= 40, promoted)  # target is 10; the lottery spreads promotions
        supers = [{"pubkey": a, "url": f"ws://{i}", "trusted": False} for i, a in enumerate(agents[:30])]
        demote = sum(M.decide(a, True, True, supers, 100, c, now) == "demote" for a in agents[:30])
        self.assertEqual(demote, 30 - M.target_supers(100, c))
        self.assertEqual(M.decide(agents[0], False, False, [], 200, c, now), "stay")  # unreachable never promotes
        picks = M.choose(agents[0], supers, 3)
        self.assertEqual(picks, M.choose(agents[0], supers, 3))
        self.assertEqual(len(set(picks)), 3)


class Gate(unittest.TestCase):
    def test_settlement_decisions(self):
        from workstead.settle import settle_pr
        r = Repo({"relay": {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 0, "pov_min_prior_verifications": 0,
                            "trusted_agents": [S.pubkey(key(i)).hex() for i in range(2, 6)]}})
        now = int(r.w.clock())
        t = N.task_event(PUB, "https://github.com/acme/project", "a" * 40, {"title": "T"}, 5, 2, now - 500)
        out = "f" * 40
        pr = r.as_("worker").open_pr("T", "Done.\n\n<!-- ws:relay " + json.dumps({"task": t["id"]}) + " -->",
                                    "ws/relay-x", ["src/app.py"], sha=out)
        def rc(sk, role, at):
            return N.receipt_event(sk, t["id"], "https://github.com/acme/project", "a" * 40, out, "c" * 64, "d" * 64,
                                   "pass", role, at, ref="https://x.example/r.git#b" if role == "produce" else None)
        events = [t, rc(key(2), "produce", now - 400)]
        q = r.fresh_queue()
        self.assertEqual(settle_pr(q, None, pr, events)["decision"], "waiting")
        events += [rc(key(3), "verify", now - 300), rc(key(4), "verify", now - 200)]
        r.w.checks[out] = "pending"
        self.assertEqual(settle_pr(q, None, pr, events)["decision"], "waiting")
        r.w.checks[out] = "success"
        res = settle_pr(q, None, pr, events)
        self.assertEqual(res["decision"], "merged", res)
        self.assertTrue(r.w.prs[pr]["merged"])
        # A PR whose head isn't the verified commit is rejected.
        pr2 = r.as_("worker").open_pr("T", "x\n\n<!-- ws:relay " + json.dumps({"task": t["id"]}) + " -->",
                                     "ws/relay-y", ["src/app.py"], sha="e" * 40)
        self.assertIn(settle_pr(q, None, pr2, events)["decision"], ("rejected",))


class SeedlessBootstrap(unittest.TestCase):
    """With no seed and no super-peer yet, agents with a public address found the mesh."""

    def test_routable_addresses(self):
        for ip in ("2001:4860:4860::8888", "8.8.8.8"):
            self.assertTrue(M._routable(ip), ip)
        for ip in ("10.0.0.5", "192.168.0.156", "100.64.1.1", "127.0.0.1", "fe80::1%en0", "fd00::1", "::1", "x"):
            self.assertFalse(M._routable(ip), ip)

    def test_first_agent_with_a_public_address_becomes_a_super_peer(self):
        from unittest import mock
        c = cfg()
        m = M.Mesh(c, key(1), S.pubkey(key(1)).hex(), REPO, None)
        with mock.patch.object(M, "global_address", return_value="[2001:db8::7]"):
            self.assertTrue(m._check_reachable([]))
        self.assertEqual(m.public_url, "ws://[2001:db8::7]:0")
        m.promote()
        try:
            self.assertEqual(m.public_url, f"ws://[2001:db8::7]:{m.server.port}")
            RelayConn(m.server.url, 3).close()  # the agent reaches its own relay
        finally:
            m.demote()
        m2 = M.Mesh(c, key(2), S.pubkey(key(2)).hex(), REPO, None)
        with mock.patch.object(M, "global_address", return_value=None):
            self.assertFalse(m2._check_reachable([]))  # behind NAT: stays a plain agent

    def test_wildcard_listener_accepts_ipv4_and_ipv6(self):
        import socket
        r = LocalRelay("0.0.0.0", 0)
        try:
            RelayConn(f"ws://127.0.0.1:{r.port}", 3).close()
            if r.srv.family == socket.AF_INET6:
                RelayConn(f"ws://[::1]:{r.port}", 3).close()
        finally:
            r.stop()


class Bridge(unittest.TestCase):
    """GitHub intake -> signed relay tasks -> settlement closes the issue -> dependents follow."""

    class Pool:
        def __init__(self):
            self.events = []

        def publish(self, ev):
            self.events.append(ev)

        def snapshot(self, repo_url):
            return list(self.events)

    def test_issue_to_relay_and_back(self):
        from workstead.bridge import publish_ready, task_ref
        from workstead.protocol import L
        from workstead.settle import settle_pr
        r = Repo({"relay": {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 0, "pov_min_prior_verifications": 0,
                            "trusted_agents": [S.pubkey(key(i)).hex() for i in range(2, 6)]}})
        self.assertEqual(r.cfg["driver"], "nostr")
        a = r.task("Parser")
        b = r.task("Docs for the parser", partition="docs", deps=[a])
        plan = r.task("Research options", partition="research")
        pool = self.Pool()
        q = r.fresh_queue()
        done = publish_ready(q, pool, PUB)
        self.assertEqual([d["issue"] for d in done], [a])  # b waits on a; research stays on GitHub
        self.assertIn(L["relay"], r.labels(a))
        self.assertNotIn(L["relay"], r.labels(plan))
        self.assertEqual(publish_ready(r.fresh_queue(), pool, PUB), [])  # idempotent
        tid = task_ref(q, a)
        self.assertEqual(tid, pool.events[0]["id"])
        spec = json.loads(pool.events[0]["content"])
        self.assertEqual((spec["issue"], spec["title"]), (a, "Parser"))
        self.assertNotIn("ws:meta", spec["body"])
        # Claiming the bridged task on GitHub is refused.
        r.say("worker", a, "/claim")
        self.assertNotIn(L["running"], r.labels(a))
        # The fleet verifies it over the relays; the gate merges and closes the issue.
        now = int(r.w.clock())
        out = "f" * 40
        for sk, role in ((key(2), "produce"), (key(3), "verify"), (key(4), "verify")):
            pool.publish(N.receipt_event(sk, tid, REPO, "b" * 40, out, "c" * 64, "d" * 64, "pass", role, now + 1,
                                         ref="https://x.example/r.git#b" if role == "produce" else None))
        pr = r.as_("worker").open_pr("Parser", "Done.\n\n<!-- ws:relay " + json.dumps({"task": tid}) + " -->",
                                    "ws/relay-a", ["src/parser.py"], sha=out)
        r.w.advance(5)
        res = settle_pr(r.fresh_queue(), pool, pr)
        self.assertEqual((res["decision"], res.get("issue")), ("merged", a), res)
        r.pump()
        self.assertEqual(r.issue(a)["state"], "closed")
        self.assertEqual(r.state(a)["status"], "done")
        done = publish_ready(r.fresh_queue(), pool, PUB)
        self.assertEqual([d["issue"] for d in done], [b])  # the dependent follows


if __name__ == "__main__":
    unittest.main()

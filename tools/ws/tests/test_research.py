"""Lessons from 2025-26 frontier agent fleets: contracts, findings, judge diversity, audits, backpressure."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import Repo, host_runner
from workstead import config as C, contract as K, findings as F, nostr as N, schnorr as S
from workstead.driver import RealGit
from workstead.settle import audit_sampled, settle_pr

PUB = bytes.fromhex("11" * 32)
REPO = "https://github.com/acme/project"


def key(i):
    return bytes([i]) * 32


class Contract(unittest.TestCase):
    def test_lean_escape_hatches_are_caught(self):
        c = K.contract(C.load(data={"repo": "a/b", "verify": {"profile": "lean", "frozen": ["Statements/"]}}), {})
        patch = ("+++ b/Proof.lean\n+theorem main : P := by\n+  sorry\n+axiom cheat : False\n"
                 "+-- a comment that mentions nothing\n")
        v = K.violations(["Proof.lean", "Statements/Main.lean"], {"Proof.lean": patch}, c)
        self.assertTrue(any("frozen path Statements/Main.lean" in x for x in v), v)
        self.assertTrue(any("sorry" in x for x in v))
        self.assertTrue(any("axiom" in x for x in v))
        # Removed lines don't count: deleting a sorry is the point.
        self.assertEqual(K.violations(["Proof.lean"], {"Proof.lean": "+++ b/Proof.lean\n-  sorry\n+  simp\n"}, c), [])

    def test_task_checks_add_to_project_defaults(self):
        cfg = C.load(data={"repo": "a/b", "verify": {"forbid": ["TODO"], "must_fail": ["make control"]}})
        c = K.contract(cfg, {"checks": {"frozen": ["bench/*.py"], "forbid": ["TODO", "HACK"]}})
        self.assertEqual(c, {"forbid": ["TODO", "HACK"], "frozen": ["bench/*.py"], "must_fail": ["make control"]})
        self.assertRaises(C.ConfigError, C.load, data={"repo": "a/b", "verify": {"profile": "coq?"}})

    def test_verifier_fails_a_result_whose_control_passes(self):
        """A 'proof' that also proves a known-false control proves too much."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
            run("init", "-q", "-b", "main")
            (root / "claim.txt").write_text("all zeros on the line\n")
            run("add", "-A")
            run("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
            (root / "proof.txt").write_text("works for everything\n")
            run("add", "-A")
            run("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "proof")
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
            cfg = C.load(data={"repo": "a/b", "verify": {"must_fail": ["test -f proof.txt"]}})
            g = RealGit(root, cfg, "", "true")
            g.runner = host_runner
            problems = g.contract_problems(root, base, out, {"spec": {}})
            self.assertTrue(any("proves too much" in p for p in problems), problems)
            cfg2 = C.load(data={"repo": "a/b", "verify": {"must_fail": ["test -f no-such-file"]}})
            g2 = RealGit(root, cfg2, "", "true")
            g2.runner = host_runner
            self.assertEqual(g2.contract_problems(root, base, out, {"spec": {}}), [])

    def test_gate_rejects_a_pr_that_edits_the_statement(self):
        r = Repo({"verify": {"frozen": ["Statements/"]},
                  "relay": {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 0, "pov_min_prior_verifications": 0,
                            "trusted_agents": [S.pubkey(key(i)).hex() for i in range(2, 6)]}})
        now = int(r.w.clock())
        t = N.task_event(PUB, REPO, "a" * 40, {"title": "Prove it"}, 5, 2, now - 500)
        out = "f" * 40
        events = [t] + [N.receipt_event(sk, t["id"], REPO, "a" * 40, out, "c" * 64, "d" * 64, "pass", role, now - 100,
                                        ref="https://x.example/r.git#b" if role == "produce" else None)
                        for sk, role in ((key(2), "produce"), (key(3), "verify"), (key(4), "verify"))]
        pr = r.as_("worker").open_pr("Prove it", "x\n\n<!-- ws:relay " + json.dumps({"task": t["id"]}) + " -->",
                                    "ws/relay-x", ["Proof.lean", "Statements/Main.lean"], sha=out)
        res = settle_pr(r.fresh_queue(), None, pr, events)
        self.assertEqual(res["decision"], "rejected", res)
        self.assertIn("frozen", res["reason"])
        self.assertFalse(r.w.prs[pr]["merged"])


class Findings(unittest.TestCase):
    def test_ledger_collapses_and_ranks(self):
        tid = "ab" * 32
        evs = [F.finding_event(key(2), REPO, "Horocycle equidistribution", "dead-end", "step 3 assumes RH", 1000, tid),
               F.finding_event(key(3), REPO, "horocycle   equidistribution!", "dead-end", "", 1001, tid),
               F.finding_event(key(4), REPO, "Pontryagin index bound, reversed", "partial", "gives 1/2 only", 1002, tid),
               F.finding_event(key(4), REPO, "Pontryagin index bound, reversed", "partial", "dup by same key", 1003, tid),
               F.finding_event(key(5), REPO, "Something for another task", "works", "", 1004, "cd" * 32)]
        cfg = C.load(data={"repo": "acme/project", "relay": {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 0}})
        self.assertTrue(all(N.well_formed(e, REPO, cfg) for e in evs))
        rows = F.ledger(evs, tid)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["verdict"], rows[0]["keys"]), ("dead-end", 2))
        self.assertEqual(rows[1]["keys"], 1)  # one key, one vote
        view = F.render(rows)
        self.assertIn("first failing step: step 3 assumes RH", view)
        self.assertLess(len(view), 400)

    def test_tampered_fingerprint_is_rejected(self):
        ev = F.finding_event(key(2), REPO, "real idea", "dead-end", "", 1000)
        forged = dict(ev, tags=[t if t[0] != "fp" else ["fp", F.fingerprint("another idea")] for t in ev["tags"]])
        self.assertFalse(F.well_formed(forged))
        self.assertRaises(ValueError, F.finding_event, key(2), REPO, "x", "solved!!", "", 1000)


class JudgeDiversity(unittest.TestCase):
    def test_quorum_must_span_model_families(self):
        c = C.load(data={"repo": "acme/project", "relay": {
            "publishers": [S.pubkey(PUB).hex()], "pow_bits": 0, "pov_min_prior_verifications": 0, "distinct_models": 2,
            "trusted_agents": [S.pubkey(key(i)).hex() for i in range(2, 8)]}})
        t = N.task_event(PUB, REPO, "a" * 40, {"title": "T"}, 3, 2, 1000)

        def rc(i, role, model):
            return N.receipt_event(key(i), t["id"], REPO, "a" * 40, "f" * 40, "c" * 64, "d" * 64, "pass", role, 1100 + i,
                                   ref="https://x.example/r.git#b" if role == "produce" else None, model=model)
        evs = [t, N.claim_event(key(2), t["id"], REPO, 1001, 3600), rc(2, "produce", "claude"),
               rc(3, "verify", "claude"), rc(4, "verify", "claude"), rc(5, "verify", "claude")]
        self.assertEqual(N.compute(evs, c, REPO, 2000)[t["id"]]["status"], "submitted")  # three of one family
        evs.append(rc(6, "verify", "gpt"))
        row = N.compute(evs, c, REPO, 2000)[t["id"]]
        self.assertEqual((row["status"], row["models"]), ("verified", ["claude", "gpt"]))


class AuditAndBackpressure(unittest.TestCase):
    def test_audit_sample_is_deterministic_and_near_the_rate(self):
        ids = [S.pubkey(key(i)).hex() for i in range(1, 250)]
        picked = sum(audit_sampled(i, 0.1) for i in ids)
        self.assertTrue(10 <= picked <= 45, picked)
        self.assertEqual([audit_sampled(i, 0.1) for i in ids[:20]], [audit_sampled(i, 0.1) for i in ids[:20]])

    def test_bridge_waits_while_results_pile_up(self):
        from workstead.bridge import publish_ready

        class Pool:
            def __init__(self):
                self.events = []

            def publish(self, ev):
                self.events.append(ev)

            def snapshot(self, repo_url):
                return list(self.events)
        r = Repo({"relay": {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 0, "max_unverified": 1,
                            "pov_min_prior_verifications": 0, "trusted_agents": [S.pubkey(key(2)).hex()]}})
        r.task("One")
        r.task("Two")
        pool = Pool()
        first = publish_ready(r.fresh_queue(), pool, PUB)
        self.assertEqual(len(first), 1)  # room for one while nothing is waiting
        t = pool.events[0]
        now = int(r.w.clock())
        pool.publish(N.claim_event(key(2), t["id"], "https://github.com/acme/project", now, 3600))
        pool.publish(N.receipt_event(key(2), t["id"], "https://github.com/acme/project", "b" * 40, "f" * 40, "c" * 64,
                                     "d" * 64, "pass", "produce", now + 1, ref="https://x.example/r.git#b"))
        r.w.advance(10)
        self.assertEqual(publish_ready(r.fresh_queue(), pool, PUB), [])  # one result awaits verification


if __name__ == "__main__":
    unittest.main()

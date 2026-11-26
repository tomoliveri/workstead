"""Guest mode: contributing without a GitHub account, through signed patches and the gateway."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import Repo
from workstead import config as C, guest as G, nostr as N, schnorr as S
from workstead.driver import RealGit
from workstead.relay import matches

PUB = bytes.fromhex("11" * 32)
GUEST = bytes([9]) * 32
GPK = S.pubkey(GUEST).hex()
REPO_URL = "https://github.com/acme/project"


def key(i):
    return bytes([i]) * 32


def git(cwd, *args, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
             GIT_COMMITTER_EMAIL="t@e", **(env or {}))
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=e).stdout.decode().strip()


def origin_with_base():
    """A bare 'origin' with one commit on main, and the base sha."""
    d = Path(tempfile.mkdtemp())
    src, bare = d / "src", d / "origin.git"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "model.py").write_text("def f(x):\n    return x\n")
    (src / "data.bin").write_bytes(bytes(range(256)))
    (src / "old.txt").write_text("remove me\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "base")
    git(d, "clone", "-q", "--bare", str(src), str(bare))
    return bare, git(src, "rev-parse", "HEAD")


def clone(bare):
    d = Path(tempfile.mkdtemp()) / "c"
    git(d.parent, "clone", "-q", str(bare), str(d))
    return d


class Pool:
    def __init__(self):
        self.events = []

    @property
    def milestone(self):
        return self

    def publish(self, ev):
        self.events.append(ev)

    def query(self, filters):
        return [e for e in self.events if any(matches(e, f) for f in filters)]


class CanonicalCommit(unittest.TestCase):
    def test_any_machine_rebuilds_the_same_commit_from_the_patch(self):
        bare, base = origin_with_base()
        a = clone(bare)
        (a / "model.py").write_text("def f(x):\n    return 2 * x\n")
        (a / "data.bin").write_bytes(bytes(range(255, -1, -1)) + b"\x00\r\n")
        (a / "new dir").mkdir()
        (a / "new dir" / "run.sh").write_text("#!/bin/sh\necho hi\r\n")
        (a / "old.txt").unlink()
        git(a, "add", "-A")
        git(a, "update-index", "--chmod=+x", "new dir/run.sh")  # portable (Windows has no exec bit)
        tree = git(a, "write-tree")
        out = G.canonical_commit(a, tree, base, "Double it", "ab" * 32, GPK, 1_700_000_000)
        patch = G.diff(a, base, out)
        # A verifier with hostile git settings rebuilds the identical commit.
        b = clone(bare)
        cfgfile = Path(tempfile.mkdtemp()) / "gitconfig"
        cfgfile.write_text("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n\trenames = copies\n"
                           "[core]\n\tautocrlf = true\n[commit]\n\tgpgsign = true\n")
        old = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfgfile)
        try:
            self.assertEqual(G.rebuild(b, base, patch, "Double it", "ab" * 32, GPK, 1_700_000_000), out)
            self.assertEqual(G.diff(b, base, out), patch)
        finally:
            if old is None:
                del os.environ["GIT_CONFIG_GLOBAL"]
            else:
                os.environ["GIT_CONFIG_GLOBAL"] = old
        # Any change to the message inputs gives a different commit: nobody can re-attribute it.
        self.assertNotEqual(G.rebuild(b, base, patch, "Double it", "ab" * 32, S.pubkey(key(3)).hex(),
                                      1_700_000_000), out)
        self.assertIn(("new dir/run.sh", "100755"), G.changed_files(patch))


class PatchEvents(unittest.TestCase):
    def test_chunks_reassemble_and_tampering_fails(self):
        data = os.urandom(40_000)
        tid = "cd" * 32
        evs = G.patch_events(GUEST, REPO_URL, tid, data, 1000)
        self.assertGreater(len(evs), 3)
        cfg = C.load(data={"repo": "acme/project", "relay": {"pow_bits": 0}})
        self.assertTrue(all(N.well_formed(e, REPO_URL, cfg) for e in evs))
        h = hashlib.sha256(data).hexdigest()
        self.assertEqual(G.assemble(evs, h, GPK, 1 << 20), data)
        self.assertIsNone(G.assemble(evs[:-1], h, GPK, 1 << 20))  # a part missing
        self.assertIsNone(G.assemble(evs, h, S.pubkey(key(3)).hex(), 1 << 20))  # someone else's key
        forged = dict(evs[0], content=evs[0]["content"][::-1])
        self.assertEqual(G.assemble([forged] + evs[1:], h, GPK, 1 << 20), None)  # bad signature dropped
        self.assertIsNone(G.assemble(evs, h, GPK, 10_000))  # over the size limit
        # A stranger's extra parts for the same hash are ignored.
        noise = G.patch_events(key(3), REPO_URL, tid, b"junk", 1001)
        self.assertEqual(G.assemble(noise + evs, h, GPK, 1 << 20), data)


class WhatGuestsMayChange(unittest.TestCase):
    def test_protected_dependency_and_odd_files_are_refused(self):
        cfg = C.load(data={"repo": "acme/project"})

        def patch(path, mode="100644"):
            return (f"diff --git a/{path} b/{path}\nnew file mode {mode}\nindex 0000000..1111111\n--- /dev/null\n"
                    f"+++ b/{path}\n@@ -0,0 +1 @@\n+x\n").encode()
        self.assertEqual(G.problems(cfg, patch("research/model.py")), [])
        for path, why in ((".github/workflows/ci.yml", "protected"), ("tools/ws/workstead/guest.py", "protected"),
                          ("workstead.json", "protected"), ("package.json", "dependency"),
                          ("requirements.txt", "dependency"), ("tools/lib/x.py", "protected")):
            self.assertTrue(any(why in p for p in G.problems(cfg, patch(path))), path)
        self.assertTrue(any("symbolic link" in p for p in G.problems(cfg, patch("link", "120000"))))
        self.assertTrue(any("submodule" in p for p in G.problems(cfg, patch("sub", "160000"))))
        self.assertTrue(any("larger" in p for p in G.problems(cfg, patch("big.txt") + b"+" * 300_000)))


class GuestProducesAndOthersVerify(unittest.TestCase):
    def test_round_trip_through_real_git(self):
        bare, base = origin_with_base()
        cfg = C.load(data={"repo": "acme/project", "relay": {"pow_bits": 0}})
        pool = Pool()
        task = {"id": "ef" * 32, "base": base, "created_at": 1_700_000_123, "spec": {"title": "Double it"}}
        g = RealGit(clone(bare), cfg, "", "true")
        g.guest_pk = GPK
        g.publish_patch = lambda tid, data: [pool.publish(e) for e in G.patch_events(GUEST, REPO_URL, tid, data, 5)]
        wt = g.worktree(task)
        (wt / "model.py").write_text("def f(x):\n    return 2 * x\n")
        b, out, patch, tests, verdict, ref = g.produce(task)
        self.assertEqual((b, verdict), (base, "pass"))
        self.assertEqual(ref, "nostr:" + patch)
        self.assertTrue(N.REF_RE.match(ref))
        v = RealGit(clone(bare), cfg, "", "true")
        v.fetch_patch = lambda h, producer: G.fetch_patch(pool, h, producer, 1 << 20)
        vpatch, _, vverdict = v.verify(ref, base, out, dict(task, producer=GPK))
        self.assertEqual((vpatch, vverdict), (patch, "pass"))
        # A receipt claiming another commit for the same patch is never vouched for.
        v2 = RealGit(clone(bare), cfg, "", "true")
        v2.fetch_patch = v.fetch_patch
        self.assertEqual(v2.verify(ref, base, "1" * 40, dict(task, producer=GPK))[2], "fail")

    def test_guest_cannot_deliver_a_workflow_change(self):
        bare, _ = origin_with_base()
        cfg = C.load(data={"repo": "acme/project", "relay": {"pow_bits": 0}})
        g = RealGit(clone(bare), cfg, "", "true")
        g.guest_pk = GPK
        g.publish_patch = lambda tid, data: None
        task = {"id": "ef" * 32, "base": git(g.root, "rev-parse", "HEAD"), "created_at": 1, "spec": {}}
        wt = g.worktree(task)
        (wt / ".github" / "workflows").mkdir(parents=True)
        (wt / ".github" / "workflows" / "x.yml").write_text("on: push\n")
        with self.assertRaises(RuntimeError) as e:
            g.produce(task)
        self.assertIn("protected path", str(e.exception))


class Gateway(unittest.TestCase):
    def setUp(self):
        self.bare, self.base = origin_with_base()
        self.r = Repo({"relay": {"publishers": [S.pubkey(PUB).hex()], "pow_bits": 0, "pov_min_prior_verifications": 0,
                                 "trusted_agents": [S.pubkey(key(i)).hex() for i in range(2, 6)]}})
        self.pushed = []

    def verified(self, path, content):
        """Publish a task, a guest's patch and receipts from two trusted verifiers; return the events."""
        now = int(self.r.w.clock())
        t = N.task_event(PUB, REPO_URL, self.base, {"title": "Double it"}, 5, 2, now - 500)
        c = clone(self.bare)
        (c / path).parent.mkdir(parents=True, exist_ok=True)
        (c / path).write_text(content)
        git(c, "add", "-A")
        out = G.canonical_commit(c, git(c, "write-tree"), self.base, "Double it", t["id"], GPK, t["created_at"])
        patch = G.diff(c, self.base, out)
        h = hashlib.sha256(patch).hexdigest()
        pool = Pool()
        for e in G.patch_events(GUEST, REPO_URL, t["id"], patch, now - 300):
            pool.publish(e)
        events = [t, N.claim_event(GUEST, t["id"], REPO_URL, t["created_at"] + 1, 3600),
                  N.receipt_event(GUEST, t["id"], REPO_URL, self.base, out, h, "c" * 64, "pass", "produce",
                                  now - 200, ref="nostr:" + h)]
        events += [N.receipt_event(key(i), t["id"], REPO_URL, self.base, out, h, "c" * 64, "pass", "verify", now - 100)
                   for i in (3, 4)]
        return t, out, pool, events

    def run_gateway(self, pool, events):
        return G.open_guest_prs(self.r.fresh_queue(), pool, events, PUB,
                                push=lambda work, sha, branch: self.pushed.append((sha, branch)),
                                remote=str(self.bare))

    def test_opens_one_pull_request_for_a_verified_guest_result(self):
        t, out, pool, events = self.verified("model.py", "def f(x):\n    return 2 * x\n")
        self.assertEqual(N.compute(events, self.r.cfg, REPO_URL, int(self.r.w.clock()))[t["id"]]["status"], "verified")
        res = self.run_gateway(pool, events)
        self.assertEqual(res[0]["decision"], "opened", res)
        self.assertEqual(self.pushed, [(out, "ws/guest-" + t["id"][:12])])
        body = self.r.w.issues[res[0]["pr"]]["body"]
        self.assertIn("without a GitHub account", body)
        self.assertIn(S.npub(S.pubkey(GUEST)), body)
        self.assertNotIn("Closes #", body)
        self.assertEqual(self.run_gateway(pool, events), [])  # never twice

    def test_refuses_protected_changes_and_tells_the_guest(self):
        t, _, pool, events = self.verified(".github/workflows/steal.yml", "on: push\n")
        res = self.run_gateway(pool, events)
        self.assertEqual(res[0]["decision"], "refused")
        self.assertIn("protected path", res[0]["reason"])
        self.assertEqual(self.pushed, [])
        told = [e for e in pool.events if e["kind"] == N.K_FINDING and N.tag(e, "e") == t["id"]]
        self.assertEqual(len(told), 1)

    def test_refuses_when_the_patch_isnt_the_verified_commit(self):
        t, out, pool, events = self.verified("model.py", "def f(x):\n    return 2 * x\n")
        # Swap the patch for another one under the same hash claim: assembly fails, nothing is pushed.
        pool.events = [e for e in pool.events if e["kind"] != N.K_PATCH]
        res = self.run_gateway(pool, events)
        self.assertEqual(res[0]["decision"], "waiting")
        self.assertEqual(self.pushed, [])


    def test_refuses_a_patch_that_rebuilds_another_commit(self):
        t, out, pool, events = self.verified("model.py", "def f(x):\n    return 2 * x\n")
        h = N.tag(events[2], "ref")[len("nostr:"):]
        fake = "1" * 40  # what the receipts claim: not what the patch builds
        events = events[:2] + [N.receipt_event(GUEST, t["id"], REPO_URL, self.base, fake, h, "c" * 64, "pass",
                                               "produce", events[2]["created_at"], ref="nostr:" + h)]
        events += [N.receipt_event(key(i), t["id"], REPO_URL, self.base, fake, h, "c" * 64, "pass", "verify",
                                   events[2]["created_at"] + 50) for i in (3, 4)]
        res = self.run_gateway(pool, events)
        self.assertEqual(res[0]["decision"], "refused", res)
        self.assertIn("isn't the verified", res[0]["reason"])
        self.assertEqual(self.pushed, [])


class GuestCommands(unittest.TestCase):
    def test_guest_gating(self):
        from workstead.cli import _guest_blocked
        ctx = type("Ctx", (), {"cfg": C.load(data={"repo": "a/b", "relay": {"publishers": ["11" * 32]}})})()
        args = lambda cmd, ref="": type("A", (), {"cmd": cmd, "ref": ref})()  # noqa: E731
        self.assertEqual(_guest_blocked(ctx, args("next")), "")
        self.assertIn("needs a GitHub account", _guest_blocked(ctx, args("found")))
        self.assertIn("issue-queue", _guest_blocked(ctx, args("claim", "a/b#4")))
        issues = type("Ctx", (), {"cfg": C.load(data={"repo": "a/b", "driver": "github-issues"})})()
        self.assertIn("relay mode", _guest_blocked(issues, args("next")))

    def test_guest_detection(self):
        old = {k: os.environ.get(k) for k in ("WS_GUEST",)}
        try:
            os.environ["WS_GUEST"] = "1"
            self.assertTrue(G.is_guest())
            os.environ["WS_GUEST"] = "0"
            self.assertFalse(G.is_guest())
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertTrue(G.login_for(GPK).startswith("guest-"))
        self.assertEqual(json.loads(json.dumps(G.commit_env(GPK, 5)))["GIT_AUTHOR_DATE"], "@5 +0000")


if __name__ == "__main__":
    unittest.main()

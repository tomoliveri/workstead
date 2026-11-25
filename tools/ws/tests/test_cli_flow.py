"""End to end: the real `ws` commands, real git, a fake GitHub, the real queue."""
import argparse
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness import L, Repo
from workstead import worker as W


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def ns(**kw):
    return argparse.Namespace(**kw)


class CliFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ws-cli-"))
        W.HOME = self.tmp / "home"
        origin = self.tmp / "origin.git"
        seed = self.tmp / "seed"
        git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.tmp)
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "workstead.json").write_text(json.dumps({"repo": "acme/project"}))
        (seed / "app.py").write_text("print('hi')\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init", cwd=seed)
        git("remote", "add", "origin", str(origin), cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        self.clone = self.tmp / "clone"
        git("clone", "-q", str(origin), str(self.clone), cwd=self.tmp)
        git("config", "user.name", "alice", cwd=self.clone)
        git("config", "user.email", "alice@example.com", cwd=self.clone)

        self.r = Repo()
        self.r.w.perms["alice"] = "write"
        self.r.w.perms["rev"] = "write"
        self.orig_wait = W.wait_for
        W.wait_for = lambda fn, timeout=0, step=0: (self.r.pump(), fn())[1]

    def tearDown(self):
        W.wait_for = self.orig_wait

    def ctx(self, login="alice"):
        c = W.Ctx(self.clone, repo="acme/project", cfg=self.r.cfg, gh=self.r.as_(login))
        return c

    def run_cmd(self, fn, ctx, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fn(ctx, ns(**kw))
        out = buf.getvalue().strip()
        return code, out

    def test_task_from_next_to_merge(self):
        t = self.r.task("Add a greeting function")
        self.r.sweep()
        c = self.ctx()
        code, out = self.run_cmd(W.cmd_next, c, max_hours=None)
        self.assertEqual(code, W.EXIT_NOT_JOINED)
        self.run_cmd(W.cmd_join, c, confirmed=True, node="laptop-1", features=None, partitions=None,
                     max_hours=None, no_give_back=False)
        code, out = self.run_cmd(W.cmd_next, c, max_hours=None)
        self.assertEqual(code, W.EXIT_WORK, out)
        item = json.loads(out)
        self.assertEqual(item["number"], t)
        self.assertEqual(item["next_command"], f"ws claim acme/project#{t}")

        code, out = self.run_cmd(W.cmd_claim, c, ref=str(t), node=None, timeout=5)
        self.assertEqual(code, W.EXIT_WORK, out)
        wt = Path(json.loads(out)["worktree"])
        self.assertTrue((wt / "app.py").exists())
        self.assertEqual(self.r.state(t)["lease"]["node"], "laptop-1")

        code, packet = self.run_cmd(W.cmd_show, c, ref=str(t))
        self.assertIn("Deliver: a pull request", packet)
        self.assertIn("Add a greeting function", packet)

        (wt / "greet.py").write_text("def greet():\n    return 'hello'\n")
        code, out = self.run_cmd(W.cmd_checkpoint, c, ref=str(t), message="wip")
        self.assertIn("checkpointed", out)
        claim = self.r.state(t)["lease"]["comment"]
        self.assertIn("ref=acme/project:ws/", self.r.w.comments[claim]["body"])

        summary = self.tmp / "SUMMARY.md"
        summary.write_text("Added greet(). Ran it by hand.")
        code, out = self.run_cmd(W.cmd_submit, c, ref=str(t), file=None, summary_file=str(summary), plan=False,
                                 no_validate=False)
        self.assertEqual(code, 0, out)
        pr = json.loads(out)["pr"]
        self.r.pump()
        self.assertEqual(self.r.state(t)["status"], "review")
        self.assertIn(L["needs_review"], self.r.labels(pr))
        self.assertTrue(W.lease_path("acme/project", t).exists())  # kept for rework heartbeats

        rc = self.ctx("rev")
        code, out = self.run_cmd(W.cmd_claim, rc, ref=str(pr), node="rev-1", timeout=5)
        self.assertEqual(json.loads(out)["status"], "leased", out)
        code, packet = self.run_cmd(W.cmd_show, rc, ref=str(pr))
        self.assertIn("How to review", packet)
        review = self.tmp / "REVIEW.md"
        review.write_text("Meets the criteria.")
        self.run_cmd(lambda cx, a: W.cmd_simple(cx, a, "approve"), rc, ref=str(pr), body_file=str(review))
        self.r.pump()
        self.assertTrue(self.r.w.prs[pr]["merged"])
        self.assertEqual(self.r.state(t)["status"], "done")
        code, out = self.run_cmd(W.cmd_beat, c, ref=None)
        self.assertIn("closed; record removed", out)
        led = W.ledger("acme/project")
        self.assertGreaterEqual(led["work"], 0)

    def test_research_result_and_found(self):
        t = self.r.task("Survey logging libraries", partition="research")
        self.r.sweep()
        c = self.ctx()
        self.run_cmd(W.cmd_join, c, confirmed=True, node="n", features=None, partitions=None, max_hours=None,
                     no_give_back=True)
        code, out = self.run_cmd(W.cmd_claim, c, ref=str(t), node=None, timeout=5)
        self.assertIsNone(json.loads(out)["worktree"])
        res = self.tmp / "RESULT.md"
        res.write_text("Use the standard library logger; here is why.")
        self.run_cmd(W.cmd_submit, c, ref=str(t), file=str(res), summary_file=None, plan=False, no_validate=False)
        self.r.pump()
        self.assertEqual(self.r.state(t)["status"], "done")
        found = self.tmp / "FOUND.md"
        found.write_text("The config loader silently ignores unknown keys, which hides typos in production configs.")
        code, out = self.run_cmd(W.cmd_found, c, from_ref=str(t), title="Config loader ignores unknown keys",
                                 body_file=str(found), partition="code", priority=4, est_hours=1.0)
        n = int(json.loads(out)["ref"].split("#")[1])
        self.r.pump()
        # alice has write access, so her proposal is admitted at once.
        self.assertIn(L["ready"], self.r.labels(n))
        self.assertEqual(self.r.meta(n)["found_in"], t)

    def test_plan_submission(self):
        job = self.r.job()
        self.r.sweep()
        c = self.ctx()
        self.run_cmd(W.cmd_join, c, confirmed=True, node="n", features=None, partitions=None, max_hours=None,
                     no_give_back=False)
        code, out = self.run_cmd(W.cmd_claim, c, ref=str(job), node=None, timeout=5)
        self.assertEqual(json.loads(out)["status"], "leased", out)
        plan = Path(__file__).resolve().parents[3] / "docs" / "examples" / "plan-feature.json"
        code, _ = self.run_cmd(W.cmd_submit, c, ref=str(job), file=str(plan), summary_file=None, plan=False,
                                 no_validate=False)
        self.assertEqual(code, 0)
        self.r.pump()
        self.assertEqual(len(self.r.children(job)), 7)
        self.assertEqual(self.r.state(job)["status"], "split")

    def test_declined_claim_reports_the_reason(self):
        t = self.r.task("Mine to do")
        self.r.say("alice", t, "/claim")
        pr = self.r.open_pr("alice", t)
        c = self.ctx()
        self.run_cmd(W.cmd_join, c, confirmed=True, node="n", features=None, partitions=None, max_hours=None,
                     no_give_back=False)
        code, out = self.run_cmd(W.cmd_claim, c, ref=str(pr), node=None, timeout=5)
        self.assertEqual(code, W.EXIT_LOST)
        self.assertEqual(json.loads(out)["status"], "declined")
        self.assertIn("own", json.loads(out)["message"])

    def test_lost_claim_is_reported_and_avoided(self):
        t = self.r.task("Contended task")
        self.r.say("bob", t, "/claim")
        c = self.ctx()
        self.run_cmd(W.cmd_join, c, confirmed=True, node="n", features=None, partitions=None, max_hours=None,
                     no_give_back=False)
        self.r.bot.add_labels(t, [L["ready"]])  # a stale label, as from an old board
        code, out = self.run_cmd(W.cmd_claim, c, ref=str(t), node=None, timeout=5)
        self.assertEqual(code, W.EXIT_LOST, out)
        self.assertIn(t, W.avoided("acme/project"))


if __name__ == "__main__":
    unittest.main()

import json
import random
import unittest
from pathlib import Path

import harness  # noqa: F401  (sets WS_HOME, sys.path)
from workstead import config as C
from workstead import dedup, plan as P, sched
from workstead import worker as W
from workstead.fakegh import FakeGitHub, World
from workstead.protocol import (beat_body, beat_ref, clean_meta, json_block, linked_task, parse_command,
                                read_block, replace_block, write_block)

CFG = C.load(data={"repo": "acme/project"})


class Protocol(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(parse_command("/claim node=mac-1"), ("claim", {"node": "mac-1"}, ""))
        self.assertEqual(parse_command("/claim"), ("claim", {}, ""))
        self.assertEqual(parse_command("  /release no_time\nbye"), ("release", {"reason": "no_time"}, "bye"))
        self.assertEqual(parse_command("/done\nresult\nmore")[2], "result\nmore")
        self.assertIsNone(parse_command("please /claim"))
        self.assertIsNone(parse_command("/claim node=$(rm -rf)"))
        self.assertIsNone(parse_command("/claim; echo hi"))
        self.assertIsNone(parse_command("/unknown"))
        self.assertEqual(parse_command("/duplicate #12")[1], {"of": "12"})
        self.assertEqual(parse_command("/admit priority=7")[1], {"priority": "7"})
        self.assertIsNone(parse_command("/priority 12"))

    def test_blocks_round_trip_and_resist_injection(self):
        body = "Hello\n\n" + write_block("meta", {"a": 1, "s": "x --> <!-- ws:meta {} -->"})
        self.assertEqual(read_block(body, "meta")["a"], 1)
        self.assertNotIn("-->", write_block("meta", {"s": "-->"})[:-3])
        new = replace_block(body, "meta", {"a": 2})
        self.assertEqual(read_block(new, "meta"), {"a": 2})
        self.assertIsNone(read_block("<!-- ws:meta {broken -->", "meta"))

    def test_clean_meta_drops_garbage(self):
        m = clean_meta({"partition": "nope", "priority": 99, "deps": [3, "x", -1, True, 3], "features": ["GPU!", "gpu"],
                        "est_hours": "lots", "parent": "1"}, CFG["partitions"])
        self.assertIsNone(m["partition"])
        self.assertEqual(m["priority"], 9)
        self.assertEqual(m["deps"], [3])
        self.assertEqual(m["features"], ["gpu"])
        self.assertEqual(m["est_hours"], 1.0)
        self.assertIsNone(m["parent"])

    def test_beats_and_links(self):
        b = beat_body("n1", "2026-01-01T00:00:00Z", "alice/proj:ws/3-x@abcdef1")
        self.assertEqual(parse_command(b)[0], "claim")
        self.assertEqual(beat_ref(b), "alice/proj:ws/3-x@abcdef1")
        self.assertIsNone(beat_ref(beat_body("n1", "t", "bad ref; rm")))
        self.assertEqual(linked_task("Fixes #42 and more"), 42)
        self.assertEqual(linked_task(write_block("pr", {"task": 7}) + " closes #9"), 7)
        self.assertIsNone(linked_task("see #3"))
        self.assertEqual(json_block("x\n```json\n{\"a\": 1}\n```\n"), {"a": 1})


class Plans(unittest.TestCase):
    def test_arrays_fan_out_and_gather(self):
        tasks, errors = P.validate({"tasks": [
            {"key": "map", "title": "Translate {item}", "array": ["fr", "de", "ja"], "partition": "docs"},
            {"key": "reduce", "title": "Merge translations", "deps": ["map"]},
        ]}, CFG, 0)
        self.assertEqual(errors, [])
        self.assertEqual(len(tasks), 4)
        self.assertEqual(tasks[-1]["key"], "reduce")
        self.assertEqual(len(tasks[-1]["deps"]), 3)
        self.assertEqual(tasks[0]["title"], "Translate fr")

    def test_errors(self):
        cases = [
            ({"tasks": []}, "no tasks"),
            ({"tasks": [{"key": "A", "title": "Bad key"}]}, "key"),
            ({"tasks": [{"key": "a", "title": "x"}]}, "title"),
            ({"tasks": [{"key": "a", "title": "Task", "partition": "review"}]}, "partition"),
            ({"tasks": [{"key": "a", "title": "Task", "deps": ["zz"]}]}, "unknown dependency"),
            ({"tasks": [{"key": "a", "title": "Task", "deps": ["b"]}, {"key": "b", "title": "Task", "deps": ["a"]}]}, "cycle"),
            ({"tasks": [{"key": f"k{i}", "title": "Task"} for i in range(61)]}, "limit"),
            ({"tasks": [{"key": "a", "title": "Task", "est_hours": 0}]}, "est_hours"),
            ("nope", "object"),
        ]
        for doc, want in cases:
            _, errors = P.validate(doc, CFG, 0)
            self.assertTrue(any(want in e for e in errors), (doc, errors))
        _, errors = P.validate({"tasks": [{"key": "a", "title": "Task"}]}, CFG, 4)
        self.assertTrue(errors)

    def test_external_deps(self):
        tasks, errors = P.validate({"tasks": [{"key": "a", "title": "Task", "deps": ["#12", 13]}]}, CFG, 0,
                                   existing_ok=lambda n: n == 12)
        self.assertTrue(any("#13" in e for e in errors))

    def test_dependents_count(self):
        g = {1: [], 2: [1], 3: [1], 4: [2, 3], 5: []}
        d = P.dependents_count(g)
        self.assertEqual(d[1], 3)
        self.assertEqual(d[4], 0)
        self.assertEqual(d[5], 0)


class Scheduler(unittest.TestCase):
    def item(self, n, prio=3, deps=(), **kw):
        meta = clean_meta({"priority": prio, "deps": list(deps), "partition": kw.pop("partition", "code"),
                           "features": kw.pop("features", []), "licenses": kw.pop("licenses", []),
                           "est_hours": kw.pop("est", 1)}, CFG["partitions"])
        return dict({"number": n, "meta": meta, "kind": "task", "status": "queued", "created_at": "2026-01-01T00:00:00Z"}, **kw)

    def test_deps_state(self):
        st = {1: "done", 2: "open", 3: "failed"}.get
        self.assertEqual(sched.deps_state({"deps": [1]}, st)[0], "ready")
        self.assertEqual(sched.deps_state({"deps": [1, 2]}, st), ("waiting", [2]))
        self.assertEqual(sched.deps_state({"deps": [3]}, st), ("never", [3]))
        self.assertEqual(sched.deps_state({"deps": [3], "after": "any"}, st)[0], "ready")

    def test_constraints_backfill_licenses_and_order(self):
        w = {"login": "me", "features": ["python"], "max_hours": 2, "trusted": False}
        items = [self.item(1, prio=9, features=["gpu"]), self.item(2, prio=8, est=5),
                 self.item(3, prio=7, licenses=["db"]), self.item(4, prio=1), self.item(5, prio=6)]
        running = [dict(self.item(9, licenses=["db"]), status="running")]
        ranked = sched.rank(items, items + running, w, CFG, 1.8e9)
        self.assertEqual([r[1]["number"] for r in ranked], [5, 4])
        self.assertTrue(items[0]["reason"].startswith("Constraint"))
        self.assertEqual(items[1]["reason"], "Backfill")
        self.assertTrue(items[2]["reason"].startswith("Licenses"))

    def test_critical_path_breaks_ties_and_reviews_need_trust(self):
        items = [self.item(1), self.item(2)]
        downstream = [self.item(10 + i, deps=[2], status="queued") for i in range(3)]
        ranked = sched.rank(items, items + downstream, {"login": "me", "trusted": False}, CFG, 1.8e9)
        self.assertEqual(ranked[0][1]["number"], 2)
        rv = [{"number": 7, "kind": "review", "author": "x", "meta": {"priority": 8}, "created_at": "2026-01-01T00:00:00Z"}]
        self.assertEqual(sched.rank(rv, [], {"login": "me", "trusted": False}, CFG, 1.8e9), [])
        self.assertEqual(len(sched.rank(rv, [], {"login": "me", "trusted": True}, CFG, 1.8e9)), 1)

    def test_choose_spreads_but_respects_priority(self):
        ranked = [(1000.0 + i, {"number": i, "kind": "task"}, {}) for i in range(10)]
        ranked += [(10.0, {"number": 99, "kind": "task"}, {})]
        ranked.sort(key=lambda x: -x[0])
        rng = random.Random(1)
        picks = {sched.choose(ranked, CFG, rng)["number"] for _ in range(300)}
        self.assertGreater(len(picks), 6)
        self.assertNotIn(99, picks)
        resume = [(5.0, {"number": 1, "kind": "resume"}, {})] + ranked
        self.assertEqual(sched.choose(resume, CFG, rng)["number"], 1)

    def test_time_limit(self):
        self.assertEqual(sched.time_limit_hours({"est_hours": 1, "partition": "code"}, CFG), 3)
        self.assertEqual(sched.time_limit_hours({"est_hours": 0.1, "partition": "code"}, CFG), 1)
        self.assertEqual(sched.time_limit_hours({"est_hours": 50, "partition": "code"}, CFG), 12)


class Dedup(unittest.TestCase):
    def test_similar_and_different(self):
        others = [{"number": 1, "title": "Add retry with backoff to HTTP client", "body": "Retries on 5xx with jitter."},
                  {"number": 2, "title": "Write user onboarding docs", "body": "A guide for new users."}]
        self.assertEqual(dedup.matches("Add retry and backoff to the HTTP client", "Retry 5xx responses with jitter.", others, 0.6)[0][0], 1)
        self.assertEqual(dedup.matches("Speed up image decoding", "Use SIMD.", others, 0.6), [])


class Config(unittest.TestCase):
    def test_rejects_typos_and_bad_values(self):
        with self.assertRaises(C.ConfigError):
            C.load(data={"leasez": {}})
        with self.assertRaises(C.ConfigError):
            C.load(data={"review": {"merge": "yolo"}})
        with self.assertRaises(C.ConfigError):
            C.load(data={"give_back": {"share": 0.5}})
        cfg = C.load(data={"partitions": {"design": {"deliver": "comment", "max_hours": 2}}})
        self.assertIn("design", cfg["partitions"])
        self.assertIn("code", cfg["partitions"])

    def test_template_config_is_valid(self):
        root = Path(__file__).resolve().parents[3]
        C.load(data=json.loads((root / "workstead.json").read_text()))


class Worker(unittest.TestCase):
    def setUp(self):
        self.w = World(repo="acme/project")
        self.w.template = "workstead/workstead"
        W.HOME = Path(harness.os.environ["WS_HOME"]) / f"t{random.random()}"

    def ctx(self, cfg=None, login="alice"):
        c = W.Ctx(Path("."), repo="acme/project", cfg=cfg or C.load(data={"repo": "acme/project"}),
                  gh=FakeGitHub(self.w, login))
        return c

    def test_consent_mentions_give_back_only_when_enabled(self):
        off = W.consent_text(self.ctx())
        self.assertNotIn("Give back", off)
        on = W.consent_text(self.ctx(C.load(data={"repo": "acme/project", "give_back": {"enabled": True}})))
        self.assertIn("Give back 0.1%", on)
        self.assertIn("workstead/workstead", on)
        self.assertIn("1,000 hours", on)
        self.assertIn("no give-back", on)

    def test_give_back_is_owed_after_enough_work(self):
        cfg = C.load(data={"repo": "acme/project", "give_back": {"enabled": True}})
        c = self.ctx(cfg)
        self.assertIsNone(W.give_back_target(c))
        W.ledger_add("acme/project", "work", 499 * 3600, "x")
        self.assertIsNone(W.give_back_target(c))  # 0.499 h owed < 0.5 h
        W.ledger_add("acme/project", "work", 2 * 3600, "x")
        self.assertEqual(W.give_back_target(c), "workstead/workstead")
        W.ledger_add("acme/project", "give", 0.6 * 3600, "y")
        self.assertIsNone(W.give_back_target(c))
        W.save_profile({"give_back": False})
        W.ledger_add("acme/project", "work", 5000 * 3600, "x")
        self.assertIsNone(W.give_back_target(c))  # the worker opted out

    def test_give_back_disabled_or_self_is_never_targeted(self):
        W.ledger_add("acme/project", "work", 10 ** 7, "x")
        self.assertIsNone(W.give_back_target(self.ctx()))
        self.w.template = "acme/project"
        cfg = C.load(data={"repo": "acme/project", "give_back": {"enabled": True}})
        self.assertIsNone(W.give_back_target(self.ctx(cfg)))

    def test_candidates_resume_is_node_aware(self):
        c = self.ctx()
        c.node = lambda: "n1"
        tasks = [{"number": 1, "status": "running", "lease": {"login": "alice", "node": "n2"}, "meta": {}},
                 {"number": 2, "status": "running", "lease": {"login": "alice", "node": "n1"}, "meta": {}},
                 {"number": 3, "status": "queued", "ready": True, "meta": {"priority": 3}}]
        kinds = {x["number"]: x["kind"] for x in W.candidates(c, tasks, [])}
        self.assertEqual(kinds, {2: "resume", 3: "task"})

    def test_parse_ref(self):
        self.assertEqual(W.parse_ref("12", "a/b"), ("a/b", 12))
        self.assertEqual(W.parse_ref("x/y#3", "a/b"), ("x/y", 3))
        with self.assertRaises(SystemExit):
            W.parse_ref("x/y#", "a/b")


class CodeOwners(unittest.TestCase):
    def test_rendered_per_repository_and_never_leaks_the_template_owner(self):
        import tempfile
        from workstead import admin
        root = Path(tempfile.mkdtemp())
        self.assertTrue(admin.render_codeowners(root, {"owner": "alice", "owner_type": "User"}, CFG))
        text = (root / ".github" / "CODEOWNERS").read_text()
        self.assertIn("/tools/ws/ @alice", text)
        self.assertFalse(admin.render_codeowners(root, {"owner": "alice", "owner_type": "User"}, CFG))
        # An organisation without listed maintainers: the rendered file goes.
        self.assertTrue(admin.render_codeowners(root, {"owner": "acme", "owner_type": "Organization"}, CFG))
        self.assertFalse((root / ".github" / "CODEOWNERS").exists())
        org = C.load(data={"repo": "acme/x", "maintainers": ["bob", "carol"]})
        admin.render_codeowners(root, {"owner": "acme", "owner_type": "Organization"}, org)
        self.assertIn("@bob @carol", (root / ".github" / "CODEOWNERS").read_text())
        (root / ".github" / "CODEOWNERS").write_text("* @someone\n")  # hand-written: left alone
        self.assertFalse(admin.render_codeowners(root, {"owner": "alice", "owner_type": "User"}, CFG))


class Simulation(unittest.TestCase):
    def test_small_fleet_finishes_without_duplicate_work(self):
        from workstead.sim import Sim
        for seed in (1, 2):
            res = Sim(agents=20, jobs=1, seed=seed, die_p=0.004).run(2000)
            self.assertTrue(res["finished"], res)
            self.assertEqual(res["violations"], [], res)


if __name__ == "__main__":
    unittest.main()

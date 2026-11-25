import json
import unittest

from harness import BOT, L, Repo
from workstead import config as C
from workstead import safety as SAFE
from workstead.engine import Queue

T0 = "2027-01-15T08:00:00Z"


def flag(subject="mallory", project="alice/shop", evidence=None, signal="malicious_code"):
    return {"v": 1, "subject": subject, "signal": signal, "project": project,
            "evidence": evidence or [f"https://github.com/{project}/pull/7"]}


class Upstream(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        w = self.r.w
        w.foreign = {
            "alice/shop": {"owner": "alice", "type": "User", "issues": {7: "mallory", 8: "alice"}},
            "acme/api": {"owner": "acme", "type": "Organization", "members": ["carol"], "issues": {3: "mallory"}},
            "bob/game": {"owner": "bob", "type": "User", "issues": {9: "mallory", 10: "innocent"}},
            "dan/secret": {"owner": "dan", "type": "User", "private": True, "issues": {1: "mallory"}},
        }
        self.n = self.r.bot.create_issue("Fleet safety", "x", [L["safety"]])["number"]

    def post(self, who, obj):
        return self.r.say(who, self.n, "/flag\n```json\n" + json.dumps(obj) + "\n```")

    def state(self):
        for c in self.r.comments(self.n):
            if c["author"] == BOT and "ws:state" in c["body"]:
                from workstead.protocol import read_block
                return read_block(c["body"], "state")

    def listed(self):
        return SAFE.listing(self.state(), self.r.w.clock())

    def test_verification_rules(self):
        cases = [
            ("eve", flag(), "only alice/shop's owner"),                                  # no authority
            ("alice", flag(evidence=["https://github.com/alice/shop/pull/8"]), "none of the evidence"),  # not authored
            ("mallory", flag(subject="alice", evidence=["https://github.com/alice/shop/pull/8"]), "only"),  # retaliation
            ("dan", flag(project="dan/secret", evidence=["https://github.com/dan/secret/issues/1"]), "public"),
            ("alice", dict(flag(), note="he is evil"), "unknown fields"),               # free text smuggling
            ("alice", flag(evidence=["https://evil.example/x"]), "evidence link"),
            ("alice", flag(subject="alice", evidence=["https://github.com/alice/shop/pull/8"]), "yourself"),
        ]
        for who, obj, why in cases:
            c = self.post(who, obj)
            self.assertNotIn(c["id"], self.r.w.comments, who)
            self.assertIn(why, self.r.last_reply(self.n), (who, obj))
        self.assertEqual(self.state()["flags"], [])

    def test_two_independent_projects_list_an_account(self):
        self.post("alice", flag())
        self.assertEqual(self.listed(), {})                 # one project isn't enough
        self.assertNotIn("mallory", self.r.issue(self.n)["body"])  # uncorroborated flags aren't named
        self.post("alice", flag(signal="prompt_injection"))  # same project again: still one
        self.assertEqual(self.listed(), {})
        self.post("carol", flag(project="acme/api", evidence=["https://github.com/acme/api/issues/3"]))  # org member
        entry = self.listed()["mallory"]
        self.assertEqual((entry["status"], entry["projects"]), ("corroborated", 2))
        self.assertIn("@mallory", self.r.issue(self.n)["body"])

    def test_flags_cant_be_edited_afterwards(self):
        c = self.post("alice", flag())
        self.r.as_("alice").edit_comment(c["id"], "/flag\n```json\n" + json.dumps(flag(subject="innocent")) + "\n```")
        self.r.fresh_queue().ingest_safety(self.n)
        self.assertEqual([f["subject"] for f in self.state()["flags"]], ["mallory"])

    def test_confirm_clear_appeal_and_noise(self):
        self.post("bob", flag(project="bob/game", evidence=["https://github.com/bob/game/issues/9"]))
        c = self.r.say("alice", self.n, "/confirm @mallory")   # not an upstream maintainer
        self.assertNotIn(c["id"], self.r.w.comments)
        self.r.say("boss", self.n, "/confirm @mallory")
        self.assertEqual(self.listed()["mallory"]["status"], "confirmed")
        self.r.say("mallory", self.n, "/appeal")
        self.assertTrue(self.listed()["mallory"]["appealed"])
        rant = self.r.say("troll", self.n, "Everyone knows bob is a scammer!!!")
        self.assertNotIn(rant["id"], self.r.w.comments)
        self.r.w.advance(60)
        self.r.say("boss", self.n, "/clear @mallory")
        self.assertEqual(self.listed(), {})

    def test_entries_expire_and_publish(self):
        self.post("alice", flag())
        self.post("bob", flag(project="bob/game", evidence=["https://github.com/bob/game/pull/9"]))
        self.r.sweep()
        snap = json.loads(self.r.w.snapshots["ws-board"]["safety.json"])
        self.assertIn("mallory", snap["listed"])
        self.r.w.advance(181 * 86400)
        self.assertEqual(self.listed(), {})


class Downstream(unittest.TestCase):
    def repo(self, action="maintainer", deny=()):
        r = Repo({"safety": {"action": action}, "trust": {"deny": list(deny)}})
        listed = {"mallory": {"login": "mallory", "status": "corroborated", "projects": 2, "signals": ["malicious_code"]}}
        r.fresh_queue = lambda: Queue(r.bot, r.cfg, clock=r.w.clock, flags_loader=lambda: listed)
        r.w.merged_by_login["mallory"] = 9   # would otherwise have earned trust
        r.w.merged_by_login["rev"] = 9
        return r

    def test_listed_accounts_lose_trust_and_need_a_maintainer(self):
        r = self.repo()
        t = r.task("Feature")
        r.say("mallory", t, "/claim")
        pr = r.open_pr("mallory", t)
        self.assertIn(L["needs_maintainer"], r.labels(pr))
        self.assertIn("safety net", r.last_reply(pr))
        r.say("rev", pr, "/claim")
        r.say("rev", pr, "/approve")
        self.assertFalse(r.w.prs[pr]["merged"])
        t2 = r.task("Other feature")
        r.say("alice", t2, "/claim")
        pr2 = r.open_pr("alice", t2)
        c = r.say("mallory", pr2, "/claim")       # can't review
        self.assertTrue(r.reacted(c["id"], "-1"))
        p = r.as_("mallory").create_issue("Helpful refactor", "Please merge.\n\n<!-- ws:meta {} -->", [])
        r.pump()
        self.assertIn(L["triage"], r.labels(p["number"]))   # proposals always go to triage

    def test_block_mode_and_local_deny_list(self):
        r = self.repo(action="block")
        t = r.task("Feature")
        c = r.say("mallory", t, "/claim")
        self.assertTrue(r.reacted(c["id"], "-1"))
        self.assertIn("safety net", r.last_reply(t))
        r2 = self.repo(deny=["eve"])
        t2 = r2.task("Feature")
        c2 = r2.say("eve", t2, "/claim")
        self.assertTrue(r2.reacted(c2["id"], "-1"))


class Validation(unittest.TestCase):
    def test_schema(self):
        self.assertEqual(SAFE.validate_flag(flag())[1], [])
        for bad in (dict(flag(), signal="vibes"), dict(flag(), subject="not a login!"), dict(flag(), evidence=[]),
                    dict(flag(), evidence=["https://github.com/other/repo/pull/1"]), "x"):
            self.assertTrue(SAFE.validate_flag(bad)[1], bad)

    def test_config(self):
        with self.assertRaises(C.ConfigError):
            C.load(data={"safety": {"action": "nuke"}})


if __name__ == "__main__":
    unittest.main()

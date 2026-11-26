"""Test harness: a fake repository whose events are processed by the real queue."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("WS_HOME", tempfile.mkdtemp(prefix="ws-home-"))
os.environ.setdefault("WS_GUEST", "0")  # tests never depend on this machine's GitHub login

from workstead import config as C  # noqa: E402
from workstead.engine import Queue  # noqa: E402
from workstead.fakegh import FakeGitHub, World  # noqa: E402
from workstead.protocol import L, read_block, write_block  # noqa: E402

BOT = "github-actions[bot]"


class Repo:
    def __init__(self, cfg_over=None, private=False, maintainers=("boss",)):
        data = {"repo": "acme/project", "maintainers": list(maintainers)}
        for k, v in (cfg_over or {}).items():
            data[k] = v
        self.cfg = C.load(data=data)
        self.w = World(private=private)
        for m in maintainers:
            self.w.perms[m] = "admin"
        self.bot = FakeGitHub(self.w, BOT)
        self.q = Queue(self.bot, self.cfg, clock=self.w.clock)
        self.users = {}

    def as_(self, login: str) -> FakeGitHub:
        if login not in self.users:
            self.users[login] = FakeGitHub(self.w, login)
        return self.users[login]

    def fresh_queue(self) -> Queue:
        """A new Queue per workflow run, like Actions (no cross-run caches)."""
        return Queue(self.bot, self.cfg, clock=self.w.clock)

    def pump(self, limit: int = 10000) -> int:
        n = 0
        while self.w.events and n < limit:
            name, payload = self.w.events.pop(0)
            self.fresh_queue().handle_event(name, payload)
            n += 1
        return n

    def sweep(self):
        snap = self.fresh_queue().sweep()
        self.pump()
        return snap

    # helpers
    def issue(self, n):
        return self.w.issues[n]

    def labels(self, n):
        return set(self.w.issues[n]["labels"])

    def state(self, n):
        for cid in sorted(self.w.comments):
            c = self.w.comments[cid]
            if c["issue"] == n and c["author"] == BOT:
                st = read_block(c["body"], "state")
                if st is not None:
                    return st
        return None

    def meta(self, n):
        return read_block(self.w.issues[n]["body"], "meta")

    def comments(self, n):
        return [self.w.comments[c] for c in sorted(self.w.comments) if self.w.comments[c]["issue"] == n]

    def last_reply(self, n):
        cs = [c for c in self.comments(n) if c["author"] == BOT and "ws:state" not in c["body"]]
        return cs[-1]["body"] if cs else ""

    def job(self, title="Build the thing", body="Make a thing that does X, Y and Z.", by="boss"):
        i = self.as_(by).create_issue(title, body, [L["job"]])
        self.pump()
        return i["number"]

    def task(self, title, partition="code", deps=(), licenses=(), est=1.0, priority=3, by="boss"):
        meta = {"partition": partition, "deps": list(deps), "licenses": list(licenses), "est_hours": est,
                "priority": priority}
        i = self.as_(by).create_issue(title, f"Do {title} carefully and completely.\n\n" + write_block("meta", meta), [L["task"]])
        self.pump()
        return i["number"]

    def say(self, login, n, body):
        c = self.as_(login).create_comment(n, body)
        self.pump()
        return c

    def plan(self, login, n, tasks):
        return self.say(login, n, "/plan\n```json\n" + json.dumps({"tasks": tasks}) + "\n```")

    def children(self, n):
        return sorted(i for i in self.w.issues if (self.meta(i) or {}).get("parent") == n and self.issue(i)["author"] == BOT)

    def open_pr(self, login, task, files=("src/app.py",), sha=None):
        n = self.as_(login).open_pr(f"Fix for #{task}", f"Closes #{task}\n\nDid it.", f"ws/{task}-x", list(files), sha=sha or "")
        self.pump()
        return n

    def reacted(self, cid, content):
        return any(r[0] == cid and r[1] == content for r in self.w.reactions)

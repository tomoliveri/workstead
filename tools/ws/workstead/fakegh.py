"""An in-memory GitHub for tests and the fleet simulator.

``World`` holds the repository; ``FakeGitHub(world, login)`` is a client acting
as one account. It mirrors the GitHub behaviour the protocol relies on:

* issues and pull requests share one number space;
* comment ids increase;
* actions taken with the Actions token (the bot) raise no events, except
  workflow_dispatch, exactly like GITHUB_TOKEN;
* anyone may be assigned once they have commented.
"""
from __future__ import annotations

import copy
import itertools
from typing import Any, Callable, Dict, Iterable, List, Optional

from .gh import GitHubError
from .protocol import now_iso


class World:
    def __init__(self, repo: str = "acme/project", bot: str = "github-actions[bot]",
                 clock: Optional[Callable[[], float]] = None, private: bool = False):
        self.repo = repo
        self.bot = bot
        self.t = 1_800_000_000.0
        self.clock = clock or (lambda: self.t)
        self.private = private
        self.issues: Dict[int, Dict[str, Any]] = {}
        self.prs: Dict[int, Dict[str, Any]] = {}
        self.comments: Dict[int, Dict[str, Any]] = {}
        self.numbers = itertools.count(1)
        self.cids = itertools.count(1000)
        self.ids = itertools.count(900000)
        self.perms: Dict[str, str] = {}
        self.checks: Dict[str, str] = {}
        self.labels: Dict[str, Dict[str, str]] = {}
        self.events: List[tuple] = []
        self.snapshots: Dict[str, Dict[str, str]] = {}
        self.sub_issues: Dict[int, List[int]] = {}
        self.blocked_by: Dict[int, List[int]] = {}
        self.reactions: List[tuple] = []
        self.calls = 0
        self.merged_by_login: Dict[str, int] = {}
        self.template: Optional[str] = None
        self.fail_next: List[str] = []  # method names that raise once (fault injection)
        # Other repositories, for safety-net verification:
        # {"owner/name": {"owner": login, "type": "User"|"Organization", "members": [...], "issues": {n: author}}}
        self.foreign: Dict[str, Dict[str, Any]] = {}
        self.heads: Dict[str, str] = {}

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> str:
        return now_iso(self.clock())

    def emit(self, name: str, payload: Dict[str, Any], actor: str) -> None:
        if actor == self.bot:
            return  # GITHUB_TOKEN never triggers workflows
        payload = dict(payload, sender={"login": actor, "type": "Bot" if actor.endswith("[bot]") else "User"},
                       repository={"full_name": self.repo, "private": self.private})
        self.events.append((name, payload))

    def raw_issue(self, n: int) -> Dict[str, Any]:
        i = self.issues[n]
        d = {"number": n, "id": i["id"], "node_id": f"I_{i['id']}", "title": i["title"], "body": i["body"],
             "state": i["state"], "state_reason": i["state_reason"],
             "labels": [{"name": x} for x in i["labels"]],
             "assignees": [{"login": a} for a in i["assignees"]], "user": {"login": i["author"]},
             "author_association": i["author_association"], "created_at": i["created_at"],
             "updated_at": i["updated_at"], "closed_at": i["closed_at"]}
        if i["is_pr"]:
            d["pull_request"] = {"url": ""}
        return d

    def raw_comment(self, cid: int) -> Dict[str, Any]:
        c = self.comments[cid]
        return {"id": cid, "node_id": f"C_{cid}", "body": c["body"], "user": {"login": c["author"]},
                "created_at": c["created_at"], "updated_at": c["updated_at"],
                "issue_url": f"https://api.github.com/repos/{self.repo}/issues/{c['issue']}",
                "author_association": c["author_association"]}

    def raw_pr(self, n: int) -> Dict[str, Any]:
        p, i = self.prs[n], self.issues[n]
        return {"number": n, "title": i["title"], "body": i["body"], "user": {"login": i["author"]},
                "state": i["state"], "merged": p["merged"], "draft": p["draft"],
                "head": {"sha": p["sha"], "ref": p["head_ref"], "repo": {"full_name": p["head_repo"]}},
                "base": {"ref": p["base"]}, "mergeable": p["mergeable"],
                "labels": [{"name": x} for x in i["labels"]],
                "assignees": [{"login": a} for a in i["assignees"]],
                "created_at": i["created_at"], "updated_at": i["updated_at"],
                "additions": p.get("additions", 10), "deletions": p.get("deletions", 2),
                "changed_files": len(p["files"])}

    def association(self, login: str) -> str:
        perm = self.perms.get(login, "none")
        if perm == "admin":
            return "OWNER"
        if perm in ("write", "maintain"):
            return "COLLABORATOR"
        return "NONE"


class FakeGitHub:
    def __init__(self, world: World, login: str):
        self.w = world
        self.login = login
        self.repo = world.repo
        self.remaining = 5000
        self.calls = 0

    def _call(self, name: str) -> None:
        self.calls += 1
        self.w.calls += 1
        if name in self.w.fail_next:
            self.w.fail_next.remove(name)
            raise GitHubError(502, f"injected failure in {name}")

    def budget_ok(self) -> bool:
        return True

    def _touch(self, n: int) -> None:
        self.w.issues[n]["updated_at"] = self.w.now()

    # identity
    def me(self) -> str:
        return self.login

    def repo_info(self) -> Dict[str, Any]:
        self._call("repo_info")
        return {"private": self.w.private, "default_branch": "main",
                "can_push": self.w.perms.get(self.login) in ("admin", "write", "maintain"),
                "fork": False, "template": self.w.template, "parent": None,
                "owner": self.repo.split("/")[0], "owner_type": "User"}

    def permission(self, login: str) -> str:
        self._call("permission")
        return self.w.perms.get(login, "none")

    def search_count(self, q: str) -> int:
        self._call("search_count")
        if "expired" in q:
            import re as _re
            m = _re.search(r"ws:expired @([A-Za-z0-9-]+)", q)
            who = m.group(1) if m else ""
            return sum(1 for c in self.w.comments.values() if f"ws:expired @{who}" in c["body"])
        return 0

    def merged_pr_count(self, login: str, label: str) -> int:
        self._call("merged_pr_count")
        return self.w.merged_by_login.get(login, 0)

    # issues
    def get_issue(self, n: int, _count: bool = True) -> Dict[str, Any]:
        if _count:
            self._call("get_issue")
        if n not in self.w.issues:
            raise GitHubError(404, "Not Found")
        from .gh import _issue
        return _issue(self.w.raw_issue(n))

    def list_issues(self, labels: Iterable[str] = (), state: str = "open", since: Optional[str] = None,
                    assignee: Optional[str] = None, include_prs: bool = False, limit: int = 10000) -> List[Dict[str, Any]]:
        self._call("list_issues")
        from .gh import _issue
        labels = list(labels)
        out = []
        for n in sorted(self.w.issues):
            i = self.w.issues[n]
            if state != "all" and i["state"] != state:
                continue
            if any(lab not in i["labels"] for lab in labels):
                continue
            if since and (i["updated_at"] or "") < since:
                continue
            if assignee and assignee not in i["assignees"]:
                continue
            if i["is_pr"] and not include_prs:
                continue
            out.append(_issue(self.w.raw_issue(n)))
        return out[:limit]

    def _new_issue(self, title: str, body: str, labels: List[str], is_pr: bool = False) -> int:
        n = next(self.w.numbers)
        self.w.issues[n] = {"id": next(self.w.ids), "title": title, "body": body, "state": "open",
                            "state_reason": None, "labels": list(dict.fromkeys(labels)), "assignees": [],
                            "author": self.login, "author_association": self.w.association(self.login),
                            "created_at": self.w.now(), "updated_at": self.w.now(), "closed_at": None,
                            "is_pr": is_pr, "commenters": {self.login}, "locked": False}
        return n

    def create_issue(self, title: str, body: str, labels: List[str]) -> Dict[str, Any]:
        self._call("create_issue")
        n = self._new_issue(title, body, labels)
        self.w.emit("issues", {"action": "opened", "issue": self.w.raw_issue(n)}, self.login)
        return self.get_issue(n, _count=False)

    def edit_issue(self, n: int, **fields: Any) -> Dict[str, Any]:
        self._call("edit_issue")
        i = self.w.issues[n]
        was = i["state"]
        old_body = i["body"]
        for k in ("title", "body", "state", "state_reason"):
            if fields.get(k) is not None:
                i[k] = fields[k]
        if i["state"] == "closed" and was == "open":
            i["closed_at"] = self.w.now()
            i["state_reason"] = fields.get("state_reason") or "completed"
            self.w.emit("issues", {"action": "closed", "issue": self.w.raw_issue(n)}, self.login)
        elif i["state"] == "open" and was == "closed":
            i["closed_at"] = None
            i["state_reason"] = "reopened"
            self.w.emit("issues", {"action": "reopened", "issue": self.w.raw_issue(n)}, self.login)
        elif fields.get("body") is not None and old_body != i["body"]:
            self.w.emit("issues", {"action": "edited", "issue": self.w.raw_issue(n),
                                   "changes": {"body": {"from": old_body}}}, self.login)
        self._touch(n)
        return self.get_issue(n, _count=False)

    def add_labels(self, n: int, labels: List[str]) -> None:
        self._call("add_labels")
        for lab in labels:
            if lab not in self.w.issues[n]["labels"]:
                self.w.issues[n]["labels"].append(lab)
        self._touch(n)

    def remove_label(self, n: int, label: str) -> None:
        self._call("remove_label")
        if label in self.w.issues[n]["labels"]:
            self.w.issues[n]["labels"].remove(label)
        self._touch(n)

    def add_assignees(self, n: int, logins: List[str]) -> None:
        self._call("add_assignees")
        i = self.w.issues[n]
        for who in logins:
            allowed = who in i["commenters"] or self.w.perms.get(who) in ("admin", "write", "maintain")
            if allowed and who not in i["assignees"] and len(i["assignees"]) < 10:
                i["assignees"].append(who)
        self._touch(n)

    def remove_assignees(self, n: int, logins: List[str]) -> None:
        self._call("remove_assignees")
        i = self.w.issues[n]
        i["assignees"] = [a for a in i["assignees"] if a not in logins]
        self._touch(n)

    # comments
    def list_comments(self, n: int) -> List[Dict[str, Any]]:
        self._call("list_comments")
        from .gh import _comment
        return [_comment(self.w.raw_comment(c)) for c in sorted(self.w.comments) if self.w.comments[c]["issue"] == n]

    def repo_comments_since(self, since: str) -> List[Dict[str, Any]]:
        self._call("repo_comments_since")
        from .gh import _comment
        cs = [c for c in self.w.comments if self.w.comments[c]["updated_at"] >= since]
        cs.sort(key=lambda c: self.w.comments[c]["updated_at"])
        return [_comment(self.w.raw_comment(c)) for c in cs]

    def create_comment(self, n: int, body: str) -> Dict[str, Any]:
        self._call("create_comment")
        i = self.w.issues[n]
        if i.get("locked") and self.w.perms.get(self.login) not in ("admin", "write") and self.login != self.w.bot:
            raise GitHubError(403, "issue is locked")
        cid = next(self.w.cids)
        self.w.comments[cid] = {"issue": n, "author": self.login, "body": body, "created_at": self.w.now(),
                                "updated_at": self.w.now(), "author_association": self.w.association(self.login)}
        i["commenters"].add(self.login)
        self._touch(n)
        self.w.emit("issue_comment", {"action": "created", "issue": self.w.raw_issue(n),
                                      "comment": self.w.raw_comment(cid)}, self.login)
        from .gh import _comment
        return _comment(self.w.raw_comment(cid))

    def edit_comment(self, cid: int, body: str) -> Dict[str, Any]:
        self._call("edit_comment")
        c = self.w.comments.get(cid)
        if c is None:
            raise GitHubError(404, "Not Found")
        if c["author"] != self.login and self.login != self.w.bot and self.w.perms.get(self.login) != "admin":
            raise GitHubError(403, "cannot edit another user's comment")
        c["body"] = body
        c["updated_at"] = self.w.now()
        self.w.emit("issue_comment", {"action": "edited", "issue": self.w.raw_issue(c["issue"]),
                                      "comment": self.w.raw_comment(cid)}, self.login)
        from .gh import _comment
        return _comment(self.w.raw_comment(cid))

    def delete_comment(self, cid: int) -> None:
        self._call("delete_comment")
        self.w.comments.pop(cid, None)

    def react(self, cid: int, content: str) -> None:
        self._call("react")
        self.w.reactions.append((cid, content, self.login))

    def add_sub_issue(self, parent: int, child_id: int) -> bool:
        self._call("add_sub_issue")
        self.w.sub_issues.setdefault(parent, []).append(child_id)
        return True

    def add_blocked_by(self, n: int, blocker_id: int) -> bool:
        self._call("add_blocked_by")
        self.w.blocked_by.setdefault(n, []).append(blocker_id)
        return True

    def pin_issue(self, node_id: str) -> bool:
        self._call("pin_issue")
        return True

    # pull requests
    def open_pr(self, title: str, body: str, head_ref: str, files: List[str], sha: str = "",
                head_repo: Optional[str] = None) -> int:
        """Test helper standing in for git push + create_pr."""
        self._call("create_pr")
        n = self._new_issue(title, body, [], is_pr=True)
        self.w.prs[n] = {"merged": False, "draft": False, "sha": sha or f"{n:07x}aaaa", "head_ref": head_ref,
                         "head_repo": head_repo or self.repo, "base": "main", "mergeable": True, "files": files}
        self.w.emit("pull_request_target", {"action": "opened", "pull_request": self.w.raw_pr(n),
                                            "number": n}, self.login)
        return n

    def create_pr(self, head: str, base: str, title: str, body: str, draft: bool = False) -> Dict[str, Any]:
        n = self.open_pr(title, body, head.split(":")[-1], ["src/x.py"])
        return self.get_pr(n)

    def push_pr(self, n: int, sha: str, files: Optional[List[str]] = None) -> None:
        """Test helper: new commits on a pull request."""
        self.w.prs[n]["sha"] = sha
        if files is not None:
            self.w.prs[n]["files"] = list(files)
        self.w.emit("pull_request_target", {"action": "synchronize", "pull_request": self.w.raw_pr(n),
                                            "number": n}, self.login)

    def find_pr(self, head: str) -> Optional[Dict[str, Any]]:
        ref = head.split(":")[-1]
        for n, p in self.w.prs.items():
            if p["head_ref"] == ref and self.w.issues[n]["state"] == "open":
                return self.get_pr(n)
        return None

    def get_pr(self, n: int) -> Dict[str, Any]:
        self._call("get_pr")
        from .gh import _pr
        return _pr(self.w.raw_pr(n))

    def pr_files(self, n: int) -> List[str]:
        self._call("pr_files")
        return list(self.w.prs[n]["files"])

    def pr_patches(self, n: int) -> Dict[str, str]:
        self._call("pr_patches")
        p = self.w.prs[n]
        return {f: (p.get("patches") or {}).get(f, "") for f in p["files"]}

    def repo_owner(self, full: str):
        self._call("repo_owner")
        f = self.w.foreign.get(full)
        if not f or f.get("private"):
            raise GitHubError(404, "Not Found")
        return f["owner"], f.get("type", "User")

    def public_member(self, org: str, login: str) -> bool:
        self._call("public_member")
        return any(f["owner"] == org and login in f.get("members", []) for f in self.w.foreign.values())

    def foreign_author(self, full: str, n: int) -> str:
        self._call("foreign_author")
        return self.w.foreign[full]["issues"][n]

    def pr_reviews(self, n: int) -> List[Dict[str, Any]]:
        self._call("pr_reviews")
        return list(self.w.prs[n].get("reviews", []))

    def review_pr(self, n: int, state: str = "APPROVED") -> None:
        """Test helper: a native GitHub review (no workflow event, like a fork PR)."""
        self.w.prs[n].setdefault("reviews", []).append({"login": self.login, "state": state, "sha": self.w.prs[n]["sha"]})

    def prs_for_sha(self, sha: str) -> List[int]:
        self._call("prs_for_sha")
        return [n for n, p in self.w.prs.items() if p["sha"] == sha]

    def branch_sha(self, branch: str) -> str:
        self._call("branch_sha")
        return self.w.heads.get(branch, "b" * 40)

    def checks_state(self, sha: str) -> str:
        self._call("checks_state")
        return self.w.checks.get(sha, "success")

    def merge_pr(self, n: int, sha: str, method: str) -> bool:
        self._call("merge_pr")
        p, i = self.w.prs[n], self.w.issues[n]
        if p["merged"] or i["state"] != "open" or p["sha"] != sha or not p["mergeable"]:
            return False
        p["merged"] = True
        i["state"] = "closed"
        i["closed_at"] = self.w.now()
        self.w.merged_by_login[i["author"]] = self.w.merged_by_login.get(i["author"], 0) + 1
        self.w.emit("pull_request_target", {"action": "closed", "pull_request": self.w.raw_pr(n),
                                            "number": n}, self.login)
        return True

    # housekeeping
    def ensure_label(self, name: str, color: str, description: str) -> str:
        self._call("ensure_label")
        had = name in self.w.labels
        self.w.labels[name] = {"color": color, "description": description}
        return "updated" if had else "created"

    def dispatch(self, workflow: str, ref: str, inputs: Dict[str, str]) -> None:
        self._call("dispatch")
        # Allowed even from the bot token, like the real API.
        self.w.events.append(("workflow_dispatch", {"inputs": dict(inputs), "workflow": workflow,
                                                    "sender": {"login": self.login, "type": "Bot"}}))

    def publish_snapshot(self, branch: str, files: Dict[str, str], message: str) -> None:
        self._call("publish_snapshot")
        self.w.snapshots[branch] = copy.deepcopy(files)

    def read_snapshot(self, branch: str, path: str, public: bool = True) -> Optional[str]:
        self._call("read_snapshot")
        return self.w.snapshots.get(branch, {}).get(path)

"""The queue: the GitHub Actions side of workstead.

How it stays correct with thousands of workers and no server:

1. **The comment log is the truth.** Each issue's commands are processed in
   comment-id order, from a cursor kept in the queue's sticky state comment.
   Re-running a reconcile is harmless: it picks up where the cursor stopped.
2. **One writer per issue.** The workflow runs in a concurrency group per
   issue number, so two runs never mutate one issue at once. If GitHub drops a
   queued run, the next run still processes every unprocessed comment.
3. **Cross-issue effects are dispatched.** When an event on one issue must
   change another (a merged PR completes its task, a child completes its
   parent), the queue fires ``workflow_dispatch`` for the other issue, so its
   own serialized run applies the change.
4. **Labels and assignees are a projection** of the state, recomputed on
   every write and repaired by the sweep.
5. **Earliest claim wins.** Claims are applied in comment order, so of two
   simultaneous ``/claim`` comments the earlier one always gets the lease, in
   every run.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as C
from . import dedup
from . import learnings as LRN
from . import safety as SAFE
from . import plan as P
from . import sched
from .gh import GitHubError, log
from .protocol import (CLOSED_STATES, beat_ref, L, LABEL_INFO, SQUEUE, STATE_LABELS,
                       clean_meta, json_block, linked_task, log as hist, new_state,
                       now_iso, parse_command, parse_ts, partition_label, read_block,
                       replace_block, status_from_labels, status_label, write_block)

COLLAB = ("OWNER", "MEMBER", "COLLABORATOR")


class Queue:
    def __init__(self, gh, cfg: Dict[str, Any], workflow: str = "ws-queue.yml", ref: str = "",
                 clock: Callable[[], float] = time.time, private: Optional[bool] = None,
                 flags_loader: Optional[Callable[[], Dict[str, Any]]] = None):
        self.gh = gh
        self.cfg = cfg
        self.workflow = workflow
        self.ref = ref or cfg["default_branch"]
        self.clock = clock
        self._private = private
        self._trust: Dict[str, bool] = {}
        self._maint: Dict[str, bool] = {}
        self._issue_cache: Dict[int, Dict[str, Any]] = {}
        self.dispatched: List[Tuple[int, str]] = []
        self.flags_loader = flags_loader
        self._flags: Optional[Dict[str, Any]] = None
        self.bots = set(cfg["bots"]) | ({os.environ["WS_BOT_LOGIN"]} if os.environ.get("WS_BOT_LOGIN") else set())

    # --- time & identity ----------------------------------------------------

    def now(self) -> float:
        return self.clock()

    def ts(self) -> str:
        return now_iso(self.now())

    def is_bot(self, login: str) -> bool:
        return login in self.bots

    @property
    def mode(self) -> str:
        m = self.cfg["trust"]["mode"]
        if m != "auto":
            return m
        if self._private is None:
            try:
                self._private = bool(self.gh.repo_info()["private"])
            except GitHubError:
                self._private = False
        return "open" if self._private else "earned"

    # --- the shared safety net ---------------------------------------------------

    def safety_upstream(self) -> Optional[str]:
        s = self.cfg["safety"]
        if s["upstream"]:
            return s["upstream"]
        try:
            info = self.gh.repo_info()
        except GitHubError:
            return None
        return self.cfg["give_back"]["upstream"] or info.get("template") or self.gh.repo

    def shared_list(self) -> Dict[str, Any]:
        """Accounts listed by the shared safety net, keyed by lowercase login."""
        if self._flags is not None:
            return self._flags
        self._flags = {}
        if self.flags_loader is not None:
            self._flags = self.flags_loader() or {}
            return self._flags
        if not self.cfg["safety"]["shared"]:
            return self._flags
        up = self.safety_upstream()
        if not up:
            return self._flags
        try:
            src = self.gh if up.lower() == self.gh.repo.lower() else type(self.gh)(up)
            raw = src.read_snapshot(self.cfg["board"]["branch"], "safety.json", public=True)
            self._flags = (json.loads(raw) or {}).get("listed", {}) if raw else {}
        except (GitHubError, ValueError, TypeError) as e:
            log(f"safety list unavailable ({e}); continuing without it")
        return self._flags

    def flagged(self, login: str) -> Optional[Dict[str, Any]]:
        """Why this account isn't trusted by the safety net, or None."""
        if not login or self.is_bot(login):
            return None
        if login.lower() in {x.lower() for x in self.cfg["trust"]["deny"]}:
            return {"status": "denied", "signals": ["local deny list"], "projects": 0}
        return self.shared_list().get(login.lower())

    def blocked(self, login: str) -> bool:
        f = self.flagged(login)
        return bool(f) and (f["status"] == "denied" or self.cfg["safety"]["action"] == "block")

    def is_maintainer(self, login: str, assoc: Optional[str] = None) -> bool:
        """Write access or listed. author_association answers most cases for free."""
        if login in self._maint:
            return self._maint[login]
        ok = login in self.cfg["maintainers"]
        if not ok and assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN"):
            self._maint[login] = False
            return False
        if not ok and assoc == "OWNER":
            ok = True
        elif not ok and not self.is_bot(login):
            try:
                ok = self.gh.permission(login) in ("admin", "maintain", "write")
            except GitHubError:
                ok = False
        self._maint[login] = ok
        return ok

    def is_trusted(self, login: str, assoc: Optional[str] = None) -> bool:
        if login in self._trust:
            return self._trust[login]
        t = self.cfg["trust"]
        if self.flagged(login):
            self._trust[login] = False
            return False
        ok = (self.is_bot(login) or login in t["allow"] or self.mode == "open" or self.is_maintainer(login, assoc))
        if not ok and t["trusted_after_merged"] > 0:
            try:
                ok = self.gh.merged_pr_count(login, L["pr"]) >= t["trusted_after_merged"]
            except GitHubError:
                ok = False
        self._trust[login] = ok
        return ok

    def authored_by_trusted(self, issue: Dict[str, Any]) -> bool:
        a = issue["author"]
        if self.is_bot(a):
            return True
        return (a in self.cfg["maintainers"] or issue.get("author_association") in COLLAB) and not self.flagged(a)

    # --- loading and saving -------------------------------------------------

    def meta_of(self, issue: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """A task's meta, only if its body was written by a trusted author."""
        raw = read_block(issue.get("body"), "meta")
        if raw is None or not self.authored_by_trusted(issue):
            return None
        m = clean_meta(raw, self.cfg["partitions"])
        if not m["partition"]:
            m["partition"] = self.cfg["default_partition"]
        return m

    def load(self, n: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
        issue = self.gh.get_issue(n)
        comments = self.gh.list_comments(n)
        sc, state = None, None
        for c in comments:
            if self.is_bot(c["author"]):
                st = read_block(c["body"], "state")
                if st is not None:
                    sc, state = c, st
                    break
        if state is None:
            kind = "pr" if issue["is_pr"] else "task"
            state = new_state(kind, status_from_labels(issue["labels"]) or ("queued" if kind == "task" else ""))
            if kind == "task" and state["status"] in CLOSED_STATES and issue["state"] == "open":
                state["status"] = "queued"
        return issue, comments, sc, state

    def save(self, n: int, issue: Dict[str, Any], sc: Optional[Dict[str, Any]],
             before: str, state: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> None:
        state["v"] = 1
        if json.dumps(state, sort_keys=True) == before and sc is not None:
            return
        body = self.render_status(n, state, meta) + "\n\n" + write_block("state", state)
        if sc is None:
            self.gh.create_comment(n, body)
        else:
            self.gh.edit_comment(sc["id"], body)

    def project_task(self, n: int, issue: Dict[str, Any], state: Dict[str, Any], meta: Optional[Dict[str, Any]],
                     deps_ready: Optional[bool] = None) -> None:
        """Make labels, assignees and open/closed match the state."""
        status = state["status"]
        if deps_ready is None:
            deps_ready = status != "queued" or (meta is None) or \
                sched.deps_state(meta, self.dep_status)[0] == "ready"
        want = {L["task"], status_label(status, deps_ready)}
        if meta:
            want.add(partition_label(meta["partition"]))
        have = set(issue["labels"])
        managed = set(STATE_LABELS) | {x for x in have if x.startswith("p:")}
        add = sorted(want - have)
        remove = sorted((have & managed) - want)
        if add:
            self.gh.add_labels(n, add)
        for x in remove:
            self.gh.remove_label(n, x)
        people = {x["login"] for x in (state.get("lease"), state.get("review_lease")) if x}
        cur = set(issue["assignees"])
        if cur - people:
            self.gh.remove_assignees(n, sorted(cur - people))
        if people - cur:
            self.gh.add_assignees(n, sorted(people - cur))
        if status in CLOSED_STATES and issue["state"] == "open":
            self.gh.edit_issue(n, state="closed", state_reason="completed" if status == "done" else "not_planned")
        elif status not in CLOSED_STATES and issue["state"] == "closed":
            self.gh.edit_issue(n, state="open")
        issue["labels"] = sorted((have | set(add)) - set(remove))

    def dispatch(self, n: int, reason: str, kind: str = "") -> None:
        self.dispatched.append((n, reason))
        if not kind:
            kind = "task" if reason.split(":")[0] in ("expire_task", "child_closed", "child_added", "refresh", "cancel",
                                                      "pr_opened", "pr_closed", "merged", "changes", "conflict",
                                                      "checks_failed") else ""
        try:
            self.gh.dispatch(self.workflow, self.ref, {"issue": str(n), "reason": reason, "kind": kind})
        except GitHubError as e:
            log(f"dispatch #{n} {reason} failed: {e}; the sweep will catch it")

    def dep_status(self, d: int) -> str:
        """done, failed, cancelled, open or missing, for a dependency."""
        if d not in self._issue_cache:
            try:
                self._issue_cache[d] = self.gh.get_issue(d)
            except GitHubError:
                return "missing"
        i = self._issue_cache[d]
        if L["task"] not in i["labels"] or not self.authored_by_trusted(i):
            return "missing"
        if i["state"] == "open":
            return "open"
        if L["done"] in i["labels"] or (i.get("state_reason") == "completed" and L["failed"] not in i["labels"]):
            return "done"
        return "failed" if L["failed"] in i["labels"] else "cancelled"

    # --- rendering ----------------------------------------------------------

    def render_status(self, n: int, s: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> str:
        if s.get("kind") == "pr":
            rows = [("Task", f"#{s['task']}" if s.get("task") else "not linked"),
                    ("Approvals", ", ".join(f"@{a['login']}" for a in s.get("approvals", [])) or "none yet"),
                    ("Reviewer", f"@{s['review_lease']['login']}" if s.get("review_lease") else "unclaimed: comment `/claim` to review"),
                    ("Review rounds", str(s.get("rounds", 0)))]
            if s.get("reason"):
                rows.append(("Note", s["reason"]))
            head = "### Workstead review"
        else:
            st = s["status"]
            lease = s.get("lease")
            what = {"queued": "waiting to be claimed", "running": "leased", "review": "submitted, in review",
                    "split": "split into child tasks", "held": "held for a human", "triage": "waiting for admission",
                    "done": "completed", "failed": "failed", "cancelled": "cancelled"}[st]
            rows = [("State", f"`{SQUEUE[st]}` **{st}**: {what}" + (f" ({s['reason']})" if s.get("reason") else ""))]
            if lease:
                rows.append(("Lease", f"@{lease['login']} (node `{lease.get('node') or '-'}`) since {lease['since']}, "
                                      f"time limit {lease.get('limit_h', '?')} h, heartbeat every "
                                      f"{self.cfg['leases']['heartbeat_minutes']} min"))
            if s.get("review_lease"):
                rows.append(("Reviewer", f"@{s['review_lease']['login']}"))
            if meta:
                rows.append(("Partition", f"`{meta['partition']}` · priority {meta['priority']} · est {meta['est_hours']} h"
                             + (f" · needs {', '.join(meta['features'])}" if meta["features"] else "")
                             + (f" · licenses {', '.join(meta['licenses'])}" if meta["licenses"] else "")))
                if meta["deps"]:
                    rows.append(("Depends on", ", ".join(f"#{d}" for d in meta["deps"]) + f" (after{meta['after']})"))
                if meta.get("parent"):
                    rows.append(("Parent", f"#{meta['parent']}" + (f" · job #{meta['job']}" if meta.get("job") and meta["job"] != meta["parent"] else "")))
            if s.get("children"):
                rows.append(("Children", ", ".join(f"#{c}" for c in s["children"][:80])))
            if s.get("pr"):
                rows.append(("Pull request", f"#{s['pr']}"))
            rows.append(("Attempts", f"{s.get('attempts', 0)} of {self.cfg['leases']['max_attempts']}"))
            head = "### Workstead status"
        table = "\n".join(["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows])
        hist_rows = "\n".join(f"- {h[0]} {h[1]}" + (f" @{h[2]}" if h[2] else "") for h in s.get("history", [])[-12:])
        return (f"{head}\n\n{table}\n\n<details><summary>History</summary>\n\n{hist_rows or '- (none)'}\n\n</details>\n\n"
                "<sub>Maintained by the workstead queue. Commands are the first line of a comment: "
                "<code>/claim</code>, <code>/release</code>, <code>/done</code>, <code>/plan</code>, <code>/fail</code>, "
                "<code>/approve</code>, <code>/request-changes</code>. See docs/protocol.md.</sub>")

    # --- entry point: webhook events ----------------------------------------

    def handle_event(self, name: str, payload: Dict[str, Any]) -> None:
        sender = (payload.get("sender") or {})
        if self._private is None and isinstance(payload.get("repository"), dict) and "private" in payload["repository"]:
            self._private = bool(payload["repository"]["private"])
        if name == "workflow_dispatch":
            inp = payload.get("inputs") or {}
            if inp.get("issue"):
                n = int(inp["issue"])
                reason = inp.get("reason") or "refresh"
                kind = inp.get("kind") or ("pr" if self.gh.get_issue(n)["is_pr"] else "task")
                if kind == "pr":
                    self.reconcile_pr(n, reason)
                else:
                    self.reconcile(n, reason)
            return
        if name == "workflow_run":
            sha = (payload.get("workflow_run") or {}).get("head_sha")
            for pr in self.gh.prs_for_sha(sha) if sha else []:
                self.dispatch(pr, "checks", "pr")
            return
        if sender.get("type") == "Bot" and self.is_bot(sender.get("login", "")):
            return
        action = payload.get("action")
        if name == "issue_comment":
            if action != "created":
                return  # edits are heartbeats; the sweep reads them
            issue = payload["issue"]
            n = issue["number"]
            names = [x.get("name") for x in issue.get("labels", [])]
            if "pull_request" in issue:
                self.reconcile_pr(n)
            elif L["learnings"] in names:
                self.ingest_learnings(n, payload["comment"])
            elif L["safety"] in names:
                self.ingest_safety(n)
            elif any(x in (L["task"], L["proposed"], L["job"]) for x in names):
                self.reconcile(n)
            return
        if name == "issues":
            n = payload["issue"]["number"]
            if action == "opened":
                self.on_opened(n)
            elif action in ("closed", "reopened"):
                labels = [x.get("name") for x in payload["issue"].get("labels", [])]
                if L["task"] in labels:
                    self.reconcile(n, action)
            return
        if name in ("pull_request_target", "pull_request"):
            n = payload.get("number") or payload["pull_request"]["number"]
            self.reconcile_pr(n, f"pr_{action}")

    # --- new issues: jobs, proposals, manual tasks --------------------------

    def on_opened(self, n: int) -> None:
        issue = self.gh.get_issue(n)
        labels = set(issue["labels"])
        raw_meta = read_block(issue["body"], "meta")
        if not (labels & {L["job"], L["proposed"], L["task"]}) and raw_meta is None:
            return  # an ordinary issue; not ours
        if self.is_bot(issue["author"]):
            return  # created by the queue itself, already initialised
        state = new_state("task", "triage")
        before = json.dumps(state, sort_keys=True)
        is_job = L["job"] in labels
        dups = dedup.matches(issue["title"], issue["body"],
                             [i for i in self.gh.list_issues(labels=[L["task"]]) if i["number"] != n],
                             self.cfg["dedup"]["similarity"])
        notes = []
        if dups:
            self.gh.add_labels(n, [L["possible_duplicate"]])
            notes.append("This looks like " + ", ".join(f"#{d} ({s:.0%} similar)" for d, s in dups)
                         + ". A triager will decide whether it is new.")
        author = issue["author"]
        if not dups and self.authored_by_trusted(issue):
            meta = self._meta_for_new(issue, raw_meta, is_job)
            body = replace_block(issue["body"], "meta", meta)
            if body != issue["body"]:
                self.gh.edit_issue(n, body=body)
                issue["body"] = body
            state["status"] = "queued"
            if L["proposed"] in issue["labels"]:
                self.gh.remove_label(n, L["proposed"])
                issue["labels"].remove(L["proposed"])
            hist(state, "admitted (trusted author)", author, self.ts())
            meta = self.meta_of(issue)
            self.project_task(n, issue, state, meta)
            self.save(n, issue, None, before, state, meta)
            return
        if not dups and self.is_trusted(author):
            new = self.admit_copy(issue, raw_meta, is_job, author, "trusted proposer")
            self.gh.create_comment(n, f"Admitted as #{new}. Workstead tasks are written by the queue so that "
                                      "their text cannot change after admission. Continue there.")
            self.gh.edit_issue(n, state="closed", state_reason="not_planned")
            return
        hist(state, "proposed", author, self.ts())
        self.gh.add_labels(n, [L["task"], L["triage"], L["proposed"]])
        notes.append("A trusted worker or maintainer reviews proposals: `/admit`, `/reject REASON` or "
                     "`/duplicate #N`. Trusted workers can comment `/claim` to take the triage.")
        self.gh.create_comment(n, render_note(notes) + "\n\n" + write_block("state", state))

    def _meta_for_new(self, issue: Dict[str, Any], raw_meta: Optional[Dict[str, Any]], is_job: bool) -> Dict[str, Any]:
        meta = clean_meta(raw_meta or form_fields(issue.get("body", "")), self.cfg["partitions"])
        if is_job:
            meta.update(partition="plan", origin="job", depth=0, parent=None, job=issue["number"])
        elif not meta["partition"]:
            meta["partition"] = self.cfg["default_partition"]
        if meta["origin"] == "found" and meta.get("found_in"):
            src = self._safe_issue(meta["found_in"])
            sm = self.meta_of(src) if src else None
            if sm:  # found while working on a real task: attach to its job
                job = sm.get("job") or meta["found_in"]
                ji = self._safe_issue(job)
                if ji and ji["state"] == "open":
                    meta["job"] = job
                    meta["parent"] = job
        return meta

    def _safe_issue(self, n: int) -> Optional[Dict[str, Any]]:
        try:
            return self.gh.get_issue(n)
        except GitHubError:
            return None

    def admit_copy(self, issue: Dict[str, Any], raw_meta: Optional[Dict[str, Any]], is_job: bool,
                   admitter: str, why: str, priority: Optional[int] = None) -> int:
        """Re-create a proposal as a queue-authored task, so it can't be edited later."""
        meta = self._meta_for_new(issue, raw_meta, is_job)
        meta["source"] = issue["number"]
        if priority is not None:
            meta["priority"] = priority
        body = re.sub(r"<!--\s*ws:meta.*?-->", "", issue["body"] or "", flags=re.S).strip()
        text = (f"{body}\n\n---\n<sub>Proposed by @{issue['author']} in #{issue['number']}; "
                f"admitted by @{admitter} ({why}).</sub>\n\n{write_block('meta', meta)}\n")
        ready = sched.deps_state(meta, self.dep_status)[0] == "ready"
        labels = [L["task"], partition_label(meta["partition"]), L["ready"] if ready else L["pending"]]
        if is_job:
            labels.append(L["job"])
        new = self.gh.create_issue(issue["title"], text, labels)
        if meta.get("parent"):
            self.gh.add_sub_issue(meta["parent"], new["id"])
            self.dispatch(meta["parent"], f"child_added:{new['number']}")
        if is_job:
            self.gh.edit_issue(new["number"], body=replace_block(text, "meta", dict(meta, job=new["number"])))
        return new["number"]

    # --- entry point: one task issue ----------------------------------------

    def reconcile(self, n: int, reason: str = "") -> None:
        issue, comments, sc, state = self.load(n)
        if issue["is_pr"]:
            self.reconcile_pr(n, reason)
            return
        self._comments = comments
        before = json.dumps(state, sort_keys=True) if sc else ""
        meta = self.meta_of(issue)
        replies: List[str] = []
        after_save: List[Callable[[], None]] = []

        for c in comments:
            if c["id"] <= state.get("cursor", 0) or self.is_bot(c["author"]):
                continue
            state["cursor"] = c["id"]
            cmd = parse_command(c["body"])
            if cmd is None:
                continue
            ok, msg = self.apply(n, issue, state, meta, c, cmd, after_save)
            if ok or msg:
                self.gh.react(c["id"], "+1" if ok else "-1")
            if msg:
                replies.append(msg)
        meta = self.meta_of(issue)  # commands may have rewritten the body

        if reason:
            msg = self.apply_reason(n, issue, state, meta, reason, after_save)
            if msg:
                replies.append(msg)

        # Closed by a human or by a merge (issues.closed): settle the state.
        if issue["state"] == "closed" and state["status"] not in CLOSED_STATES:
            if issue.get("state_reason") == "completed":
                self.finish(n, state, "done", "closed as completed", after_save, meta)
            else:
                self.finish(n, state, "cancelled", "closed as not planned", after_save, meta)
        elif issue["state"] == "open" and state["status"] in CLOSED_STATES and reason == "reopened":
            state["status"] = "queued"
            state["lease"] = None
            hist(state, "reopened", "", self.ts())

        if state["status"] == "queued" and meta:
            ds, blocking = sched.deps_state(meta, self.dep_status)
            if ds == "never":
                state["status"] = "held"
                state["reason"] = "DependencyNeverSatisfied: " + ", ".join(f"#{b}" for b in blocking)
                hist(state, "held: a dependency failed or was cancelled", "", self.ts())
                replies.append(f"Held: dependency {', '.join(f'#{b}' for b in blocking)} will never complete. "
                               "A maintainer can `/unhold` after fixing the plan, or `/cancel`.")

        self.project_task(n, issue, state, meta)
        if replies:
            self.gh.create_comment(n, "\n\n".join(replies))
        self.save(n, issue, sc, before, state, meta)
        for fn in after_save:
            fn()

    def apply(self, n, issue, state, meta, c, cmd, after_save) -> Tuple[bool, str]:
        name, args, rest = cmd
        who = c["author"]
        st = state["status"]
        assoc = c.get("author_association")
        self.is_maintainer(who, assoc)  # primes the cache from the free association
        maint = self._maint.get(who, False)
        lease = state.get("lease") or {}
        holder = lease.get("login")

        if name == "claim":
            if st in ("review", "triage"):
                return self.claim_review(state, c, who, args, submitter=self._submitter(issue, state),
                                         body=issue["title"] + "\n" + issue["body"] if st == "triage" else None)
            if st != "queued":
                # A lost race or a stale board. The 👎 reaction is the answer;
                # clients confirm leases by comment id, so no reply is needed.
                return False, ""
            if meta is None:
                return False, f"@{who} #{n} has no valid task metadata; a maintainer must fix it."
            if self.blocked(who):
                return False, f"@{who} this account can't claim work here (Workstead safety net). Maintainers can review the listing."
            ds, blocking = sched.deps_state(meta, self.dep_status)
            if ds != "ready":
                return False, f"@{who} #{n} is waiting on {', '.join(f'#{b}' for b in blocking)} (Dependency)."
            why = self.claim_blocker(n, who, meta)
            if why:
                return False, f"@{who} {why}"
            limit = sched.time_limit_hours(meta, self.cfg)
            state["status"] = "running"
            state["reason"] = ""
            state["lease"] = {"login": who, "node": args.get("node", ""), "comment": c["id"],
                              "since": c["created_at"], "limit_h": limit}
            hist(state, f"leased (node {args.get('node') or '-'}, limit {limit} h)", who, c["created_at"])
            return True, ""

        if name == "release":
            rl = state.get("review_lease") or {}
            if rl.get("login") == who or (maint and rl and not holder):
                state["review_lease"] = None
                hist(state, "review lease released", who, c["created_at"])
                return True, ""
            if st != "running" or (holder != who and not maint):
                return False, f"@{who} you don't hold a lease on #{n}."
            self.keep_resume_point(state)
            state["status"] = "queued"
            state["lease"] = None
            state["reason"] = ""
            hist(state, f"released ({args.get('reason') or 'no reason'})", who, c["created_at"])
            return True, f"Released by @{who}. #{n} is open again."

        if name == "fail":
            if st != "running" or (holder != who and not maint):
                return False, f"@{who} you don't hold a lease on #{n}."
            return True, self.requeue(n, state, f"failed by @{who}: {args.get('reason') or 'no reason'}", who)

        if name == "done":
            if st != "running" or holder != who:
                return False, f"@{who} only the lease holder can submit #{n}."
            part = C.partition(self.cfg, (meta or {}).get("partition", ""))
            if part["deliver"] == "pr":
                return False, f"@{who} #{n} is delivered by a pull request that says `Closes #{n}`, not `/done`."
            if not rest or len(rest) > self.cfg["limits"]["result_chars"]:
                return False, f"@{who} `/done` needs the result below the first line (at most {self.cfg['limits']['result_chars']} characters)."
            state["submission"] = {"type": "comment", "ref": c["id"], "by": who}
            return True, self.submitted(n, state, meta, who, c["created_at"], after_save)

        if name == "plan":
            if st != "running" or holder != who:
                return False, f"@{who} only the lease holder can plan #{n}."
            if len(rest) > self.cfg["limits"]["plan_chars"]:
                return False, f"@{who} the plan is too long."
            doc = json_block(rest)
            tasks, errors = P.validate(doc, self.cfg, (meta or {}).get("depth", 0), self._existing_ok)
            if not errors:
                errors = self._job_budget_errors(n, meta, len(tasks))
            if errors:
                return False, f"@{who} the plan was not accepted:\n" + "\n".join(f"- {e}" for e in errors) + \
                    "\n\nFix it and post `/plan` again. See docs/planning.md."
            state["submission"] = {"type": "plan", "ref": c["id"], "by": who}
            if self.is_trusted(who):
                return True, self.materialize(n, issue, state, meta, tasks, c, who, after_save)
            state["status"] = "review"
            hist(state, "plan submitted for review (untrusted planner)", who, c["created_at"])
            return True, f"Plan from @{who} received ({len(tasks)} tasks). A trusted reviewer must `/approve` it before the tasks are created."

        if name == "approve":
            return self.review_verdict(n, issue, state, meta, c, who, True, after_save)
        if name == "request-changes":
            return self.review_verdict(n, issue, state, meta, c, who, False, after_save)

        if name in ("admit", "reject", "duplicate"):
            if st != "triage":
                return False, f"@{who} #{n} is not waiting for triage."
            rl = state.get("review_lease") or {}
            allowed = maint or (self.is_trusted(who) and who != issue["author"] and rl.get("login") in (None, who))
            if not allowed:
                return False, f"@{who} triage needs a maintainer, or a trusted worker other than the author who holds the triage lease."
            seen = rl.get("body")
            if name == "admit" and seen and seen != fingerprint(issue["title"] + "\n" + issue["body"]):
                state["review_lease"] = None
                return False, (f"@{who} the proposal was edited after you claimed the triage, so it wasn't admitted. "
                               "Claim it again and review the current text.")
            if name == "admit":
                if self.authored_by_trusted(issue):
                    m = self._meta_for_new(issue, read_block(issue["body"], "meta"), L["job"] in issue["labels"])
                    if args.get("priority"):
                        m["priority"] = int(args["priority"])
                    body = replace_block(issue["body"], "meta", m)
                    self.gh.edit_issue(n, body=body)
                    issue["body"] = body
                    if L["proposed"] in issue["labels"]:
                        self.gh.remove_label(n, L["proposed"])
                    state.update(status="queued", review_lease=None, reason="")
                    hist(state, "admitted", who, c["created_at"])
                    return True, f"Admitted by @{who}."
                pr = int(args["priority"]) if args.get("priority") else None
                new = self.admit_copy(issue, read_block(issue["body"], "meta"), L["job"] in issue["labels"], who, "triage", pr)
                state["status"] = "cancelled"
                state["review_lease"] = None
                state["reason"] = f"admitted as #{new}"
                hist(state, f"admitted as #{new}", who, c["created_at"])
                return True, f"Admitted by @{who} as #{new}. Continue there."
            if name == "reject":
                state["status"] = "cancelled"
                state["review_lease"] = None
                state["reason"] = f"rejected: {args.get('reason') or 'no reason'}"
                hist(state, "rejected", who, c["created_at"])
                return True, f"Rejected by @{who}: {args.get('reason') or 'no reason given'}."
            other = int(args["of"])
            state["status"] = "cancelled"
            state["review_lease"] = None
            state["reason"] = f"duplicate of #{other}"
            self.gh.add_labels(n, [L["duplicate"]])
            hist(state, f"duplicate of #{other}", who, c["created_at"])
            return True, f"Duplicate of #{other} (@{who})."

        # maintainer commands (scontrol)
        if not maint:
            return False, f"@{who} `/{name}` is for maintainers."
        if name == "hold":
            if st in CLOSED_STATES:
                return False, ""
            state.update(status="held", lease=None, review_lease=None, reason=f"Held: {args.get('reason') or 'by a maintainer'}")
            hist(state, "held", who, c["created_at"])
            return True, f"Held by @{who}."
        if name == "unhold":
            if st != "held":
                return False, f"#{n} is not held."
            state.update(status="queued", attempts=0, reason="", rounds=0)
            hist(state, "released from hold", who, c["created_at"])
            return True, f"Released from hold by @{who}."
        if name == "requeue":
            if st not in ("running", "review", "held"):
                return False, f"#{n} is {st}; nothing to requeue."
            state.update(status="queued", lease=None, review_lease=None, reason="", pr=None)
            hist(state, "requeued", who, c["created_at"])
            return True, f"Requeued by @{who}."
        if name == "cancel":
            self.finish(n, state, "cancelled", f"cancelled by @{who}: {args.get('reason') or ''}".strip(), after_save, meta)
            for child in state.get("children", []):
                after_save.append(lambda ch=child: self.dispatch(ch, "cancel"))
            return True, f"Cancelled by @{who}."
        if name == "priority":
            if meta is None:
                return False, "no task metadata"
            meta["priority"] = int(args["priority"])
            body = replace_block(issue["body"], "meta", meta)
            self.gh.edit_issue(n, body=body)
            issue["body"] = body
            hist(state, f"priority {meta['priority']}", who, c["created_at"])
            return True, ""
        return False, ""

    def _submitter(self, issue, state) -> str:
        sub = state.get("submission") or {}
        return sub.get("by") or issue["author"]

    def claim_review(self, state, c, who, args, submitter: str, body: Optional[str] = None) -> Tuple[bool, str]:
        if state.get("review_lease"):
            return False, ""  # lost race: reaction only
        if who == submitter:
            return False, f"@{who} you can't review your own submission."
        if not self.is_trusted(who):
            return False, f"@{who} reviews and triage count only from trusted workers (see docs/concepts.md#trust)."
        state["review_lease"] = {"login": who, "node": args.get("node", ""), "comment": c["id"], "since": c["created_at"],
                                 "limit_h": int(C.partition(self.cfg, "review")["max_hours"])}
        if body is not None:
            state["review_lease"]["body"] = fingerprint(body)
        hist(state, "review leased", who, c["created_at"])
        return True, ""

    def claim_blocker(self, n: int, who: str, meta: Dict[str, Any]) -> Optional[str]:
        if L["relay"] in self.gh.get_issue(n)["labels"]:
            return "this task is on the relay queue; agents claim it there (`ws next`)."
        held = self.gh.list_issues(labels=[L["running"]], assignee=who)
        held = [i for i in held if i["number"] != n]
        if len(held) >= self.cfg["leases"]["max_per_account"]:
            return (f"you already hold {len(held)} leases (the limit per account is "
                    f"{self.cfg['leases']['max_per_account']}). Finish or `/release` one first.")
        dr = self.cfg["drain"]
        if dr["expiries"] > 0:
            since = now_iso(self.now() - dr["window_hours"] * 3600)[:10]
            try:
                count = self.gh.search_count(f'repo:{self.gh.repo} "ws:expired @{who}" in:comments created:>{since}')
            except GitHubError:
                count = 0
            if count >= dr["expiries"]:
                return (f"{count} of your leases expired recently, so this account is drained (Slurm: DRAIN) "
                        f"for up to {dr['pause_hours']} hours. Make sure your agent sends heartbeats (`ws beat`).")
        if meta.get("licenses"):
            for i in self.gh.list_issues(labels=[L["running"]]):
                if i["number"] == n:
                    continue
                om = self.meta_of(i) or {}
                clash = set(om.get("licenses", [])) & set(meta["licenses"])
                if clash:
                    return f"#{i['number']} holds license {', '.join(sorted(clash))} (Licenses); try again when it finishes."
        return None

    def requeue(self, n: int, state: Dict[str, Any], why: str, who: str = "") -> str:
        state["attempts"] = state.get("attempts", 0) + 1
        self.keep_resume_point(state)
        state["lease"] = None
        state["pr"] = None
        if state["attempts"] >= self.cfg["leases"]["max_attempts"]:
            state["status"] = "held"
            state["reason"] = f"MaxAttempts: {why}"
            hist(state, f"held after {state['attempts']} attempts: {why}", who, self.ts())
            return f"{why}. That was attempt {state['attempts']} of {self.cfg['leases']['max_attempts']}, so #{n} is held for a maintainer (`/unhold` to retry)."
        state["status"] = "queued"
        state["reason"] = ""
        hist(state, f"requeued: {why}", who, self.ts())
        return f"{why}. #{n} is back in the queue (attempt {state['attempts']} of {self.cfg['leases']['max_attempts']})."

    def keep_resume_point(self, state: Dict[str, Any]) -> None:
        """Checkpoint/restart: remember where the last worker got to."""
        lease = state.get("lease") or {}
        cc = next((c for c in getattr(self, "_comments", []) if c["id"] == lease.get("comment")), None)
        ref = beat_ref(cc["body"]) if cc else None
        if ref or state.get("pr"):
            state["resume"] = {"ref": ref, "pr": state.get("pr"), "by": lease.get("login")}

    def submitted(self, n, state, meta, who, ts, after_save) -> str:
        part = C.partition(self.cfg, (meta or {}).get("partition", ""))
        if part.get("review") and self.cfg["review"]["approvals_required"] > 0:
            state["status"] = "review"
            state["approvals"] = []
            hist(state, "submitted for review", who, ts)
            return f"Result from @{who} received. A trusted reviewer can `/claim` and `/approve` or `/request-changes`."
        self.finish(n, state, "done", f"completed by @{who}", after_save, meta)
        return f"Completed by @{who}."

    def review_verdict(self, n, issue, state, meta, c, who, approve: bool, after_save) -> Tuple[bool, str]:
        if state["status"] != "review":
            return False, f"@{who} #{n} is not in review."
        rl = state.get("review_lease") or {}
        sub = state.get("submission") or {}
        if who == sub.get("by"):
            return False, f"@{who} you can't review your own submission."
        if not (self.is_maintainer(who) or (rl.get("login") == who and self.is_trusted(who))):
            return False, f"@{who} comment `/claim` first to take the review (trusted workers only)."
        state["review_lease"] = None
        if not approve:
            state["rounds"] = state.get("rounds", 0) + 1
            if state["rounds"] > self.cfg["review"]["max_rounds"]:
                state.update(status="held", reason="MaxReviewRounds")
                hist(state, "held: too many review rounds", who, c["created_at"])
                return True, f"Changes requested by @{who}, and that is round {state['rounds']}: held for a maintainer."
            if sub.get("type") == "plan" and not (state.get("lease") or {}).get("login"):
                state.update(status="queued")
            else:
                state["status"] = "running"
                if state.get("lease"):
                    state["lease"]["since"] = c["created_at"]
            hist(state, "changes requested", who, c["created_at"])
            return True, f"Changes requested by @{who}. @{sub.get('by', '')} please address them and submit again."
        approvals = {a["login"] for a in state.get("approvals", [])} | {who}
        state["approvals"] = [{"login": a} for a in sorted(approvals)]
        hist(state, "approved", who, c["created_at"])
        if len(approvals) < self.cfg["review"]["approvals_required"]:
            return True, f"Approved by @{who} ({len(approvals)} of {self.cfg['review']['approvals_required']})."
        if sub.get("type") == "plan":
            body = next((x["body"] for x in self.gh.list_comments(n) if x["id"] == sub["ref"]), "")
            parsed = parse_command(body)
            tasks, errors = P.validate(json_block(parsed[2] if parsed else ""), self.cfg,
                                       (meta or {}).get("depth", 0), self._existing_ok)
            if errors:
                state["status"] = "running"
                return True, "The approved plan no longer validates:\n" + "\n".join(f"- {e}" for e in errors)
            fake = {"id": sub["ref"], "created_at": c["created_at"]}
            return True, self.materialize(n, issue, state, meta, tasks, fake, sub["by"], after_save)
        self.finish(n, state, "done", f"approved by @{who}", after_save, meta)
        return True, f"Approved by @{who}. Done."

    def finish(self, n, state, status, why, after_save, meta) -> None:
        if state["status"] in CLOSED_STATES:
            return
        state["status"] = status
        state["reason"] = why if status != "done" else ""
        state["lease"] = None
        state["review_lease"] = None
        hist(state, why, "", self.ts())
        parent = (meta or {}).get("parent")
        after_save.append(lambda: self.after_close(n, parent))

    def after_close(self, n: int, parent: Optional[int]) -> None:
        if parent:
            self.dispatch(parent, f"child_closed:{n}")
        self.refresh_dependents(n)

    def refresh_dependents(self, n: int) -> None:
        """Flip pending -> ready on tasks that waited on n (labels only)."""
        self._issue_cache.pop(n, None)
        for i in self.gh.list_issues(labels=[L["pending"]]):
            m = self.meta_of(i)
            if not m or n not in m["deps"]:
                continue
            ds, _ = sched.deps_state(m, self.dep_status)
            if ds == "ready":
                self.gh.add_labels(i["number"], [L["ready"]])
                self.gh.remove_label(i["number"], L["pending"])
            elif ds == "never":
                self.dispatch(i["number"], "refresh")

    def _existing_ok(self, d: int) -> bool:
        return self.dep_status(d) in ("open", "done")

    def _job_budget_errors(self, n, meta, adding: int) -> List[str]:
        job = (meta or {}).get("job") or n
        cap = self.cfg["plans"]["max_tasks_per_job"]
        count = 0
        for i in self.gh.list_issues(labels=[L["task"]], state="all"):
            m = read_block(i["body"], "meta") or {}
            if m.get("job") == job:
                count += 1
        if count + adding > cap:
            return [f"job #{job} would have {count + adding} tasks; the limit is {cap} (plans.max_tasks_per_job)"]
        return []

    # --- plans ----------------------------------------------------------------

    def materialize(self, n, issue, state, meta, tasks, c, planner, after_save) -> str:
        meta = meta or clean_meta({}, self.cfg["partitions"])
        job = meta.get("job") or n
        ref = f"{n}/{c['id']}"
        made: Dict[str, int] = {}
        # Idempotency: children already created by an interrupted run.
        for i in self.gh.list_issues(labels=[L["task"]], state="all", since=c["created_at"]):
            m = read_block(i["body"], "meta") or {}
            pr = m.get("plan_ref") or ""
            if self.is_bot(i["author"]) and pr.startswith(ref + "/"):
                made[pr[len(ref) + 1:]] = i["number"]
        ids: Dict[int, int] = {}
        rows = []
        for t in tasks:
            if t["key"] in made:
                continue
            deps = sorted({made[d] for d in t["deps"]} | set(t["ext_deps"]))
            cm = {"partition": t["partition"], "priority": t["priority"], "deps": deps, "after": t["after"],
                  "parent": n, "job": job, "depth": meta.get("depth", 0) + 1, "est_hours": t["est_hours"],
                  "features": t["features"], "licenses": t["licenses"], "origin": "plan",
                  "plan_ref": f"{ref}/{t['key']}"}
            cm = clean_meta(cm, self.cfg["partitions"])
            ready = sched.deps_state(cm, self.dep_status)[0] == "ready"
            acc = "\n".join(f"- [ ] {a}" for a in t["acceptance"]) or "- [ ] The goal above is met and verified."
            split_hint = ""
            if t["partition"] != "plan" and t["est_hours"] > self.cfg["plans"]["split_above_hours"]:
                split_hint = (f"\n\n> This task is estimated at {t['est_hours']} h. Whoever claims it should "
                              "consider `/plan` to split it first.")
            body = (f"{t['body'].strip() or t['title']}\n\n### Acceptance criteria\n\n{acc}{split_hint}\n\n---\n"
                    f"<sub>Part of job #{job} · planned in #{n} by @{planner} · partition `{t['partition']}` · "
                    f"est {t['est_hours']} h" + (f" · after #{', #'.join(map(str, deps))}" if deps else "")
                    + f"</sub>\n\n{write_block('meta', cm)}\n")
            labels = [L["task"], partition_label(t["partition"]), L["ready"] if ready else L["pending"]]
            new = self.gh.create_issue(t["title"], body, labels)
            made[t["key"]] = new["number"]
            ids[new["number"]] = new["id"]
            self._issue_cache[new["number"]] = new
            self.gh.add_sub_issue(n, new["id"])
            for d in deps[:10]:
                did = ids.get(d) or (self._issue_cache.get(d) or {}).get("id")
                if did:
                    self.gh.add_blocked_by(new["number"], did)
            rows.append(f"| #{new['number']} | {t['title'][:70]} | `{t['partition']}` | {t['est_hours']} | "
                        + (", ".join(f"#{d}" for d in deps) or "-") + " |")
        children = [made[t["key"]] for t in tasks]
        state["children"] = sorted(set(state.get("children", [])) | set(children))
        state["status"] = "split"
        state["lease"] = None
        state["reason"] = ""
        hist(state, f"split into {len(children)} tasks", planner, c["created_at"])
        table = "| Task | Title | Partition | Est h | After |\n|---|---|---|---|---|\n" + "\n".join(rows)
        return (f"Plan from @{planner} accepted: #{n} is split into {len(children)} tasks. It completes when they all do."
                + (f"\n\n{table}" if rows else ""))

    # --- dispatched reasons -----------------------------------------------------

    def apply_reason(self, n, issue, state, meta, reason, after_save) -> str:
        kind, _, arg = reason.partition(":")
        st = state["status"]
        if kind == "expire":
            return self.check_expiry(n, state, after_save)
        if kind in ("child_closed", "child_added", "refresh") and st == "split":
            return self.rollup(n, state, after_save, meta)
        if kind == "cancel" and st not in CLOSED_STATES:
            self.finish(n, state, "cancelled", "parent cancelled", after_save, meta)
            for child in state.get("children", []):
                after_save.append(lambda ch=child: self.dispatch(ch, "cancel"))
            return "Cancelled because its parent was cancelled."
        if kind.startswith("pr_") or kind in ("merged", "changes", "conflict", "checks_failed"):
            return self.task_pr_event(n, issue, state, meta, kind, int(arg) if arg.isdigit() else 0, after_save)
        return ""

    def check_expiry(self, n, state, after_save) -> str:
        now = self.now()
        hb = self.cfg["leases"]["heartbeat_minutes"] * 60
        msgs = []
        for key in ("lease", "review_lease"):
            lease = state.get(key)
            if not lease:
                continue
            if key == "lease" and state["status"] != "running":
                continue
            comments = {c["id"]: c for c in getattr(self, "_comments", None) or self.gh.list_comments(n)}
            cc = comments.get(lease.get("comment"))
            last = max(parse_ts(lease["since"]), parse_ts(cc["updated_at"]) if cc else 0)
            limit_s = float(lease.get("limit_h") or 6) * 3600
            why = ""
            if now - last > hb:
                why = f"went {self.cfg['leases']['heartbeat_minutes']} minutes without a heartbeat"
            elif key == "lease" and now - parse_ts(lease["since"]) > limit_s:
                why = f"passed its {lease.get('limit_h')}-hour time limit"
            if not why:
                continue
            who = lease["login"]
            if key == "review_lease":
                state["review_lease"] = None
                hist(state, f"review lease expired: {why}", who, self.ts())
                msgs.append(f"The review lease held by @{who} expired: it {why}. <!-- ws:expired @{who} -->")
            else:
                msgs.append(self.requeue(n, state, f"The lease held by @{who} expired: it {why}", who)
                            + f" <!-- ws:expired @{who} -->")
        return "\n\n".join(msgs)

    def rollup(self, n, state, after_save, meta) -> str:
        kids = set(state.get("children", []))
        # One scan of open tasks: any open child (planned or attached later,
        # such as a found issue) means not finished. O(open/100) calls.
        for i in self.gh.list_issues(labels=[L["task"]]):
            m = self.meta_of(i)
            if m and m.get("parent") == n:
                kids.add(i["number"])
                state["children"] = sorted(kids)
                return ""
        state["children"] = sorted(kids)
        statuses = {k: self.dep_status(k) for k in state["children"]}
        if not statuses or any(s == "open" for s in statuses.values()):
            return ""
        bad = [k for k, s in statuses.items() if s in ("failed", "cancelled", "missing")]
        if bad:
            if state["status"] != "held":
                state.update(status="held", reason="ChildNotCompleted: " + ", ".join(f"#{b}" for b in bad))
                hist(state, "held: some children did not complete", "", self.ts())
                return (f"All child tasks are closed, but {', '.join(f'#{b}' for b in bad)} did not complete. "
                        "A maintainer can `/unhold` this to plan the rest again, or close it.")
            return ""
        self.finish(n, state, "done", "all child tasks completed", after_save, meta)
        return f"All {len(statuses)} child tasks completed, so #{n} is done."

    def task_pr_event(self, n, issue, state, meta, kind, pr_n, after_save) -> str:
        if not pr_n:
            return ""
        pr = self.gh.get_pr(pr_n)
        lease = state.get("lease") or {}
        if kind == "merged" or (pr["merged"] and kind in ("pr_closed", "pr_opened")):
            if state["status"] in CLOSED_STATES:
                return ""
            self.finish(n, state, "done", f"merged in #{pr_n}", after_save, meta)
            return f"Merged in #{pr_n}. Done."
        if kind == "pr_opened":
            if state["status"] not in ("running", "review") or lease.get("login") != pr["author"]:
                return ""
            state["pr"] = pr_n
            state["submission"] = {"type": "pr", "ref": pr_n, "by": pr["author"]}
            if state["status"] != "review":
                state["status"] = "review"
                hist(state, f"pull request #{pr_n}", pr["author"], self.ts())
            return ""
        if kind == "pr_closed":
            if state.get("pr") == pr_n and state["status"] == "review":
                state["pr"] = None
                state["status"] = "running"
                if state.get("lease"):
                    state["lease"]["since"] = self.ts()
                hist(state, f"pull request #{pr_n} closed unmerged", "", self.ts())
                return f"#{pr_n} was closed without merging. @{lease.get('login')} still holds the lease: open a new pull request or `/release`."
            return ""
        if kind in ("changes", "conflict", "checks_failed"):
            if state.get("pr") != pr_n or state["status"] != "review":
                return ""
            state["rounds"] = state.get("rounds", 0) + 1
            if state["rounds"] > self.cfg["review"]["max_rounds"]:
                state.update(status="held", reason="MaxReviewRounds")
                hist(state, "held: too many review rounds", "", self.ts())
                return f"#{pr_n} needed rework {state['rounds']} times: held for a maintainer."
            state["status"] = "running"
            if state.get("lease"):
                state["lease"]["since"] = self.ts()
            what = {"changes": "Changes were requested", "conflict": "It has merge conflicts",
                    "checks_failed": "Its checks failed"}[kind]
            hist(state, f"rework: {what.lower()}", "", self.ts())
            return f"@{lease.get('login')} #{pr_n} needs rework: {what}. Your lease continues; push fixes to the same branch."
        return ""

    # --- pull requests -------------------------------------------------------

    def reconcile_pr(self, n: int, reason: str = "") -> None:
        # Steady state costs two reads: the pull request and its comments.
        pr = self.gh.get_pr(n)
        comments = self.gh.list_comments(n)
        sc, state = None, None
        for c in comments:
            if self.is_bot(c["author"]):
                st = read_block(c["body"], "state")
                if st is not None:
                    sc, state = c, st
                    break
        state = state or new_state("pr")
        issue = {"labels": list(pr["labels"]), "assignees": list(pr["assignees"]), "state": pr["state"]}
        before = json.dumps(state, sort_keys=True) if sc else ""
        replies: List[str] = []
        t = state.get("task")
        if t is None:
            t = linked_task(pr["body"])
            if not t or not self.link_pr(n, pr, issue, state, t, replies):
                if replies:
                    self.gh.create_comment(n, "\n\n".join(replies))
                return
        if reason == "pr_synchronize" and state.get("approvals"):
            state["approvals"] = [a for a in state["approvals"] if a.get("sha") == pr["head_sha"]]
            hist(state, "new commits; earlier approvals are stale", pr["author"], self.ts())
        if reason == "pr_synchronize":
            # New commits get the same scrutiny as the first: a pull request
            # that later adds a dependency or touches CI needs a maintainer.
            why = self.needs_maintainer(n, pr)
            if why and L["needs_maintainer"] not in issue["labels"]:
                self.gh.add_labels(n, [L["needs_maintainer"]])
                issue["labels"].append(L["needs_maintainer"])
                replies.append(f"New commits mean this pull request {why}, so a maintainer must now `/approve` it.")
            # Any push after rework (review changes, failed checks, conflicts)
            # puts the task back in review. Idempotent on the task side.
            state["reason"] = f"{why}: a maintainer must approve" if why else ""
            self.dispatch(t, f"pr_opened:{n}")
            if L["changes"] in issue["labels"]:
                self.gh.remove_label(n, L["changes"])
                issue["labels"].remove(L["changes"])
            if L["approved"] in issue["labels"] and not state.get("approvals"):
                self.gh.remove_label(n, L["approved"])
                issue["labels"].remove(L["approved"])
            if L["needs_review"] not in issue["labels"] and self._needs_review(None, state):
                self.gh.add_labels(n, [L["needs_review"]])
                issue["labels"].append(L["needs_review"])
        if reason == "pr_closed":
            self.dispatch(t, ("merged:" if pr["merged"] else "pr_closed:") + str(n))
            state["review_lease"] = None

        for c in comments:
            if c["id"] <= state.get("cursor", 0) or self.is_bot(c["author"]):
                continue
            state["cursor"] = c["id"]
            cmd = parse_command(c["body"])
            if cmd is None:
                continue
            ok, msg = self.apply_pr(n, pr, issue, state, c, cmd, t)
            if ok or msg:
                self.gh.react(c["id"], "+1" if ok else "-1")
            if msg:
                replies.append(msg)

        if reason == "expire":
            msg = self.check_expiry_pr(n, state)
            if msg:
                replies.append(msg)

        if pr["state"] == "open" and L["pr"] in issue["labels"]:
            msg = self.try_merge(n, pr, issue, state, None, t)
            if msg:
                replies.append(msg)
        people = {state["review_lease"]["login"]} if state.get("review_lease") else set()
        cur = set(issue["assignees"])
        if cur - people:
            self.gh.remove_assignees(n, sorted(cur - people))
        if people - cur:
            self.gh.add_assignees(n, sorted(people - cur))
        if replies:
            self.gh.create_comment(n, "\n\n".join(replies))
        self.save(n, issue, sc, before, state, None)

    def link_pr(self, n, pr, issue, state, t, replies) -> bool:
        """First sight of a pull request: is it a lease holder's work on a task?"""
        tissue = self._safe_issue(t)
        managed = bool(tissue and L["task"] in tissue["labels"] and not tissue["is_pr"] and self.authored_by_trusted(tissue))
        if not managed:
            if pr["head_ref"] and pr["head_ref"].startswith("ws/") and L["unlinked"] not in issue["labels"] and pr["state"] == "open":
                self.gh.add_labels(n, [L["unlinked"]])
                replies.append("This branch looks like workstead work, but the pull request doesn't say `Closes #N` "
                               "for a task. Add it to the description, or open it with `ws submit`.")
            return False
        if pr["state"] != "open":
            return False
        _, _, _, tstate = self.load(t)
        tlease = (tstate.get("lease") or {}).get("login")
        if tlease != pr["author"]:
            if L["unlinked"] not in issue["labels"]:
                self.gh.add_labels(n, [L["unlinked"]])
                holder = f"@{tlease} holds it" if tlease else "it isn't leased"
                replies.append(f"@{pr['author']} #{t} is a workstead task and {holder}. Claim a task (`/claim`) before "
                               "working on it, so that no work is duplicated. This pull request won't be merged by the fleet.")
            return False
        if L["unlinked"] in issue["labels"]:
            self.gh.remove_label(n, L["unlinked"])
            issue["labels"].remove(L["unlinked"])
        if tstate.get("pr") and tstate["pr"] != n and self.gh.get_pr(tstate["pr"])["state"] == "open":
            replies.append(f"#{t} already has pull request #{tstate['pr']}. Push to that branch instead.")
            return False
        tmeta = self.meta_of(tissue) or {}
        part = C.partition(self.cfg, tmeta.get("partition", ""))
        state["task"] = t
        state["review_required"] = self.cfg["review"]["approvals_required"] if part.get("review") else 0
        labels = [L["pr"]]
        why = self.needs_maintainer(n, pr)
        if why:
            labels.append(L["needs_maintainer"])
            state["reason"] = f"{why}: a maintainer must approve"
            replies.append(f"This pull request {why}, so a maintainer must also `/approve` it before the fleet merges it.")
        if state["review_required"]:
            labels.append(L["needs_review"])
        self.gh.add_labels(n, labels)
        issue["labels"] += labels
        hist(state, f"linked to #{t}", pr["author"], self.ts())
        self.dispatch(t, f"pr_opened:{n}", "task")
        return True

    def _needs_review(self, tmeta, state) -> bool:
        need = state.get("review_required")
        if need is None:
            part = C.partition(self.cfg, (tmeta or {}).get("partition", ""))
            need = self.cfg["review"]["approvals_required"] if part.get("review") else 0
        return len(state.get("approvals", [])) < need

    def needs_maintainer(self, n: int, pr: Dict[str, Any]) -> str:
        """Why a pull request needs a maintainer's approval, or "" if it doesn't."""
        import fnmatch
        rv = self.cfg["review"]
        fl = self.flagged(pr.get("author", ""))
        if fl:
            return "is from an account flagged by the Workstead safety net (" + ", ".join(fl.get("signals", [])) + ")"
        try:
            files = self.gh.pr_files(n)
        except GitHubError:
            return "couldn't be inspected"
        prefixes = rv["protected_paths"]
        hit = [f for f in files if any(f == p or (p.endswith("/") and f.startswith(p)) for p in prefixes)]
        if hit:
            return f"touches protected paths ({', '.join(hit[:3])})"
        pats = rv["maintainer_patterns"]
        hit = [f for f in files if any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], p) for p in pats)]
        if hit:
            return f"changes dependencies, build or binary files ({', '.join(hit[:3])})"
        if len(files) > rv["maintainer_if_files_over"] or \
                pr.get("additions", 0) + pr.get("deletions", 0) > rv["maintainer_if_lines_over"]:
            return "is too large for an agent review"
        return ""

    def protected(self, n: int) -> bool:
        return bool(self.needs_maintainer(n, self.gh.get_pr(n)))

    def apply_pr(self, n, pr, issue, state, c, cmd, t) -> Tuple[bool, str]:
        name, args, rest = cmd
        who = c["author"]
        if pr["state"] != "open":
            return False, ""
        if name == "claim":
            if L["needs_review"] not in issue["labels"]:
                return False, ""
            return self.claim_review(state, c, who, args, submitter=pr["author"])
        if name == "release":
            if (state.get("review_lease") or {}).get("login") == who:
                state["review_lease"] = None
                hist(state, "review lease released", who, c["created_at"])
                return True, ""
            return False, ""
        if name in ("approve", "request-changes"):
            rl = (state.get("review_lease") or {}).get("login")
            maint = self.is_maintainer(who, c.get("author_association"))
            if who == pr["author"]:
                return False, f"@{who} you can't review your own pull request."
            if not (maint or (rl == who and self.is_trusted(who))):
                return False, f"@{who} comment `/claim` first to take the review (trusted workers only)."
            state["review_lease"] = None
            if name == "approve":
                appr = [a for a in state.get("approvals", []) if a["login"] != who]
                appr.append({"login": who, "sha": pr["head_sha"], "maintainer": maint})
                state["approvals"] = appr
                hist(state, f"approved {pr['head_sha'][:7]}", who, c["created_at"])
                if not self._needs_review(None, state):
                    for x in (L["needs_review"], L["changes"]):
                        if x in issue["labels"]:
                            self.gh.remove_label(n, x)
                            issue["labels"].remove(x)
                    if L["approved"] not in issue["labels"]:
                        self.gh.add_labels(n, [L["approved"]])
                        issue["labels"].append(L["approved"])
                return True, ""
            state["rounds"] = state.get("rounds", 0) + 1
            state["approvals"] = []
            for x in (L["needs_review"], L["approved"]):
                if x in issue["labels"]:
                    self.gh.remove_label(n, x)
                    issue["labels"].remove(x)
            if L["changes"] not in issue["labels"]:
                self.gh.add_labels(n, [L["changes"]])
                issue["labels"].append(L["changes"])
            hist(state, "changes requested", who, c["created_at"])
            self.dispatch(t, f"changes:{n}")
            return True, f"@{pr['author']} changes requested by @{who}. Push fixes to this branch; review restarts automatically."
        return False, ""

    def check_expiry_pr(self, n, state) -> str:
        lease = state.get("review_lease")
        if not lease:
            return ""
        hb = self.cfg["leases"]["heartbeat_minutes"] * 60
        cc = next((c for c in self.gh.list_comments(n) if c["id"] == lease.get("comment")), None)
        last = max(parse_ts(lease["since"]), parse_ts(cc["updated_at"]) if cc else 0)
        if self.now() - last > hb or self.now() - parse_ts(lease["since"]) > float(lease.get("limit_h", 2)) * 3600:
            state["review_lease"] = None
            hist(state, "review lease expired", lease["login"], self.ts())
            return f"The review lease held by @{lease['login']} expired. <!-- ws:expired @{lease['login']} -->"
        return ""

    def try_merge(self, n, pr, issue, state, tmeta, t) -> str:
        if pr["draft"]:
            return ""
        approvals = [a for a in state.get("approvals", []) if a.get("sha") == pr["head_sha"]]
        if L["needs_maintainer"] in issue["labels"] and not any(a.get("maintainer") for a in approvals):
            # A maintainer may approve in GitHub's own review UI (which branch
            # protection's code-owner rule also needs) instead of with /approve.
            try:
                for rv in self.gh.pr_reviews(n):
                    if rv["state"] == "APPROVED" and rv["sha"] == pr["head_sha"] and rv["login"] != pr["author"] \
                            and self.is_maintainer(rv["login"]):
                        approvals.append({"login": rv["login"], "sha": rv["sha"], "maintainer": True})
                        break
            except GitHubError:
                pass  # reviews unavailable: fall back to /approve comments only
        if self._needs_review(tmeta, {"approvals": approvals, "review_required": state.get("review_required")}):
            return ""
        if L["needs_maintainer"] in issue["labels"] and not any(a.get("maintainer") for a in approvals):
            if state.get("reason") != "waiting for a maintainer's approval (protected paths)":
                state["reason"] = "waiting for a maintainer's approval (protected paths)"
                return "Approved by the fleet, but this touches protected paths, so a maintainer must also `/approve`."
            return ""
        if pr["mergeable"] is False:
            if state.get("reason") != "conflict":
                state["reason"] = "conflict"
                self.dispatch(t, f"conflict:{n}")
            return ""
        checks = "success"
        if self.cfg["review"]["require_checks"]:
            checks = self.gh.checks_state(pr["head_sha"])
            if checks == "none":
                checks = "pending"  # CI hasn't reported yet; never merge blind
        if checks == "pending":
            state["reason"] = "waiting for checks"
            return ""
        if checks == "failure":
            if state.get("reason") != f"checks failed on {pr['head_sha'][:7]}":
                state["reason"] = f"checks failed on {pr['head_sha'][:7]}"
                self.dispatch(t, f"checks_failed:{n}")
                return f"@{pr['author']} checks failed on {pr['head_sha'][:7]}. Fix them by pushing to this branch."
            return ""
        if self.cfg["review"]["merge"] == "maintainer":
            if L["ready_to_merge"] not in issue["labels"]:
                self.gh.add_labels(n, [L["ready_to_merge"]])
                issue["labels"].append(L["ready_to_merge"])
                state["reason"] = "approved and green; waiting for a maintainer to merge"
            return ""
        if self.gh.merge_pr(n, pr["head_sha"], self.cfg["review"]["merge_method"]):
            hist(state, f"merged {pr['head_sha'][:7]}", "", self.ts())
            state["reason"] = "merged"
            state["review_lease"] = None
            self.dispatch(t, f"merged:{n}")
            return ""
        state["reason"] = "merge refused by GitHub; will retry"
        return ""

    # --- the sweep (cron) -----------------------------------------------------

    def sweep(self, publish: bool = True) -> Dict[str, Any]:
        """Expire leases, repair labels, retry merges, roll up parents, publish the board."""
        from .board import build_snapshot, render_board
        now = self.now()
        prev = {}
        try:
            raw = self.gh.read_snapshot(self.cfg["board"]["branch"], "queue.json", public=False)
            prev = json.loads(raw) if raw else {}
        except (ValueError, GitHubError):
            prev = {}
        cache = {int(k): v for k, v in (prev.get("cache") or {}).items()}
        prev_cache = {n: dict(v) for n, v in cache.items()}
        learn = prev.get("learn") or LRN.new_state()
        tasks = self.gh.list_issues(labels=[L["task"]])
        prs = [i for i in self.gh.list_issues(labels=[L["pr"]], include_prs=True) if i["is_pr"]]
        items: Dict[int, Dict[str, Any]] = {}
        for i in tasks + prs:
            n = i["number"]
            c = cache.get(n)
            hot_states = ("running", "review", "split", "triage", "held")
            hot = i["is_pr"] or bool(set(i["labels"]) & {L[x] for x in hot_states})
            # Leaving a hot state (a requeue, say): read it once more to see why.
            hot = hot or bool(c and c.get("status") in hot_states)
            # Fresh only if both the timestamp and the labels match: updated_at has
            # one-second resolution, so a claim in the same second as the last
            # sweep would otherwise leave a stale status cached.
            labels_now = sorted(i["labels"])
            if not hot:
                c = {"updated_at": i["updated_at"], "labels": labels_now,
                     "status": status_from_labels(i["labels"]) or "queued", "attempts": (c or {}).get("attempts", 0)}
            elif c is None or c.get("updated_at") != i["updated_at"] or c.get("labels") != labels_now:
                if not self.gh.budget_ok():
                    c = c or {}
                else:
                    _, _, _, st = self.load(n)
                    c = {"updated_at": i["updated_at"], "labels": labels_now,
                         "lease": st.get("lease"), "review_lease": st.get("review_lease"),
                         "status": st.get("status"), "children": st.get("children", []), "pr": st.get("pr"),
                         "attempts": st.get("attempts", 0), "reason": st.get("reason", ""),
                         "approvals": len(st.get("approvals", []))}
            cache[n] = c
            items[n] = i
        cache = {n: v for n, v in cache.items() if n in items}

        window = self.cfg["leases"]["heartbeat_minutes"] * 60
        alive = set()
        for c in self.gh.repo_comments_since(now_iso(now - window)):
            alive.add((c["issue"], c["author"]))
        for n, c in cache.items():
            for key in ("lease", "review_lease"):
                lease = c.get(key)
                if not lease:
                    continue
                if key == "lease" and c.get("status") != "running":
                    continue
                stale = (n, lease["login"]) not in alive and now - parse_ts(lease["since"]) > window
                over = key == "lease" and now - parse_ts(lease["since"]) > float(lease.get("limit_h") or 6) * 3600
                if stale or over:
                    self.dispatch(n, "expire")
                    break

        # Repair projections and catch missed transitions.
        for n, i in items.items():
            if i["is_pr"]:
                if (L["approved"] in i["labels"] or L["needs_maintainer"] in i["labels"]) \
                        and L["ready_to_merge"] not in i["labels"]:
                    self.dispatch(n, "checks", "pr")
                continue
            st = cache[n].get("status")
            meta = self.meta_of(i)
            labels = set(i["labels"])
            if st == "split":
                kids = cache[n].get("children", [])
                if kids and all(k not in items for k in kids):
                    self.dispatch(n, "refresh")
                continue
            if st == "queued" and meta:
                ds, _ = sched.deps_state(meta, lambda d: "open" if d in items and not items[d]["is_pr"] else self.dep_status(d))
                if ds == "never":
                    self.dispatch(n, "refresh")
                    continue
                want = L["ready"] if ds == "ready" else L["pending"]
                drop = (L["pending"] if want == L["ready"] else L["ready"])
                if want not in labels:
                    self.gh.add_labels(n, [want])
                if drop in labels:
                    self.gh.remove_label(n, drop)
            elif st in ("running", "review", "held", "triage"):
                extra = labels & {L["ready"], L["pending"]}
                for x in extra:
                    self.gh.remove_label(n, x)
                if status_label(st) not in labels:
                    self.gh.add_labels(n, [status_label(st)])
        snap = build_snapshot(self, items, cache, now)
        snap["cache"] = {str(n): v for n, v in cache.items()}
        extra = self.learn(snap, learn, prev, prev_cache, cache, items, now)
        if publish:
            self.publish(snap, render_board(snap, self.cfg), extra)
        return snap

    # --- fleet learnings ---------------------------------------------------------

    def learn(self, snap, learn, prev, prev_cache, cache, items, now) -> Dict[str, str]:
        """Count this sweep's transitions; build the fleet report; aggregate upstream."""
        from . import __version__
        since = prev.get("generated_at")
        done = 0
        if since:
            try:
                done = sum(1 for d in self.gh.list_issues(labels=[L["done"]], state="closed", since=since)
                           if parse_ts(d.get("closed_at")) > parse_ts(since))
            except GitHubError:
                done = 0
        metas = {n: (self.meta_of(i) or {}) for n, i in items.items() if not i["is_pr"]}
        nodes = (len(snap["nodes"]), sum(len(x["nodes"]) or 1 for x in snap["nodes"]))
        if since:  # the first sweep has nothing to compare with
            LRN.observe(learn, prev_cache, cache, items, metas, now, done, nodes)
        snap["learn"] = learn
        files = {}
        try:
            rep = LRN.report(learn, self.cfg, __version__, bool(self._private))
            files["learnings.json"] = json.dumps(rep, indent=1, sort_keys=True)
            snap["learnings_topics"] = rep["topics"]
        except ValueError as e:
            log(str(e))
        safety = self.safety_snapshot(now)
        if safety is not None:
            files["safety.json"] = json.dumps(safety, indent=1, sort_keys=True)
        agg = self.aggregate_learnings(now)
        if agg is not None:
            files["fleet-learnings.json"] = json.dumps(agg, indent=1, sort_keys=True)
            snap["fleet_learnings"] = {"fleets": agg["fleets"], "signals": agg.get("signals", [])}
        return files

    # --- the safety net (upstream side) -----------------------------------------

    def _safety_state(self, n: int):
        comments = self.gh.list_comments(n)
        for c in comments:
            if self.is_bot(c["author"]):
                st = read_block(c["body"], "state")
                if st is not None and st.get("kind") == "safety":
                    return comments, c, st
        return comments, None, SAFE.new_state()

    def ingest_safety(self, n: int) -> None:
        """Verify flags, maintainer confirmations, clears and appeals, in comment order."""
        issue = self.gh.get_issue(n)
        comments, sc, state = self._safety_state(n)
        before = json.dumps(state, sort_keys=True) if sc else ""
        replies: List[str] = []
        now = self.now()
        for c in comments:
            if c["id"] <= state.get("cursor", 0) or self.is_bot(c["author"]):
                continue
            state["cursor"] = c["id"]
            who = c["author"]
            maint = self.is_maintainer(who, c.get("author_association"))
            cmd = parse_command(c["body"])
            name = cmd[0] if cmd else None
            if name == "flag":
                flag, errors = SAFE.validate_flag(json_block(cmd[2]))
                why = "; ".join(errors) if errors else SAFE.verify(flag, who, self.gh, self.is_maintainer)
                if why:
                    self.gh.delete_comment(c["id"])
                    replies.append(f"@{who} a flag was not accepted: {why}.")
                    continue
                state["flags"] = [f for f in state["flags"]
                                  if not (f["subject"].lower() == flag["subject"].lower() and f["project"].lower() == flag["project"].lower())]
                state["flags"].append(dict(flag, reporter=who, at=c["created_at"], comment=c["id"]))
                self.gh.react(c["id"], "+1")
            elif name in ("confirm", "clear") and maint:
                key = cmd[1]["login"].lower()
                state["confirmed" if name == "confirm" else "cleared"][key] = {"by": who, "at": c["created_at"]}
                if name == "clear":
                    state["appeals"].pop(key, None)
                self.gh.react(c["id"], "+1")
            elif name == "appeal":
                state["appeals"][who.lower()] = c["created_at"]
                self.gh.react(c["id"], "eyes")
            elif not maint:
                self.gh.delete_comment(c["id"])  # no free-text accusations or noise here
        listed = SAFE.listing(state, now)
        body = SAFE.render_issue(state, listed, now)
        if issue["body"] != body:
            self.gh.edit_issue(n, body=body)
        if replies:
            self.gh.create_comment(n, "\n\n".join(replies))
        if json.dumps(state, sort_keys=True) != before or sc is None:
            text = "Workstead safety-net record (maintained by the queue).\n\n" + write_block("state", state)
            if sc is None:
                self.gh.create_comment(n, text)
            else:
                self.gh.edit_comment(sc["id"], text)

    def safety_snapshot(self, now: float) -> Optional[Dict[str, Any]]:
        issues = self.gh.list_issues(labels=[L["safety"]])
        if not issues:
            return None
        _, _, state = self._safety_state(issues[0]["number"])
        return SAFE.snapshot(SAFE.listing(state, now), now)

    def ingest_learnings(self, n: int, comment: Dict[str, Any]) -> None:
        """A downstream fleet's report: keep it if it matches the schema exactly."""
        body = comment.get("body") or ""
        cmd = parse_command(body)
        ok = False
        if cmd and cmd[0] == "learnings" and len(body) < 20000:
            _, errors = LRN.validate(json_block(cmd[2]))
            ok = not errors
        cid = comment["id"]
        if ok:
            self.gh.react(cid, "+1")
        else:
            self.gh.delete_comment(cid)  # not a valid report: never keep free text here

    def aggregate_learnings(self, now: float) -> Optional[Dict[str, Any]]:
        """Upstream only: combine downstream reports and open tasks for shared problems."""
        issues = self.gh.list_issues(labels=[L["learnings"]])
        if not issues:
            return None
        n = issues[0]["number"]
        reports, keep = [], {}
        for c in self.gh.list_comments(n):
            if self.is_bot(c["author"]):
                continue
            cmd = parse_command(c["body"])
            rep, errors = LRN.validate(json_block(cmd[2])) if cmd and cmd[0] == "learnings" else ({}, ["not a report"])
            if errors or now - parse_ts(rep["generated_at"]) > 90 * 86400:
                self.gh.delete_comment(c["id"])
                continue
            prev = keep.get(rep["project"])
            if prev and prev[1]["generated_at"] >= rep["generated_at"]:
                self.gh.delete_comment(c["id"])  # superseded: one report per fleet
                continue
            if prev:
                self.gh.delete_comment(prev[0])
            keep[rep["project"]] = (c["id"], rep)
            reports.append(rep)
        agg = LRN.aggregate([r for _, r in keep.values()], now)
        agg["signals"] = LRN.fleet_signals(agg)
        opened = []
        if agg["signals"]:
            recent = self.gh.list_issues(labels=[L["task"]], state="all", since=now_iso(now - 30 * 86400))
            refs = {(read_block(i["body"], "meta") or {}).get("plan_ref") for i in recent if self.is_bot(i["author"])}
            for sig in agg["signals"]:
                ref = f"learnings/{sig['topic']}"
                if ref in refs:
                    continue
                meta = clean_meta({"partition": "research", "priority": min(9, 5 + sig["fleets"] // 5), "est_hours": 2,
                                   "topics": [sig["topic"]], "origin": "manual", "plan_ref": ref}, self.cfg["partitions"])
                title = f"Fleet learnings: {sig['title'].lower()}"
                self.gh.create_issue(title, LRN.signal_task_body(sig, agg) + "\n" + write_block("meta", meta) + "\n",
                                     [L["task"], partition_label("research"), L["ready"]])
                opened.append(sig["topic"])
        agg["opened"] = opened
        body = render_learnings_issue(agg)
        if issues[0]["body"] != body:
            self.gh.edit_issue(n, body=body)
        return agg

    def publish(self, snap: Dict[str, Any], board_md: str, extra: Optional[Dict[str, str]] = None) -> None:
        try:
            files = {"queue.json": json.dumps(snap, indent=1, sort_keys=True), "BOARD.md": board_md}
            files.update(extra or {})
            self.gh.publish_snapshot(self.cfg["board"]["branch"], files, f"workstead board {snap['generated_at']}")
        except GitHubError as e:
            log(f"publishing the board branch failed: {e}")
        boards = self.gh.list_issues(labels=[L["board"]])
        body = board_md + "\n\n<sub>Updated by the workstead sweep. Don't edit; it is rewritten.</sub>"
        if boards:
            if boards[0]["body"] != body:
                self.gh.edit_issue(boards[0]["number"], body=body)
        else:
            b = self.gh.create_issue(self.cfg["board"]["title"], body, [L["board"]])
            if b.get("node_id"):
                self.gh.pin_issue(b["node_id"])

    # --- one-time setup ---------------------------------------------------------

    def setup(self) -> List[str]:
        out = []
        for name, (color, desc) in LABEL_INFO.items():
            out.append(f"{self.gh.ensure_label(name, color, desc)} {name}")
        for p, pc in self.cfg["partitions"].items():
            out.append(f"{self.gh.ensure_label(partition_label(p), 'ededed', pc.get('about', 'partition')[:100])} {partition_label(p)}")
        return out


LEARNINGS_INTRO = """## Fleet learnings

Workstead fleets that give back also send anonymous, aggregate statistics about how the queue works for them:
lease expiry rates, estimate accuracy, why work is reworked, what gets held. Reports contain only numbers and
fixed categories (see `tools/ws/workstead/learnings.py`); anything else is deleted. One report per fleet is kept.
When a problem is common across fleets, the queue opens a task here so give-back time goes where it helps most.
"""


def render_learnings_issue(agg: Dict[str, Any]) -> str:
    rows = "\n".join(f"| `{k}` | {v['median']} | {v['p75']} | {v['n']} |" for k, v in sorted(agg["metrics"].items()))
    sigs = "\n".join(f"- **{s['topic']}**: {s['title']} (median `{s['metric']}` {s['median']} across {s['fleets']} fleets)"
                      for s in agg.get("signals", [])) or "- none yet"
    return (f"{LEARNINGS_INTRO}\n### {agg['fleets']} fleets reporting\n\n**Signals**\n\n{sigs}\n\n"
            + ("| Metric | Median | p75 | Fleets |\n|---|---|---|---|\n" + rows + "\n" if rows else "")
            + "\n<sub>Updated by the workstead sweep.</sub>")


def fingerprint(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def form_fields(body: str) -> Dict[str, Any]:
    """Fields from a GitHub issue form (rendered as '### Label' headings)."""
    out: Dict[str, Any] = {}
    for label, key, rx in (("Partition", "partition", r"[a-z][a-z0-9_-]*"), ("Priority", "priority", r"[0-9]"),
                           ("Estimate", "est_hours", r"[0-9]+(?:\.[0-9]+)?")):
        m = re.search(rf"^###\s*{label}[^\n]*\n+\s*({rx})\b", body or "", re.M | re.I)
        if m:
            v = m.group(1)
            out[key] = int(v) if key == "priority" else float(v) if key == "est_hours" else v.lower()
    return out


def render_note(lines: List[str]) -> str:
    return "\n\n".join(lines)

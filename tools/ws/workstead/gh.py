"""GitHub transport: a small REST client with no dependencies.

Everything the queue and the worker need from GitHub goes through ``GitHub``.
``fakegh.FakeGitHub`` implements the same methods in memory, which is how the
engine is tested and how the fleet simulator runs. Objects are normalised to
plain dicts (see ``_issue``, ``_comment`` and ``_pr``) so both behave alike.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
UA = "workstead/1"


def tls_context() -> ssl.SSLContext:
    """Verified TLS that also works where Python ships without CA certificates.

    The python.org macOS build has no CA bundle until "Install Certificates" is
    run, so fall back to the operating system's bundle. Verification stays on.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    paths = ssl.get_default_verify_paths()
    if not any(p and os.path.exists(p) for p in (paths.cafile, paths.capath)) or os.environ.get("SSL_CERT_FILE"):
        for f in (os.environ.get("SSL_CERT_FILE"), "/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                  "/etc/pki/tls/certs/ca-bundle.crt", "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem"):
            if f and os.path.exists(f):
                try:
                    ctx.load_verify_locations(cafile=f)
                    break
                except (OSError, ssl.SSLError):
                    continue
    return ctx


TLS = tls_context()


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"GitHub {status}: {message} ({url})")
        self.status = status


def _gh_available() -> bool:
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def token() -> str:
    for k in ("WS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass  # no gh CLI: fall through to the clear error below
    raise GitHubError(401, "no GitHub token: run `gh auth login` or set GH_TOKEN")


def _issue(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "number": d["number"], "id": d.get("id"), "node_id": d.get("node_id"),
        "title": d.get("title") or "", "body": d.get("body") or "",
        "state": d.get("state"), "state_reason": d.get("state_reason"),
        "labels": [x["name"] if isinstance(x, dict) else x for x in d.get("labels", [])],
        "assignees": [a["login"] for a in d.get("assignees") or []],
        "author": (d.get("user") or {}).get("login", ""),
        "author_association": d.get("author_association", "NONE"),
        "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
        "closed_at": d.get("closed_at"), "is_pr": "pull_request" in d,
    }


def _comment(d: Dict[str, Any]) -> Dict[str, Any]:
    m = re.search(r"/issues/([0-9]+)$", d.get("issue_url", ""))
    return {"id": d["id"], "author": (d.get("user") or {}).get("login", ""),
            "body": d.get("body") or "", "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"), "issue": int(m.group(1)) if m else None,
            "author_association": d.get("author_association", "NONE")}


def _pr(d: Dict[str, Any]) -> Dict[str, Any]:
    head = d.get("head") or {}
    return {"number": d["number"], "author": (d.get("user") or {}).get("login", ""),
            "body": d.get("body") or "", "title": d.get("title") or "", "state": d.get("state"),
            "merged": bool(d.get("merged")), "draft": bool(d.get("draft")),
            "head_sha": head.get("sha"), "head_ref": head.get("ref"),
            "head_repo": (head.get("repo") or {}).get("full_name"),
            "base_ref": (d.get("base") or {}).get("ref"), "mergeable": d.get("mergeable"),
            "labels": [x["name"] for x in d.get("labels", [])],
            "assignees": [a["login"] for a in d.get("assignees") or []],
            "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
            "additions": d.get("additions") or 0, "deletions": d.get("deletions") or 0,
            "changed_files": d.get("changed_files") or 0}


class GitHub:
    def __init__(self, repo: str, tok: Optional[str] = None, min_remaining: int = 50, api: str = "",
                 anonymous: bool = False):
        self.repo = repo
        self.anonymous = anonymous  # guests: public reads only, no token (60 requests an hour)
        self.api = (api or API).rstrip("/")
        self._token = tok
        self.min_remaining = min_remaining
        self.remaining: Optional[int] = None
        self.calls = 0
        # Last resort when Python's TLS can't verify GitHub: let `gh` do HTTPS.
        self.via_gh = os.environ.get("WS_TRANSPORT") == "gh"

    # --- plumbing -----------------------------------------------------------

    @property
    def tok(self) -> str:
        if self.anonymous:
            return ""
        if not self._token:
            self._token = token()
        return self._token

    def request(self, method: str, path: str, body: Any = None, accept: str = "application/vnd.github+json",
                raw: bool = False, tries: int = 5) -> Any:
        if self.via_gh and not self.anonymous:
            return self._request_gh(method, path, body, accept, raw)
        try:
            return self._request(method, path, body, accept, raw, tries)
        except GitHubError as e:
            if e.status == 0 and "CERTIFICATE_VERIFY_FAILED" in str(e) and not self._token_from_env() \
                    and not self.anonymous:
                self.via_gh = True
                return self._request_gh(method, path, body, accept, raw)
            raise

    @staticmethod
    def _token_from_env() -> bool:
        return any(os.environ.get(k) for k in ("WS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")) and not _gh_available()

    def _request_gh(self, method: str, path: str, body: Any, accept: str, raw: bool) -> Any:
        """The same request through the GitHub CLI (`gh api --include`)."""
        url = path if path.startswith("http") else f"{self.api}{path}"
        endpoint = url[len(self.api):] if url.startswith(self.api) else url
        cmd = ["gh", "api", "--include", "-X", method, "-H", f"Accept: {accept}", endpoint]
        if body is not None:
            cmd += ["--input", "-"]
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                           capture_output=True, text=True, timeout=120)
        self.calls += 1
        out = r.stdout.replace("\r\n", "\n")
        head, _, payload = out.partition("\n\n")
        m = re.match(r"HTTP/[0-9.]+ ([0-9]{3})", head)
        status = int(m.group(1)) if m else (200 if r.returncode == 0 else 0)
        headers = {}
        for line in head.splitlines()[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if headers.get("x-ratelimit-remaining", "").isdigit():
            self.remaining = int(headers["x-ratelimit-remaining"])
        self._last_link = headers.get("link", "")
        if status >= 400 or status == 0:
            raise GitHubError(status, (payload or r.stderr).strip()[:500], url)
        if raw:
            return payload
        return json.loads(payload) if payload.strip() else None

    def _request(self, method: str, path: str, body: Any, accept: str, raw: bool, tries: int) -> Any:
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        delay = 2.0
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method)
            if self.tok:
                req.add_header("Authorization", f"Bearer {self.tok}")
            req.add_header("Accept", accept)
            req.add_header("User-Agent", UA)
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60, context=TLS if url.startswith("https") else None) as r:
                    self.calls += 1
                    rem = r.headers.get("X-RateLimit-Remaining")
                    if rem is not None:
                        self.remaining = int(rem)
                    payload = r.read()
                    self._last_link = r.headers.get("Link", "")
                    if raw:
                        return payload.decode("utf-8", "replace")
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as e:
                self.calls += 1
                msg = e.read().decode("utf-8", "replace")[:500]
                retry_after = e.headers.get("Retry-After")
                limited = e.code == 429 or (e.code == 403 and ("rate limit" in msg.lower() or retry_after))
                if (limited or e.code >= 500) and attempt < tries - 1:
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    reset = e.headers.get("X-RateLimit-Reset")
                    if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0" and reset:
                        wait = max(wait, min(900.0, float(reset) - time.time() + 1))
                    time.sleep(min(wait, 900.0))
                    delay = min(delay * 2, 120)
                    continue
                raise GitHubError(e.code, msg, url) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < tries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 120)
                    continue
                raise GitHubError(0, str(e), url) from None
        raise GitHubError(0, "retries exhausted", url)

    def paginate(self, path: str, limit: int = 10000) -> List[Any]:
        out: List[Any] = []
        url: Optional[str] = path + ("&" if "?" in path else "?") + "per_page=100"
        while url and len(out) < limit:
            page = self.request("GET", url)
            if isinstance(page, dict):  # search and sub-resource envelopes
                page = page.get("items") or page.get("check_runs") or page.get("statuses") or []
            out.extend(page)
            m = re.search(r'<([^>]+)>;\s*rel="next"', getattr(self, "_last_link", "") or "")
            url = m.group(1) if m else None
        return out

    def budget_ok(self) -> bool:
        return self.remaining is None or self.remaining > self.min_remaining

    @property
    def r(self) -> str:
        return f"/repos/{self.repo}"

    # --- identity -----------------------------------------------------------

    def me(self) -> str:
        return self.request("GET", "/user")["login"]

    def branch_sha(self, branch: str) -> str:
        return self.request("GET", f"{self.r}/commits/{branch}")["sha"]

    def repo_info(self) -> Dict[str, Any]:
        d = self.request("GET", self.r)
        return {"private": d.get("private", False), "default_branch": d.get("default_branch", "main"),
                "can_push": bool((d.get("permissions") or {}).get("push")), "fork": d.get("fork", False),
                "template": (d.get("template_repository") or {}).get("full_name"),
                "owner": (d.get("owner") or {}).get("login"), "owner_type": (d.get("owner") or {}).get("type"),
                "description": d.get("description") or "",
                "parent": (d.get("parent") or {}).get("full_name")}

    def permission(self, login: str) -> str:
        try:
            return self.request("GET", f"{self.r}/collaborators/{login}/permission").get("permission", "none")
        except GitHubError as e:
            if e.status in (403, 404):
                return "none"
            raise

    def search_count(self, q: str) -> int:
        # One try: the search API allows ~30 requests a minute, and a queue run
        # must never stall waiting for it. Callers treat errors as "unknown".
        d = self.request("GET", "/search/issues?q=" + urllib.parse.quote(q) + "&per_page=1", tries=1)
        return int(d.get("total_count", 0))

    def merged_pr_count(self, login: str, label: str) -> int:
        return self.search_count(f'repo:{self.repo} is:pr is:merged author:{login} label:"{label}"')

    # --- issues -------------------------------------------------------------

    def get_issue(self, n: int) -> Dict[str, Any]:
        return _issue(self.request("GET", f"{self.r}/issues/{n}"))

    def list_issues(self, labels: Iterable[str] = (), state: str = "open", since: Optional[str] = None,
                    assignee: Optional[str] = None, include_prs: bool = False, limit: int = 10000) -> List[Dict[str, Any]]:
        q = {"state": state, "sort": "created", "direction": "asc"}
        if labels:
            q["labels"] = ",".join(labels)
        if since:
            q["since"] = since
        if assignee:
            q["assignee"] = assignee
        items = [_issue(d) for d in self.paginate(f"{self.r}/issues?" + urllib.parse.urlencode(q), limit)]
        return items if include_prs else [i for i in items if not i["is_pr"]]

    def create_issue(self, title: str, body: str, labels: List[str]) -> Dict[str, Any]:
        return _issue(self.request("POST", f"{self.r}/issues", {"title": title, "body": body, "labels": labels}))

    def edit_issue(self, n: int, **fields: Any) -> Dict[str, Any]:
        fields = {k: v for k, v in fields.items() if v is not None}
        return _issue(self.request("PATCH", f"{self.r}/issues/{n}", fields))

    def add_labels(self, n: int, labels: List[str]) -> None:
        if labels:
            self.request("POST", f"{self.r}/issues/{n}/labels", {"labels": labels})

    def remove_label(self, n: int, label: str) -> None:
        try:
            self.request("DELETE", f"{self.r}/issues/{n}/labels/{urllib.parse.quote(label, safe='')}")
        except GitHubError as e:
            if e.status != 404:
                raise

    def add_assignees(self, n: int, logins: List[str]) -> None:
        if logins:
            self.request("POST", f"{self.r}/issues/{n}/assignees", {"assignees": logins})

    def remove_assignees(self, n: int, logins: List[str]) -> None:
        if logins:
            self.request("DELETE", f"{self.r}/issues/{n}/assignees", {"assignees": logins})

    # --- comments -----------------------------------------------------------

    def list_comments(self, n: int) -> List[Dict[str, Any]]:
        return [_comment(c) for c in self.paginate(f"{self.r}/issues/{n}/comments?")]

    def repo_comments_since(self, since: str) -> List[Dict[str, Any]]:
        q = urllib.parse.urlencode({"since": since, "sort": "updated", "direction": "asc"})
        return [_comment(c) for c in self.paginate(f"{self.r}/issues/comments?{q}")]

    def create_comment(self, n: int, body: str) -> Dict[str, Any]:
        return _comment(self.request("POST", f"{self.r}/issues/{n}/comments", {"body": body}))

    def edit_comment(self, cid: int, body: str) -> Dict[str, Any]:
        return _comment(self.request("PATCH", f"{self.r}/issues/comments/{cid}", {"body": body}))

    def delete_comment(self, cid: int) -> None:
        try:
            self.request("DELETE", f"{self.r}/issues/comments/{cid}")
        except GitHubError as e:
            if e.status != 404:
                raise

    def react(self, cid: int, content: str) -> None:
        try:
            self.request("POST", f"{self.r}/issues/comments/{cid}/reactions", {"content": content})
        except GitHubError:
            pass  # reactions are courtesy, never load-bearing

    # --- GitHub-native relationships (mirrors; the meta block is the truth) --

    def add_sub_issue(self, parent: int, child_id: int) -> bool:
        try:
            self.request("POST", f"{self.r}/issues/{parent}/sub_issues", {"sub_issue_id": child_id})
            return True
        except GitHubError:
            return False

    def add_blocked_by(self, n: int, blocker_id: int) -> bool:
        try:
            self.request("POST", f"{self.r}/issues/{n}/dependencies/blocked_by", {"issue_id": blocker_id})
            return True
        except GitHubError:
            return False

    def pin_issue(self, node_id: str) -> bool:
        try:
            self.request("POST", "/graphql", {"query": "mutation($id:ID!){pinIssue(input:{issueId:$id}){issue{number}}}",
                                              "variables": {"id": node_id}})
            return True
        except GitHubError:
            return False

    # --- pull requests ------------------------------------------------------

    def get_pr(self, n: int) -> Dict[str, Any]:
        return _pr(self.request("GET", f"{self.r}/pulls/{n}"))

    def pr_files(self, n: int) -> List[str]:
        return [f["filename"] for f in self.paginate(f"{self.r}/pulls/{n}/files?", limit=3000)]

    def pr_patches(self, n: int) -> Dict[str, str]:
        """{file: unified diff hunk text} (GitHub omits the text for binary or huge files)."""
        return {f["filename"]: f.get("patch") or "" for f in self.paginate(f"{self.r}/pulls/{n}/files?", limit=3000)}

    # --- other repositories (safety-net verification) ---------------------------

    def repo_owner(self, full: str) -> Tuple[str, str]:
        d = self.request("GET", f"/repos/{full}")
        if d.get("private"):
            raise GitHubError(403, "private repository", full)
        return d["owner"]["login"], d["owner"]["type"]

    def public_member(self, org: str, login: str) -> bool:
        try:
            self.request("GET", f"/orgs/{org}/public_members/{login}")
            return True
        except GitHubError:
            return False

    def foreign_author(self, full: str, n: int) -> str:
        return (self.request("GET", f"/repos/{full}/issues/{n}").get("user") or {}).get("login", "")

    def pr_reviews(self, n: int) -> List[Dict[str, Any]]:
        """Native GitHub reviews: [{login, state, sha}]."""
        return [{"login": (r.get("user") or {}).get("login", ""), "state": r.get("state"), "sha": r.get("commit_id")}
                for r in self.paginate(f"{self.r}/pulls/{n}/reviews?", limit=300)]

    def prs_for_sha(self, sha: str) -> List[int]:
        try:
            return [p["number"] for p in self.request("GET", f"{self.r}/commits/{sha}/pulls") or []]
        except GitHubError:
            return []

    # The queue's own jobs: never wait on ourselves.
    OWN_CHECKS = ("workstead-queue", "workstead-sweep", "workstead-setup")

    def checks_state(self, sha: str) -> str:
        """success, pending, failure or none, over check runs and statuses."""
        runs = self.paginate(f"{self.r}/commits/{sha}/check-runs?", limit=500)
        runs = [c for c in runs if (c.get("name") or "") not in self.OWN_CHECKS]
        st = self.request("GET", f"{self.r}/commits/{sha}/status")
        statuses = st.get("statuses") or []
        if not runs and not statuses:
            return "none"
        bad = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
        if any(c.get("conclusion") in bad for c in runs) or st.get("state") in ("failure", "error") and statuses:
            return "failure"
        if any(c.get("status") != "completed" for c in runs) or (statuses and st.get("state") == "pending"):
            return "pending"
        return "success"

    def merge_pr(self, n: int, sha: str, method: str) -> bool:
        try:
            self.request("PUT", f"{self.r}/pulls/{n}/merge", {"sha": sha, "merge_method": method})
            return True
        except GitHubError as e:
            if e.status in (405, 409, 422):
                return False
            raise

    def create_pr(self, head: str, base: str, title: str, body: str, draft: bool = False) -> Dict[str, Any]:
        return _pr(self.request("POST", f"{self.r}/pulls", {"head": head, "base": base, "title": title,
                                                            "body": body, "draft": draft,
                                                            "maintainer_can_modify": True}))

    def find_pr(self, head: str) -> Optional[Dict[str, Any]]:
        got = self.request("GET", f"{self.r}/pulls?state=open&head={urllib.parse.quote(head)}")
        return _pr(got[0]) if got else None

    # --- repository housekeeping -------------------------------------------

    def ensure_label(self, name: str, color: str, description: str) -> str:
        try:
            self.request("POST", f"{self.r}/labels", {"name": name, "color": color, "description": description[:100]})
            return "created"
        except GitHubError as e:
            if e.status != 422:
                raise
            self.request("PATCH", f"{self.r}/labels/{urllib.parse.quote(name, safe='')}",
                         {"color": color, "description": description[:100]})
            return "updated"

    def dispatch(self, workflow: str, ref: str, inputs: Dict[str, str]) -> None:
        self.request("POST", f"{self.r}/actions/workflows/{workflow}/dispatches", {"ref": ref, "inputs": inputs})

    def publish_snapshot(self, branch: str, files: Dict[str, str], message: str) -> None:
        """Force-push a single-commit orphan branch holding files (the board)."""
        tree = self.request("POST", f"{self.r}/git/trees", {"tree": [
            {"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()]})
        commit = self.request("POST", f"{self.r}/git/commits", {"message": message, "tree": tree["sha"], "parents": []})
        try:
            self.request("PATCH", f"{self.r}/git/refs/heads/{branch}", {"sha": commit["sha"], "force": True})
        except GitHubError as e:
            if e.status not in (404, 422):
                raise
            self.request("POST", f"{self.r}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})

    def read_snapshot(self, branch: str, path: str, public: bool = True) -> Optional[str]:
        if public:
            try:
                url = f"https://raw.githubusercontent.com/{self.repo}/{branch}/{path}"
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30, context=TLS) as r:
                    return r.read().decode()
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass  # CDN unavailable: fall back to the API below
        try:
            return self.request("GET", f"{self.r}/contents/{path}?ref={branch}", accept="application/vnd.github.raw", raw=True)
        except GitHubError:
            return None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

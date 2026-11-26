"""Contributing without a GitHub account ("guest mode").

Relay mode already needs no GitHub for claims, verification and receipts:
they are signed Nostr events. The one step that did was delivery: pushing a
branch to your fork and opening a pull request. A guest delivers the patch
itself, as signed Patch events (kind 7663) on the relays, and the project's
own settlement workflow opens the pull request for them.

1. **Identity is a key.** `ws join` creates one (`~/.workstead/nostr.json`).
   Without a GitHub login (no `gh auth`, no token) `ws` switches to guest
   mode on its own; `WS_GUEST=1` forces it, `WS_GUEST=0` turns it off.
2. **A canonical commit.** The result is one commit on the task's base,
   built with fixed author, date and message (the producer's key, the task's
   time, its title), so anyone holding the base and the patch rebuilds the
   exact same commit hash: the `output` in the receipt.
3. **The patch on the relays**, in base64 chunks, each signed by the
   producer and tagged with the patch's sha256 (`ref: nostr:<sha256>`).
4. **Verifiers** fetch the chunks, check the hash, rebuild the commit,
   compare it with `output` and then run the checks as for any patch.
5. **The gateway** (the settle workflow, with the repository's own token)
   acts only on tasks already verified by the quorum. It rebuilds the
   commit from the patch, refuses anything that touches protected paths,
   dependency or build files, symlinks or submodules, or is too large, and
   checks the hash matches what the verifiers signed. Then it pushes
   `ws/guest-<task>`, opens the pull request in the guest's key's name and
   starts CI. The usual gate (green CI on a clean runner, the contract,
   maintainer rules) decides the merge.

No credential ever leaves the gateway, and nothing a guest sends is run by
it: it only applies a patch to git's index and compares hashes.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

CHUNK = 15000  # base64 characters per event; nostr.MAX_CONTENT is 16000
REF_PREFIX = "nostr:"
BRANCH_PREFIX = "ws/guest-"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Every git call that produces or consumes a patch pins the settings that
# change its bytes, so producers, verifiers and the gateway agree exactly.
GIT_CONF = ["-c", "core.quotepath=true", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
            "-c", "diff.renames=false", "-c", "diff.relative=false", "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            "-c", "apply.whitespace=nowarn"]
DIFF_ARGS = ["diff", "--binary", "--full-index", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
             "--diff-algorithm=myers", "--no-indent-heuristic", "--src-prefix=a/", "--dst-prefix=b/"]


# --- who is a guest ----------------------------------------------------------------------

_LOGIN: Optional[bool] = None


def has_github_login() -> bool:
    global _LOGIN
    if _LOGIN is None:
        if any(os.environ.get(k) for k in ("WS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
            _LOGIN = True
        else:
            try:
                r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
                _LOGIN = r.returncode == 0 and bool(r.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                _LOGIN = False
    return _LOGIN


def is_guest() -> bool:
    v = os.environ.get("WS_GUEST", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return not has_github_login()


def login_for(pubkey_hex: str) -> str:
    """How a guest appears in the queue: guest-<first 12 characters of the npub after npub1>."""
    from .schnorr import npub
    return "guest-" + npub(bytes.fromhex(pubkey_hex))[5:17]


# --- the canonical commit ----------------------------------------------------------------

def commit_message(title: str, task_id: str, producer: str) -> str:
    from .schnorr import npub
    title = re.sub(r"\s+", " ", title or "Relay task").strip()[:120]
    return f"{title} (relay task {task_id[:12]})\n\nContributed without a GitHub account by {npub(bytes.fromhex(producer))}\n"


def commit_env(producer: str, created_at: int) -> Dict[str, str]:
    from .schnorr import npub
    who = npub(bytes.fromhex(producer))
    date = f"@{int(created_at)} +0000"
    return {"GIT_AUTHOR_NAME": "Workstead guest", "GIT_AUTHOR_EMAIL": f"{who[:24]}@guest.workstead.invalid",
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_NAME": "Workstead guest",
            "GIT_COMMITTER_EMAIL": f"{who[:24]}@guest.workstead.invalid", "GIT_COMMITTER_DATE": date}


def _run(args: List[str], cwd: Path, env: Optional[Dict[str, str]] = None, stdin: Optional[bytes] = None) -> bytes:
    full = dict(os.environ)
    full.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    full.update(env or {})
    r = subprocess.run(["git", *GIT_CONF, *args], cwd=str(cwd), input=stdin, capture_output=True, env=full,
                       timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {r.stderr.decode(errors='replace')[-400:]}")
    return r.stdout


def canonical_commit(repo: Path, tree: str, base: str, title: str, task_id: str, producer: str,
                     created_at: int) -> str:
    env = commit_env(producer, created_at)
    msg = commit_message(title, task_id, producer).encode()
    return _run(["commit-tree", tree, "-p", base, "-F", "-"], repo, env, stdin=msg).decode().strip()


def diff(repo: Path, base: str, output: str) -> bytes:
    return _run([*DIFF_ARGS, base, output], repo)


def rebuild(repo: Path, base: str, patch: bytes, title: str, task_id: str, producer: str, created_at: int) -> str:
    """Apply a patch to base in a private index (no working tree, no filters) and commit it canonically."""
    with tempfile.TemporaryDirectory() as d:
        env = {"GIT_INDEX_FILE": str(Path(d) / "index")}
        _run(["read-tree", base], repo, env)
        if patch:
            _run(["apply", "--cached", "--binary", "-"], repo, env, stdin=patch)
        tree = _run(["write-tree"], repo, env).decode().strip()
    return canonical_commit(repo, tree, base, title, task_id, producer, created_at)


# --- patches as events -------------------------------------------------------------------

def patch_events(seckey: bytes, repo_url: str, task_id: str, patch: bytes, created_at: int, pow_bits: int = 0,
                 ttl: int = 30 * 86400) -> List[Dict[str, Any]]:
    from . import nostr as N
    h = hashlib.sha256(patch).hexdigest()
    data = base64.b64encode(patch).decode()
    parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [""]
    return [N.build(seckey, N.K_PATCH, [N.APP_TAG, ["r", repo_url], ["e", task_id], ["x", h],
                                        ["part", f"{i + 1}/{len(parts)}"]], chunk, created_at,
                    pow_bits=pow_bits, expires_at=created_at + ttl)
            for i, chunk in enumerate(parts)]


def patch_ok(ev: Dict[str, Any]) -> bool:
    from . import nostr as N
    part = N.tag(ev, "part", "")
    m = re.fullmatch(r"(\d{1,4})/(\d{1,4})", part)
    return (bool(m) and 1 <= int(m.group(1)) <= int(m.group(2)) <= 2000 and bool(HEX64.match(N.tag(ev, "x", "")))
            and bool(HEX64.match(N.tag(ev, "e", ""))) and len(ev["content"]) <= CHUNK
            and re.fullmatch(r"[A-Za-z0-9+/=]*", ev["content"]) is not None)


def assemble(events: Iterable[Dict[str, Any]], patch_hash: str, producer: str, max_bytes: int) -> Optional[bytes]:
    """The patch from its signed parts, or None if parts are missing, foreign or don't hash right."""
    from . import nostr as N
    parts: Dict[int, str] = {}
    total = None
    for ev in events:
        if ev.get("kind") != N.K_PATCH or ev.get("pubkey") != producer or N.tag(ev, "x") != patch_hash:
            continue
        if not (patch_ok(ev) and N.verify(ev)):
            continue
        i, n = (int(x) for x in N.tag(ev, "part").split("/"))
        if total is None:
            total = n
        if n == total:
            parts.setdefault(i, ev["content"])
    if total is None or len(parts) != total or len(parts) * CHUNK * 3 // 4 > max_bytes + CHUNK:
        return None
    try:
        data = base64.b64decode("".join(parts[i] for i in range(1, total + 1)), validate=True)
    except (ValueError, KeyError):
        return None
    if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != patch_hash:
        return None
    return data


def fetch_patch(relays, patch_hash: str, producer: str, max_bytes: int) -> Optional[bytes]:
    from . import nostr as N
    pool = getattr(relays, "milestone", relays)
    evs = pool.query([{"kinds": [N.K_PATCH], "#x": [patch_hash], "authors": [producer]}])
    return assemble(evs, patch_hash, producer, max_bytes)


# --- what a guest may change -------------------------------------------------------------

def changed_files(patch: bytes) -> List[Tuple[str, str]]:
    """(path, mode) for every file a patch touches; mode is the new mode ("" when deleted or unchanged)."""
    out: List[Tuple[str, str]] = []
    cur: Optional[List[str]] = None
    for line in patch.decode("utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = [m.group(2) if m else line[11:], ""]
            out.append(tuple(cur))  # type: ignore[arg-type]
        elif cur is not None and re.match(r"^(new file mode|new mode|index [0-9a-f]+\.\.[0-9a-f]+) ", line):
            mode = line.rsplit(" ", 1)[-1]
            if re.fullmatch(r"\d{6}", mode):
                cur[1] = mode
                out[-1] = (cur[0], mode)
    return out


def problems(cfg: Dict[str, Any], patch: bytes) -> List[str]:
    """Why the gateway won't open a pull request for this guest patch (empty: it will)."""
    rv, g = cfg["review"], cfg["relay"]["guest"]
    files = changed_files(patch)
    out = []
    if len(patch) > int(g["max_patch_kb"]) * 1024:
        out.append(f"the patch is larger than {g['max_patch_kb']} KB")
    if not files:
        out.append("the patch changes nothing")
    for path, mode in files:
        if path.startswith('"') or "\\" in path:
            out.append(f"{path}: quoted or unusual file name")
        elif any(path == p or (p.endswith("/") and path.startswith(p)) for p in rv["protected_paths"]):
            out.append(f"{path}: protected path (needs a GitHub account and a maintainer)")
        elif any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], p) for p in rv["maintainer_patterns"]):
            out.append(f"{path}: dependency, build or binary file (needs a GitHub account and a maintainer)")
        elif mode == "120000":
            out.append(f"{path}: symbolic link")
        elif mode == "160000":
            out.append(f"{path}: submodule")
        elif path.split("/")[0].startswith(".git") or "/.git/" in f"/{path}":
            out.append(f"{path}: git metadata")
    if len(files) > rv["maintainer_if_files_over"]:
        out.append(f"touches {len(files)} files (more than {rv['maintainer_if_files_over']})")
    return out


# --- the gateway (runs in the settle workflow) -------------------------------------------

def open_guest_prs(q, relays, events: List[Dict[str, Any]], gate_key: Optional[bytes],
                   push: Optional[Callable[[Path, str, str], None]] = None,
                   remote: str = "") -> List[Dict[str, Any]]:
    """For every verified guest result without a pull request: rebuild, check, push, open, start CI."""
    from . import nostr as N
    cfg, gh = q.cfg, q.gh
    if not cfg["relay"]["guest"]["enabled"]:
        return []
    repo_url = f"https://github.com/{gh.repo}"
    rows = N.compute(events, cfg, repo_url, int(q.now()))
    have = {m.group(1) for i in gh.list_issues(include_prs=True, state="all")
            for m in [re.search(r'ws:relay \{"task": "([0-9a-f]{64})"', i.get("body") or "")] if m}
    out = []
    for tid, row in sorted(rows.items()):
        if row["status"] != "verified" or tid in have:
            continue
        prod = [e for e in events if e["kind"] == N.K_RECEIPT and N.tag(e, "e") == tid and N.tag(e, "role") == "produce"
                and e["pubkey"] == row.get("producer") and N.tag(e, "output") == row.get("output")]
        if not prod or not (N.tag(prod[-1], "ref", "") or "").startswith(REF_PREFIX):
            continue
        receipt = prod[-1]
        res = {"task": tid, "guest": login_for(receipt["pubkey"])}
        patch = fetch_patch(relays, N.tag(receipt, "ref")[len(REF_PREFIX):], receipt["pubkey"],
                            int(cfg["relay"]["guest"]["max_patch_kb"]) * 1024)
        if patch is None:
            out.append({**res, "decision": "waiting", "reason": "patch parts not found on the relays"})
            continue
        why = problems(cfg, patch)
        if why:
            out.append({**res, "decision": "refused", "reason": "; ".join(why[:5])})
            _tell(relays, gate_key, repo_url, tid, "; ".join(why[:5]), events, int(q.now()), cfg["relay"]["pow_bits"])
            continue
        spec = row.get("spec") or {}
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            _run(["init", "--quiet", "--bare"], work)
            src = remote or repo_url
            _run(["fetch", "--quiet", "--depth=1", src, row["base"]], work)
            sha = rebuild(work, row["base"], patch, spec.get("title", ""), tid, receipt["pubkey"], row["created_at"])
            if sha != row["output"]:
                out.append({**res, "decision": "refused", "reason": f"rebuilt commit {sha[:12]} isn't the verified "
                                                                   f"{row['output'][:12]}"})
                continue
            branch = BRANCH_PREFIX + tid[:12]
            (push or _push_with_token)(work, sha, branch)
        body = (f"{spec.get('summary') or spec.get('title', '')}\n\n---\nContributed **without a GitHub account** by "
                f"`{_npub(receipt['pubkey'])}` (shown here as {res['guest']}). The relay quorum verified commit "
                f"`{sha[:12]}`; the gateway rebuilt it from the signed patch, byte for byte. Relay task `{tid}`; "
                f"producer receipt `{receipt['id']}`. Settles when CI is green and the maintainer rules pass.\n\n"
                f"<!-- ws:relay {{\"task\": \"{tid}\", \"receipt\": \"{receipt['id']}\", \"guest\": true}} -->\n")
        pr = gh.create_pr(branch, cfg["default_branch"], spec.get("title", "Relay task")[:200], body)["number"]
        try:  # pushes made with the workflow token don't start workflows by themselves
            gh.dispatch("ci.yml", branch, {})
        except Exception as e:
            res["note"] = f"CI not started: {e}"
        out.append({**res, "decision": "opened", "pr": pr, "branch": branch})
    return out


def _npub(pk: str) -> str:
    from .schnorr import npub
    return npub(bytes.fromhex(pk))


def _push_with_token(work: Path, sha: str, branch: str) -> None:
    """Push one commit with the workflow token, passed as a header and never written to disk."""
    from .gh import token
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise RuntimeError("the gateway runs in GitHub Actions")
    auth = base64.b64encode(f"x-access-token:{token()}".encode()).decode()
    _run(["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}", "push", "--quiet",
          f"https://github.com/{repo}.git", f"{sha}:refs/heads/{branch}"], work)


def _tell(relays, gate_key: Optional[bytes], repo_url: str, tid: str, reason: str, events, now: int,
          pow_bits: int = 0) -> None:
    """Tell the guest why, once, as a finding on the task (they have no GitHub to read)."""
    if not gate_key:
        return
    from . import findings as F
    from . import nostr as N
    from .schnorr import pubkey
    me = pubkey(gate_key).hex()
    approach = "deliver this result through the guest gateway"
    if any(e["kind"] == N.K_FINDING and e["pubkey"] == me and N.tag(e, "e") == tid for e in events):
        return
    try:
        relays.publish(F.finding_event(gate_key, repo_url, approach, "dead-end", f"refused: {reason}"[:300], now, tid,
                                       pow_bits=pow_bits))
    except Exception:
        return  # best effort: the refusal is also in the workflow log

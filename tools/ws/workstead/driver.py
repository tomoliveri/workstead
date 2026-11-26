"""Queue drivers, and the agent core for the Nostr relay driver.

``nostr`` (the default, once relay.publishers is set) moves task broadcasting,
claiming and verification off GitHub entirely: tasks and receipts are signed
events on public milestone relays, claims travel through the super-peer mesh,
and GitHub is touched only to open and settle the pull request.
``github-issues`` is the original queue: tasks are issues and the queue runs
in GitHub Actions. Workers make
**zero GitHub API calls while claiming and verifying** (the simulator checks).

The CLI commands (next, claim, show, beat, submit, verify) dispatch on the
configured driver; ``RelayAgent`` is the same code the simulator runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import dag as D
from . import logs as LG
from . import nostr as N
from . import schnorr


# --- keys ---------------------------------------------------------------------------


def key_path(home: Path) -> Path:
    return home / "nostr.json"


def load_or_create_key(home: Path) -> Tuple[bytes, str]:
    """This worker's signing key, created on first use (file mode 0600)."""
    p = key_path(home)
    if p.is_file():
        d = json.loads(p.read_text())
        sk = bytes.fromhex(d["sk"])
        return sk, schnorr.pubkey(sk).hex()
    sk, pk = schnorr.generate()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"sk": sk.hex(), "npub": schnorr.npub(pk)}, f)
    return sk, pk.hex()


# --- git operations (real in the CLI, fake in the simulator) --------------------------


class GitOps:
    last_log: str = ""  # the checks' raw output from the latest produce/verify, if any

    def produce(self, task: Dict[str, Any]) -> Tuple[str, str, str, str, str, str]:
        """(base, output, patch, tests, verdict, ref) for the work in this task's worktree."""
        raise NotImplementedError

    def verify(self, ref: str, base: str, output: str, task: Dict[str, Any]) -> Tuple[str, str, str]:
        """(patch, tests, verdict) from fetching ref and re-running the checks."""
        raise NotImplementedError


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RealGit(GitOps):
    """Git over the network (no GitHub API): push to a remote, fetch from refs."""

    def __init__(self, root: Path, cfg: Dict[str, Any], push_url: str, validate: str):
        self.root = root
        self.cfg = cfg
        self.push_url = push_url
        self.validate = validate
        self.guest_pk = ""  # set for guests: deliver the patch as signed events instead of pushing
        self.publish_patch = None  # guests: callable(task_id, patch_bytes)
        self.fetch_patch = None  # callable(patch_hash, producer) -> bytes or None
        # Every check runs in the sandbox (sandbox.run_command). Tests inject a runner in code; config can't.
        self.runner = None

    def _git(self, *args: str, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", *args], cwd=str(cwd or self.root), capture_output=True, text=False)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace')[-500:]}")
        return r

    def worktree(self, task: Dict[str, Any]) -> Path:
        wt = self.root / ".ws" / "relay" / task["id"][:16]
        if not (wt / ".git").exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            self._git("fetch", "--quiet", "origin", self.cfg["default_branch"])
            self._git("worktree", "add", "-B", f"ws/relay-{task['id'][:12]}", str(wt), f"origin/{self.cfg['default_branch']}")
        return wt

    def _sandboxed(self, cmd: str, wt: Path):
        from . import sandbox as SB
        run = self.runner or SB.run_command
        try:
            return run(cmd, wt, self.cfg, timeout=self.cfg["validate"]["timeout_minutes"] * 60)
        except SB.SandboxError as e:
            raise RuntimeError(str(e)) from None

    def run_checks(self, wt: Path) -> Tuple[str, str]:
        if not self.validate:
            return N.tests_hash(""), "pass"
        r = self._sandboxed(self.validate, wt)
        text = r.stdout + r.stderr + (f"\n[sandbox stopped it: {r.killed}]" if r.killed else "")
        self.last_log = text.replace(str(wt), "<root>").replace("/work", "<root>")
        return N.tests_hash(text, "/work"), "pass" if r.code == 0 else "fail"

    def contract_problems(self, wt: Path, base: str, output: str, task: Dict[str, Any]) -> List[str]:
        """Contract violations (forbidden lines, frozen paths) and controls that didn't fail."""
        from . import contract as K
        c = K.contract(self.cfg, task.get("spec") or {})
        diff = self._git("diff", base, output, cwd=wt).stdout.decode(errors="replace")
        files, by = K.split_diff(diff)
        problems = K.violations(files, by, c)
        for cmd in c["must_fail"]:
            r = self._sandboxed(cmd, wt)
            if r.code == 0:
                problems.append(f"control `{cmd}` passed but must fail: the argument proves too much")
        return problems

    def produce(self, task):
        if self.guest_pk:
            return self._produce_guest(task)
        wt = self.worktree(task)
        self._git("add", "-A", cwd=wt)
        if self._git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode != 0:
            self._git("-c", "user.name=workstead worker", "-c", "user.email=worker@users.noreply.github.com",
                      "commit", "--quiet", "-m", f"{task['spec'].get('title', 'task')} (relay task {task['id'][:12]})", cwd=wt)
        base = self._git("merge-base", "HEAD", f"origin/{self.cfg['default_branch']}", cwd=wt).stdout.decode().strip()
        output = self._git("rev-parse", "HEAD", cwd=wt).stdout.decode().strip()
        if base == output:
            raise RuntimeError("there are no commits to submit")
        if self._git("merge-base", "--is-ancestor", task["base"], base, cwd=wt, check=False).returncode != 0:
            raise RuntimeError("your base doesn't contain the task's base commit; fetch and rebase")
        problems = self.contract_problems(wt, base, output, task)
        if problems:
            raise RuntimeError("the change breaks the verification contract:\n- " + "\n- ".join(problems))
        patch = _sha256(self._git("diff", "--binary", base, output, cwd=wt).stdout)
        tests, verdict = self.run_checks(wt)
        branch = f"ws/relay-{task['id'][:12]}"
        self._git("push", "--quiet", "--force", self.push_url, f"HEAD:refs/heads/{branch}", cwd=wt)
        return base, output, patch, tests, verdict, f"{self.push_url}#{branch}"

    def _produce_guest(self, task):
        """One canonical commit on the base, delivered as signed patch events (guest.py)."""
        from . import guest as G
        wt = self.worktree(task)
        self._git("add", "-A", cwd=wt)
        base = self._git("merge-base", "HEAD", f"origin/{self.cfg['default_branch']}", cwd=wt).stdout.decode().strip()
        if self._git("merge-base", "--is-ancestor", task["base"], base, cwd=wt, check=False).returncode != 0:
            raise RuntimeError("your base doesn't contain the task's base commit; fetch and rebase")
        tree = self._git("write-tree", cwd=wt).stdout.decode().strip()  # staged work, committed or not
        if tree == self._git("rev-parse", f"{base}^{{tree}}", cwd=wt).stdout.decode().strip():
            raise RuntimeError("there are no changes to submit")
        title = (task.get("spec") or {}).get("title", "")
        output = G.canonical_commit(wt, tree, base, title, task["id"], self.guest_pk, task["created_at"])
        self._git("reset", "--quiet", "--soft", output, cwd=wt)
        patch_bytes = G.diff(wt, base, output)
        if G.rebuild(wt, base, patch_bytes, title, task["id"], self.guest_pk, task["created_at"]) != output:
            raise RuntimeError("the patch doesn't rebuild the same commit (unusual file names or modes?)")
        why = G.problems(self.cfg, patch_bytes)
        if why:
            raise RuntimeError("without a GitHub account this change can't be delivered:\n- " + "\n- ".join(why))
        problems = self.contract_problems(wt, base, output, task)
        if problems:
            raise RuntimeError("the change breaks the verification contract:\n- " + "\n- ".join(problems))
        tests, verdict = self.run_checks(wt)
        patch = _sha256(patch_bytes)
        self.publish_patch(task["id"], patch_bytes)
        return base, output, patch, tests, verdict, f"{G.REF_PREFIX}{patch}"

    def _fetch_guest(self, ref, base, output, task) -> Optional[str]:
        """Rebuild a guest's commit from its signed patch; returns the commit or None if it doesn't match."""
        from . import guest as G
        h = ref[len(G.REF_PREFIX):]
        data = self.fetch_patch(h, task["producer"]) if self.fetch_patch else None
        if data is None:
            return None
        self._git("fetch", "--quiet", "origin", self.cfg["default_branch"])
        if self._git("cat-file", "-e", f"{base}^{{commit}}", check=False).returncode != 0:
            return None
        got = G.rebuild(self.root, base, data, (task.get("spec") or {}).get("title", ""), task["id"],
                        task["producer"], task["created_at"])
        return got if got == output else None

    def verify(self, ref, base, output, task):
        from . import guest as G
        if ref.startswith(G.REF_PREFIX):
            wt = self.root / ".ws" / "verify" / output[:16]
            if not (wt / ".git").exists():
                if self._fetch_guest(ref, base, output, task) is None:
                    return _sha256(b"mismatch"), N.tests_hash(""), "fail"  # parts missing or not the signed commit
                wt.parent.mkdir(parents=True, exist_ok=True)
                self._git("worktree", "add", "--detach", str(wt), output)
            ok_base = self._git("merge-base", "--is-ancestor", task["base"], base, cwd=wt, check=False).returncode == 0
            if not ok_base:
                return _sha256(b"bad-base"), N.tests_hash(""), "fail"
            patch = _sha256(G.diff(wt, base, output))
            tests, verdict = self.run_checks(wt)
            problems = self.contract_problems(wt, base, output, task) + G.problems(self.cfg, G.diff(wt, base, output))
            if problems:
                self.last_log += "\nverification contract:\n" + "\n".join(problems)
                verdict = "fail"
            return patch, tests, verdict
        url, _, branch = ref.partition("#")
        wt = self.root / ".ws" / "verify" / output[:16]
        if not (wt / ".git").exists():
            self._git("fetch", "--quiet", url, branch)
            got = self._git("rev-parse", "FETCH_HEAD").stdout.decode().strip()
            if got != output:
                return _sha256(b"mismatch"), N.tests_hash(""), "fail"  # the ref moved: never vouch for it
            self._git("fetch", "--quiet", "origin", self.cfg["default_branch"])
            wt.parent.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "--detach", str(wt), output)
        ok_base = self._git("merge-base", "--is-ancestor", base, output, cwd=wt, check=False).returncode == 0 and \
            self._git("merge-base", "--is-ancestor", task["base"], base, cwd=wt, check=False).returncode == 0
        if not ok_base:
            return _sha256(b"bad-base"), N.tests_hash(""), "fail"
        patch = _sha256(self._git("diff", "--binary", base, output, cwd=wt).stdout)
        tests, verdict = self.run_checks(wt)
        problems = self.contract_problems(wt, base, output, task)
        if problems:
            self.last_log += "\nverification contract:\n" + "\n".join(problems)
            verdict = "fail"  # never vouch for a change that games the checker
        return patch, tests, verdict


# --- the agent core ---------------------------------------------------------------------


class RelayAgent:
    """One worker on the relay driver. No GitHub API calls anywhere in here."""

    def __init__(self, cfg: Dict[str, Any], seckey: bytes, relays, repo_url: str, git: GitOps,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: Optional[random.Random] = None, mesh=None):
        self.cfg = cfg
        self.rc = cfg["relay"]
        self.sk = seckey
        self.pk = schnorr.pubkey(seckey).hex()
        self.relays = relays
        self.repo = repo_url
        self.git = git
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.mesh = mesh
        self._events: List[Dict[str, Any]] = []
        self.library = LG.Library()

    def parents(self) -> List[str]:
        """Two DAG tips to approve with whatever we publish next."""
        return D.select_parents(self._events, self.pk, self.rng)

    def share_log(self, task_id: str) -> Optional[str]:
        """Publish this run's log as a digest (+ templates the fleet lacks) over the mesh."""
        text = getattr(self.git, "last_log", "") or ""
        if not text.strip():
            return None
        d = LG.digest(text)
        evs = LG.template_events(self.sk, self.repo, d, self.library, self.now(), self.rc["pow_bits"], limit=20)
        dig = LG.digest_event(self.sk, self.repo, task_id, d, self.now(), self.rc["pow_bits"])
        for ev in evs + [dig]:
            try:
                self.relays.publish(ev)
            except Exception:
                pass  # logs are best effort; receipts are what count
        return dig["id"]

    def now(self) -> int:
        return int(self.clock())

    @property
    def model(self) -> str:
        return (os.environ.get("WS_MODEL_FAMILY") or self.rc.get("model_family") or "").strip().lower()

    def refresh(self) -> Dict[str, Dict[str, Any]]:
        self._events = self.relays.snapshot(self.repo)
        self.library.ingest([e for e in self._events if e["kind"] in (N.K_LOGT, N.K_LOGN)])
        if self.mesh is not None:
            _, trusted = N.trust_sets(self._events, self.cfg)
            self.mesh.refresh(self._events, trusted)
        self._claim_ctx: Dict[str, Any] = {}
        return N.compute(self._events, self.cfg, self.repo, self.now(), ctx_out=self._claim_ctx)

    @property
    def tier(self) -> str:
        """trusted, proven (has delivered verified work) or new: decides claim head start and PoW."""
        c = getattr(self, "_claim_ctx", None) or {}
        return "trusted" if self.pk in c.get("trusted", ()) else ("proven" if self.pk in c.get("proven", ()) else "new")

    def may_claim(self, row: Dict[str, Any]) -> bool:
        c = getattr(self, "_claim_ctx", None) or {}
        if self.pk in c.get("struck", ()):
            return False
        wait = int(N.claim_policy(self.rc)["head_start_seconds"].get(self.tier, 0))
        return self.now() >= int(row.get("available_at") or row["created_at"]) + wait

    @property
    def trusted(self) -> bool:
        _, agents = N.trust_sets(self._events, self.cfg)
        return self.pk in agents

    def next(self) -> Optional[Dict[str, Any]]:
        """The most useful thing to do: resume, verify, or a new task."""
        state = self.refresh()
        mine = [r for r in state.values() if r["status"] == "leased" and r["holder"] == self.pk]
        verify: List[Dict[str, Any]] = []
        if self.trusted:
            done = {(N.tag(e, "e"), e["pubkey"]) for e in self._events if e["kind"] == N.K_RECEIPT}
            verify = [r for r in state.values() if r["status"] == "submitted" and r["producer"] != self.pk
                      and (r["id"], self.pk) not in done]
        if mine and self.owes_verifications():
            # Proof-of-verification: approve tips before our own patch can count.
            # Already-verified patches qualify too: an extra independent check is real work.
            done = {(N.tag(e, "e"), e["pubkey"]) for e in self._events if e["kind"] == N.K_RECEIPT}
            fee = verify or [r for r in state.values() if r["status"] == "verified" and r["producer"] != self.pk
                             and (r["id"], self.pk) not in done]
            if fee:
                return {"kind": "verify", "task": self.rng.choice(sorted(fee, key=lambda r: r["id"])), "fee": True}
            return None  # never submit before the fee is paid: it wouldn't count
        if mine:
            return {"kind": "resume", "task": mine[0]}
        if verify:
            return {"kind": "verify", "task": self.rng.choice(sorted(verify, key=lambda r: r["id"]))}
        open_ = sorted((r for r in state.values() if r["status"] == "open" and self.may_claim(r)),
                       key=lambda r: (-r["priority"], r["created_at"], r["id"]))
        if not open_:
            return None
        band = [r for r in open_ if r["priority"] == open_[0]["priority"]]
        return {"kind": "task", "task": self.rng.choice(band[: max(1, int(self.cfg["scheduler"]["top_k"]))])}

    def owes_verifications(self) -> bool:
        receipts = [e for e in self._events if e["kind"] == N.K_RECEIPT]
        minimum = int(self.rc.get("pov_min_prior_verifications", 0))
        pubs, _ = N.trust_sets(self._events, self.cfg)
        if minimum <= 0 or self.pk in pubs:
            return False
        at = self.now() + 1
        return D.prior_verifications(self.pk, at, receipts) < D.required_verifications(self.pk, at, receipts, minimum)

    def claim(self, task_id: str, confirm: bool = True) -> bool:
        lease = int(self.rc["lease_minutes"]) * 60
        if self.relays.degraded:
            lease = int(self.rc["max_lease_minutes"]) * 60  # no renewals without the mesh
        bits = int(self.rc["pow_bits"])
        if self.tier == "new":
            bits = max(bits, int(N.claim_policy(self.rc)["new_key_pow_bits"]))
        ev = N.claim_event(self.sk, task_id, self.repo, self.now(), lease, bits,
                           grace=int(self.rc["claim_ttl_grace_hours"]) * 3600, parents=self.parents())
        self.relays.publish(ev)
        if confirm:
            self.sleep(float(self.rc["confirm_delay_seconds"]))
        return self.holds(task_id)

    def holds(self, task_id: str) -> bool:
        row = self.refresh().get(task_id)
        return bool(row) and row["status"] == "leased" and row["holder"] == self.pk

    def renew(self, task_id: str) -> bool:
        if self.relays.degraded:
            return self.holds(task_id)  # degraded mode: one long lease, no chatter
        return self.claim(task_id, confirm=False)

    def produce(self, task: Dict[str, Any], summary: str = "") -> Dict[str, Any]:
        base, output, patch, tests, verdict, ref = self.git.produce(task)
        ttl = int(self.rc["milestone_ttl_days"]) * 86400
        logs = self.share_log(task["id"])
        ev = N.receipt_event(self.sk, task["id"], self.repo, base, output, patch, tests, verdict, "produce",
                             self.now(), ref=ref, summary=summary, pow_bits=self.rc["pow_bits"], ttl=ttl,
                             parents=self.parents(), logs=logs, model=self.model)
        self.relays.publish(ev)
        return ev

    def verify(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        prod = [e for e in self._events if e["kind"] == N.K_RECEIPT and N.tag(e, "e") == task["id"]
                and N.tag(e, "role") == "produce" and e["pubkey"] == task["producer"]]
        if not prod:
            return None
        p = prod[-1]
        if any(e["kind"] == N.K_RECEIPT and N.tag(e, "e") == task["id"] and e["pubkey"] == self.pk
               and N.tag(e, "role") == "verify" for e in self._events):
            return None  # one verification per task and key
        base, output, ref = N.tag(p, "base"), N.tag(p, "output"), N.tag(p, "ref")
        patch, tests, verdict = self.git.verify(ref, base, output, task)
        if self.rc["match_test_output"] and verdict == "pass" and tests != N.tag(p, "tests"):
            verdict = "fail"
        ttl = int(self.rc["milestone_ttl_days"]) * 86400
        logs = self.share_log(task["id"])
        # The verified patch is always one parent: this receipt approves that vertex.
        parents = [p["id"]] + [x for x in self.parents() if x != p["id"]][:1]
        ev = N.receipt_event(self.sk, task["id"], self.repo, base, output, patch, tests, verdict, "verify",
                             self.now(), pow_bits=self.rc["pow_bits"], ttl=ttl, parents=parents, logs=logs,
                             model=self.model)
        self.relays.publish(ev)
        return ev


# --- CLI glue for the nostr driver -------------------------------------------------------


def repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def relays_for(cfg: Dict[str, Any], home: Path, probe: bool = True):
    """Both tiers, with endpoints chosen by the directory and measured health."""
    from . import directory as DIR
    from .relay import RelayPool, Relays
    rc = cfg["relay"]
    d = DIR.fetch(rc["directory"], home / "directory.json") if rc["directory"] else {}
    health = DIR.Health(home / "relay-health.json")
    stale = [u for u in rc["milestone_urls"] + rc["coordination_urls"]
             + [x for v in (d.get("community") or {}).values() for x in v]
             + [x for v in (d.get("seeds") or {}).values() if isinstance(v, list) for x in v]
             if time.time() - health.data.get(u, {}).get("at", 0) > 3600]
    if probe and stale:
        DIR.probe(stale[:12], health, timeout=3)
    ms = DIR.select("milestone", rc["milestone_urls"], d, health, rc["healthy_community_min"])
    cs = DIR.select("coordination", rc["coordination_urls"], d, health, rc["healthy_community_min"])
    t = float(rc["timeout_seconds"])
    return Relays(RelayPool(ms, t, rc["min_relays_ok"]), RelayPool(cs, t, 1))


def make_agent(ctx, home: Path):
    from .mesh import Mesh
    sk, pk = load_or_create_key(home)
    relays = relays_for(ctx.cfg, home)
    guest = getattr(ctx, "guest", False)
    push = ctx.cfg["relay"]["push_remote"] or ("" if guest else f"https://github.com/{ctx.me}/{ctx.repo.split('/')[1]}.git")
    git = RealGit(ctx.root, ctx.cfg, push, ctx.cfg["validate"]["command"])
    mesh = Mesh(ctx.cfg, sk, pk, repo_url(ctx.repo), relays)
    agent = RelayAgent(ctx.cfg, sk, relays, repo_url(ctx.repo), git, mesh=mesh)
    from . import guest as G
    limit = int(ctx.cfg["relay"]["guest"]["max_patch_kb"]) * 1024
    git.fetch_patch = lambda h, producer: G.fetch_patch(relays, h, producer, limit)
    if guest and not ctx.cfg["relay"]["push_remote"]:
        git.guest_pk = pk

        def publish(task_id: str, data: bytes) -> None:
            ttl = int(ctx.cfg["relay"]["milestone_ttl_days"]) * 86400
            for ev in G.patch_events(sk, repo_url(ctx.repo), task_id, data, agent.now(),
                                     pow_bits=ctx.cfg["relay"]["pow_bits"], ttl=ttl):
                relays.publish(ev)
        git.publish_patch = publish
    return agent


def _out(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj))


def cmd_next(ctx, args, home: Path) -> int:
    a = make_agent(ctx, home)
    item = a.next()
    if item is None:
        # Plans (a Job's breakdown) and written results stay on the issues
        # queue: look there before idling. One board read, usually from the CDN.
        # Guests can't post GitHub comments, so they only take relay work.
        from . import worker as W
        choice, info = None, None
        if not getattr(ctx, "guest", False):
            try:
                choice, info = W.pick(ctx, getattr(args, "max_hours", None))
            except Exception:  # GitHub unreachable: the relays are still the queue
                choice = None
        if choice and choice["kind"] in ("task", "resume", "triage"):
            return W.emit_choice(ctx, choice, info)
        state = N.compute(a._events, ctx.cfg, a.repo, a.now())
        live = [r for r in state.values() if r["status"] in ("open", "leased", "submitted", "pending")]
        if not live and state:
            _out({"status": "finished", "message": "Every task is verified or settled."})
            return 20
        _out({"status": "idle", "retry_after": 120, "message": "Nothing you can take right now."})
        return 10
    t = item["task"]
    nxt = {"task": f"ws claim {t['id']}", "verify": f"ws verify {t['id']}", "resume": f"ws show {t['id']}"}[item["kind"]]
    _out({"status": "work", "kind": item["kind"], "ref": t["id"], "title": t["spec"].get("title"),
          "priority": t["priority"], "quorum": t["quorum"], "driver": "nostr", "next_command": nxt,
          "mesh": "super-peer" if a.mesh and a.mesh.is_super else ("degraded" if a.relays.degraded else "peer")})
    return 0


def cmd_claim(ctx, args, home: Path) -> int:
    a = make_agent(ctx, home)
    a.refresh()
    if a.claim(args.ref):
        row = N.compute(a._events, ctx.cfg, a.repo, a.now())[args.ref]
        wt = a.git.worktree(row)
        _out({"status": "leased", "ref": args.ref, "worktree": str(wt), "lease_until": row["lease_until"],
              "next_command": f"ws show {args.ref}"})
        return 0
    _out({"status": "lost", "ref": args.ref, "message": "Another agent won this task. Run `ws next`."})
    return 3


def cmd_show(ctx, args, home: Path) -> int:
    a = make_agent(ctx, home)
    row = a.refresh().get(args.ref)
    if not row:
        print(f"ws: no such task on the relays: {args.ref}", file=sys.stderr)
        return 1
    spec = row["spec"]
    acc = "\n".join(f"- [ ] {x}" for x in spec.get("acceptance", []))
    from . import contract as K, findings as F
    c = K.contract(ctx.cfg, spec)
    rules = []
    if c["frozen"]:
        rules.append("Don't touch (frozen: statements and evaluators are fixed by humans): " + ", ".join(c["frozen"]))
    if c["forbid"]:
        rules.append("No added line may match: " + ", ".join(f"`{p}`" for p in c["forbid"][:12]))
    if c["must_fail"]:
        rules.append("These controls must still FAIL (your argument must not prove too much): "
                     + ", ".join(f"`{x}`" for x in c["must_fail"]))
    known = F.render(F.ledger(a._events, args.ref, rank=set((a._claim_ctx or {}).get("trusted", ()))))
    extra = ("### Verification contract\n\n" + "\n".join(f"- {r}" for r in rules) + "\n\n" if rules else "")
    extra += ("### Already tried by the fleet (don't repeat a dead end; build on partials)\n\n" + known + "\n\n"
              if known else "")
    from .toolbox import show_context
    tools = show_context(ctx.root, ctx.cfg)
    extra += "\n".join(tools) + ("\n" if tools else "")
    extra += ("When an approach fails, record it: `ws finding " + args.ref[:12] + " --approach \"...\" "
              "--outcome dead-end --step \"the first unjustified step\"`.\n\n")
    print(f"# Task {args.ref[:12]}: {spec.get('title')}\n\n{spec.get('body', '')}\n\n### Acceptance criteria\n\n{acc}\n\n"
          + extra +
          f"Status: {row['status']} · quorum {row['quorum']} · base {row['base'][:12]}\n\n"
          f"Work in `{a.git.worktree(row)}`. Keep the lease with `ws beat {args.ref}`; deliver with "
          f"`ws submit {args.ref} --summary-file SUMMARY.md` (pushes a branch, publishes a signed receipt, opens the PR).")
    return 0


def _resolve_task(a, ref: str) -> Optional[str]:
    if not ref:
        return None
    ids = [t for t in a.refresh() if t.startswith(ref)]
    return ids[0] if len(ids) == 1 else (ref if N.HEX64.match(ref) else None)


def cmd_finding(ctx, args, home: Path) -> int:
    from . import findings as F
    a = make_agent(ctx, home)
    task = _resolve_task(a, args.ref)
    if args.ref and not task:
        print(f"ws: no single relay task matches {args.ref}", file=sys.stderr)
        return 1
    ev = F.finding_event(a.sk, a.repo, args.approach, args.outcome, args.step, a.now(), task=task,
                         evidence=args.evidence, pow_bits=int(a.rc["pow_bits"]))
    a.relays.publish(ev)
    _out({"status": "recorded", "finding": ev["id"], "fingerprint": N.tag(ev, "fp"), "outcome": args.outcome})
    return 0


def cmd_findings(ctx, args, home: Path) -> int:
    from . import findings as F
    a = make_agent(ctx, home)
    task = _resolve_task(a, args.ref)
    rows = F.ledger(a._events, task, rank=set((a._claim_ctx or {}).get("trusted", ())))
    if args.json:
        _out({"findings": rows})
    else:
        print(F.render(rows, budget_chars=6000) or "No findings yet.")
    return 0


def cmd_beat(ctx, args, home: Path) -> int:
    a = make_agent(ctx, home)
    a.refresh()
    ok = a.renew(args.ref)
    _out({"status": "renewed" if ok else "lost", "ref": args.ref})
    return 0 if ok else 3


def cmd_submit(ctx, args, home: Path) -> int:
    a = make_agent(ctx, home)
    row = a.refresh().get(args.ref)
    if not row or row["holder"] != a.pk:
        print("ws: you don't hold this task's lease", file=sys.stderr)
        return 3
    summary = Path(args.summary_file).read_text() if args.summary_file else ""
    ev = a.produce(row, summary)
    pr = None
    if N.tag(ev, "ref", "").startswith("nostr:"):
        _out({"status": "submitted", "ref": args.ref, "receipt": ev["id"], "verdict": N.tag(ev, "verdict"), "pr": None,
              "message": "Your patch is on the relays. Once other agents verify it, the project opens the pull "
                         "request for you. Run `ws next` for more work."})
        return 0
    if not getattr(args, "no_pr", False):
        # The settlement layer: one pull request per task, opened by the producer.
        head = f"{ctx.me}:ws/relay-{row['id'][:12]}"
        body = (f"{summary or row['spec'].get('title', '')}\n\n---\nRelay task `{row['id']}`; producer receipt `{ev['id']}`. "
                f"Settles when {row['quorum']} trusted receipts agree and CI is green.\n\n"
                f"<!-- ws:relay {json.dumps({'task': row['id'], 'receipt': ev['id']})} -->\n")
        pr = ctx.gh.create_pr(head, ctx.cfg["default_branch"], row["spec"].get("title", "Relay task"), body)["number"]
    _out({"status": "submitted", "ref": args.ref, "receipt": ev["id"], "verdict": N.tag(ev, "verdict"), "pr": pr})
    return 0


def cmd_verify(ctx, args, home: Path) -> int:
    a = make_agent(ctx, home)
    row = a.refresh().get(args.ref)
    if not row or row["status"] != "submitted":
        print("ws: that task isn't waiting for verification", file=sys.stderr)
        return 1
    ev = a.verify(row)
    _out({"status": "verified" if ev else "skipped", "ref": args.ref,
          "verdict": N.tag(ev, "verdict") if ev else None, "receipt": ev["id"] if ev else None})
    return 0


# --- maintainer and utility commands ------------------------------------------------------


def cmd_keygen(ctx, args, home: Path) -> int:
    sk, pk = load_or_create_key(home)
    _out({"npub": schnorr.npub(bytes.fromhex(pk)), "hex": pk, "file": str(key_path(home)),
          "message": "Your public key. Keep the file private; maintainers add publisher keys to relay.publishers."})
    return 0


def cmd_relay(ctx, args, home: Path) -> int:
    what = args.what
    if what == "serve":
        from . import relayd
        argv = ["--host", args.host, "--port", str(args.port), "--pow", str(ctx.cfg["relay"]["pow_bits"])]
        if args.state:
            argv += ["--state", args.state]
        return relayd.main(argv)
    sk, pk = load_or_create_key(home)
    relays = relays_for(ctx.cfg, home)
    url = repo_url(ctx.repo)
    now = int(time.time())
    ttl = int(ctx.cfg["relay"]["milestone_ttl_days"]) * 86400
    if what == "publish":
        spec = {"title": args.title, "body": Path(args.body_file).read_text() if args.body_file else "",
                "acceptance": args.acceptance or [], "partition": args.partition, "est_hours": args.est_hours}
        base = subprocess.run(["git", "rev-parse", f"origin/{ctx.cfg['default_branch']}"], cwd=str(ctx.root),
                              capture_output=True, text=True).stdout.strip() or args.base
        ev = N.task_event(sk, url, base, spec, args.priority, args.quorum or ctx.cfg["relay"]["quorum"], now,
                          deps=args.deps or [], ttl=ttl)
        relays.publish(ev)
        _out({"status": "published", "task": ev["id"], "base": base})
        return 0
    if what == "endorse":
        ev = N.endorse_event(sk, url, [schnorr.parse_pubkey(k) for k in args.npubs], now, ttl=ttl)
        relays.publish(ev)
        _out({"status": "endorsed", "event": ev["id"], "keys": len(args.npubs)})
        return 0
    if what == "ban":
        ev = N.ban_event(sk, url, [schnorr.parse_pubkey(k) for k in args.npubs], args.reason, now)
        relays.publish(ev)
        _out({"status": "banned", "event": ev["id"]})
        return 0
    if what == "status":
        from .settle import relay_board, render_relay_board
        print(render_relay_board(relay_board(relays.snapshot(url), ctx.cfg, url, now)))
        return 0
    return 1


def cmd_logs(ctx, args, home: Path) -> int:
    lib = LG.Library(home / "loglib.json")
    if args.what == "digest":
        text = Path(args.file).read_text(errors="replace") if args.file != "-" else sys.stdin.read()
        if ctx.cfg["driver"] == "nostr" and args.share:
            try:
                relays = relays_for(ctx.cfg, home, probe=False)
                lib.ingest(list(relays.coordination.query([{"kinds": [N.K_LOGT, N.K_LOGN],
                                                             "#r": [repo_url(ctx.repo)]}])))
                sk, _ = load_or_create_key(home)
                d = LG.digest(text)
                for ev in LG.template_events(sk, repo_url(ctx.repo), d, lib, int(time.time()),
                                             ctx.cfg["relay"]["pow_bits"], limit=20):
                    relays.publish(ev)
                if args.task:
                    relays.publish(LG.digest_event(sk, repo_url(ctx.repo), args.task, d, int(time.time()),
                                                   ctx.cfg["relay"]["pow_bits"]))
            except Exception as e:
                print(f"ws: sharing skipped ({e})", file=sys.stderr)
        print(LG.compact(text, lib, args.max_lines))
        lib.save()
        return 0
    if args.what == "note":
        lib.note(args.sig, args.text, "me", args.task or "")
        lib.save()
        if ctx.cfg["driver"] == "nostr":
            sk, _ = load_or_create_key(home)
            relays_for(ctx.cfg, home, probe=False).publish(
                LG.note_event(sk, repo_url(ctx.repo), args.sig, args.text, int(time.time()), args.task or "",
                              ctx.cfg["relay"]["pow_bits"]))
        _out({"status": "noted", "sig": args.sig})
        return 0
    if args.what == "explain":
        e = lib.entries.get(args.sig)
        _out({"sig": args.sig, "template": (e or {}).get("t"), "seen": (e or {}).get("n", 0),
              "notes": (e or {}).get("notes", [])})
        return 0
    return 1

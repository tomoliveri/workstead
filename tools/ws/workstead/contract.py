"""The verification contract: what "passes" means, beyond the project's checks.

Frontier-lab fleets in 2025-26 kept hitting the same failure: work that
satisfied the checker without doing the job. Lean "proofs" that used `sorry`
or a new axiom, a theorem statement quietly weakened until it was trivially
true, an evaluator exploited through a loophole, and arguments that "prove
too much" (they would also prove things known to be false). A contract
closes those holes mechanically, so a fleet can't game its own verifier:

* ``forbid``: regular expressions no added line may match (``sorry``,
  ``admit``, ``axiom``... in the ``lean`` profile).
* ``frozen``: paths a patch may not touch. Put theorem statements,
  benchmarks and evaluators here: humans agree what is being proved or
  measured; agents only supply the proof or the improvement.
* ``must_fail``: control commands that must FAIL on the result. The
  technique is run against a case where the claim is known to be false (an
  off-line zero, a known counterexample, a planted bug); if the control
  "passes", the argument proves too much and the verdict is fail.

The project sets defaults in ``workstead.json`` (``verify``); a task can
add to them in its spec (``checks``). Producers check their own diff before
submitting, every verifier enforces the contract before signing, and the
settlement gate re-checks ``forbid`` and ``frozen`` against the pull request.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, Iterable, List, Tuple

PROFILES: Dict[str, Dict[str, List[str]]] = {
    # Lean 4: escape hatches that make a proof check without proving anything.
    "lean": {"forbid": [r"\bsorry\b", r"\badmit\b", r"^\s*axiom\s", r"^\s*(private\s+)?opaque\s",
                        r"\bunsafe\b", r"@\[implemented_by", r"@\[extern", r"debug\.skipKernelTC",
                        r"\bnative_decide\b", r"set_option\s+maxRecDepth\s+\d{6,}"],
             "frozen": [], "must_fail": []},
    # Generic code: tests skipped or forced green.
    "code": {"forbid": [r"@pytest\.mark\.skip\b", r"\bit\.skip\(", r"\bxit\(", r"#\[ignore\]",
                        r"assert\s+True\s*$", r"t\.Skip\("],
             "frozen": [], "must_fail": []},
}


def contract(cfg: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, List[str]]:
    """The effective contract for a task: profile + project defaults + the task's own checks."""
    v = cfg.get("verify") or {}
    out: Dict[str, List[str]] = {"forbid": [], "frozen": [], "must_fail": []}
    for layer in [PROFILES.get(v.get("profile") or "", {}), v, (spec or {}).get("checks") or {}]:
        for k in out:
            for x in layer.get(k) or []:
                if isinstance(x, str) and x and x not in out[k]:
                    out[k].append(x)
    return out


def _added_lines(patch: str) -> Iterable[Tuple[str, str]]:
    """(file, line) for every added line of a unified diff."""
    cur = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            cur = line[4:].strip()
            cur = cur[2:] if cur.startswith("b/") else cur
        elif line.startswith("+") and not line.startswith("+++"):
            yield cur, line[1:]


def touched_frozen(files: Iterable[str], frozen: List[str]) -> List[str]:
    """Files matching a frozen glob, or under a frozen directory ("Statements/")."""
    hit = set()
    for f in files:
        for pat in frozen:
            if fnmatch.fnmatch(f, pat) or (pat.endswith("/") and f.startswith(pat)):
                hit.add(f)
    return sorted(hit)


def violations(files: Iterable[str], patch_by_file: Dict[str, str], c: Dict[str, List[str]]) -> List[str]:
    """Human-readable contract violations for a change (empty when it complies)."""
    files = list(files)
    out = [f"touches frozen path {f}" for f in touched_frozen(files, c["frozen"])]
    pats = []
    for p in c["forbid"]:
        try:
            pats.append((p, re.compile(p, re.M)))
        except re.error:
            out.append(f"bad forbid pattern {p!r} in the contract")
    for f, patch in patch_by_file.items():
        for _, line in _added_lines(patch if patch.startswith("+++") else f"+++ b/{f}\n{patch}"):
            for p, rx in pats:
                if rx.search(line):
                    out.append(f"{f}: added line matches forbidden {p!r}: {line.strip()[:80]}")
    return out[:50]


def split_diff(diff: str) -> Tuple[List[str], Dict[str, str]]:
    """A `git diff` into (files, {file: that file's diff text})."""
    files: List[str] = []
    by: Dict[str, List[str]] = {}
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            cur = parts[1].strip() if len(parts) == 2 else line
            files.append(cur)
            by[cur] = []
        elif cur is not None:
            by[cur].append(line)
    return files, {f: "\n".join(v) for f, v in by.items()}

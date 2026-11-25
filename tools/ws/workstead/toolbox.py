"""The toolbox: reusable scripts, found, vetted, shared across the network.

Fleets write the same helper again and again (a curve fit, a held-out
benchmark, a sampler). The toolbox turns a good one into a shared tool:

1. **Find** (`ws tools scan`): scripts that look reusable (a docstring,
   functions, a command line, no hard-coded paths, seeded randomness) and
   near-identical copies that should become one tool.
2. **Vet** (`ws tools vet`): a static check of the source (Python's AST).
   Network access, subprocesses, dynamic code (eval, exec, pickle, ctypes),
   secret and credential access and obfuscated blobs block a script.
   Destructive file operations, absolute paths and undeclared third-party
   imports need a reviewer's look. Static vetting is a filter, not a proof.
3. **Promote** (`ws tools promote`): copy the script into the library
   (`toolbox.path`, default `tools/lib/`) with a manifest entry pinning its
   sha256. The library is a protected path, so the pull request needs the
   fleet's review *and* a maintainer's approval: the vetting that matters is
   a person reading it.
4. **Share**: each project's `tools/lib/toolbox.json` is its registry.
   `ws tools find` lists tools in trusted projects (this one, the template
   it came from, `toolbox.sources`) and signed ToolRelease events (kind
   7662) on the relays. `ws tools get` downloads a tool at a pinned commit,
   checks its sha256 against that project's manifest, vets it again here, and
   keeps it in `.ws/tools/`. A release counts only when it is signed by a
   publisher key listed in its own project's `workstead.json`.
5. **Run** (`ws tools run`): re-check the hash, then run with a scrubbed
   environment (no tokens), and in a network-less container when
   `relay.verify_sandbox` is `docker`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MANIFEST = "toolbox.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
RULES_VERSION = 1

# --- vetting ---------------------------------------------------------------------------

NET_MODULES = {"socket", "ssl", "urllib", "urllib3", "http", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
               "poplib", "imaplib", "telnetlib", "xmlrpc", "socketserver", "paramiko", "websocket", "websockets",
               "pycurl", "grpc", "zmq", "boto3", "botocore", "webbrowser", "nntplib", "asyncssh"}
PROC_MODULES = {"subprocess", "pty", "pexpect", "sh", "plumbum"}
DYN_MODULES = {"ctypes", "cffi", "marshal", "pickle", "cPickle", "_pickle", "dill", "cloudpickle", "shelve",
               "code", "codeop", "runpy", "importlib", "imp", "pydoc", "builtins", "__builtin__", "keyring"}
DYN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "globals", "vars"}
OS_PROC = {"system", "popen", "fork", "forkpty", "kill", "killpg", "posix_spawn", "posix_spawnp", "startfile",
           "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
           "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"}
DESTRUCTIVE = {"rmtree", "remove", "unlink", "rmdir", "removedirs", "chmod", "chown", "lchown", "truncate", "move",
               "rename", "replace", "symlink", "link"}
WATCHED_VARS = re.compile(r"(TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL|AUTH|SESSION|COOKIE|NSEC|AWS_|GH_|GITHUB_)",
                         re.I)
WATCHED_PATHS = re.compile(r"(\.ssh\b|id_rsa|id_ed25519|\.aws\b|\.netrc|\.config/gh|\.gnupg|keychain|\.docker/config|"
                         r"\.kube/config|\.git-credentials|\.npmrc|\.pypirc|Cookies|Login Data|wallet)", re.I)
BLOB = re.compile(r"^[A-Za-z0-9+/=\s]{400,}$|^(?:[0-9a-fA-F]{2}){200,}$")
ABS_PATH = re.compile(r"^(/(?!dev/null$)[A-Za-z]|~[/\\]|[A-Za-z]:[\\/])")
HARD = {"network", "process", "dynamic-code", "secrets", "obfuscation", "unparseable"}


def _stdlib() -> set:
    names = set(getattr(sys, "stdlib_module_names", ()) or ()) | set(sys.builtin_module_names)
    if not getattr(sys, "stdlib_module_names", None):  # Python 3.9: list the standard library directory
        lib = Path(sysconfig.get_paths()["stdlib"])
        for p in lib.iterdir() if lib.is_dir() else []:
            if p.name != "site-packages" and (p.suffix == ".py" or (p.is_dir() and (p / "__init__.py").exists())):
                names.add(p.stem)
        dyn = lib / "lib-dynload"
        for p in dyn.iterdir() if dyn.is_dir() else []:
            names.add(p.name.split(".")[0])
    return names


STDLIB = _stdlib()


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def vet_source(text: str, allow_imports: Iterable[str] = (), filename: str = "script.py") -> Dict[str, Any]:
    """Static vetting of one Python file: {verdict, findings, imports, requires}."""
    findings: List[Dict[str, Any]] = []

    def hit(rule: str, node: Optional[ast.AST], detail: str) -> None:
        findings.append({"rule": rule, "line": getattr(node, "lineno", 0), "detail": detail})

    try:
        tree = ast.parse(text, filename=filename)
    except (SyntaxError, ValueError) as e:
        return {"verdict": "blocked", "findings": [{"rule": "unparseable", "line": getattr(e, "lineno", 0) or 0,
                                                     "detail": f"not valid Python: {e.__class__.__name__}"}],
                "imports": [], "requires": []}
    allow = {a.split(".")[0] for a in allow_imports}
    imports, aliases = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name.split(".")[0])
                aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: a sibling in the same package
                continue
            mod = node.module or ""
            imports.add(mod.split(".")[0])
            for a in node.names:
                aliases[a.asname or a.name] = f"{mod}.{a.name}"
    for m in sorted(imports):
        line = next((n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                     and m in ([a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import)
                               else [(n.module or "").split(".")[0]])), None)
        if m in NET_MODULES:
            hit("network", line, f"imports {m}")
        elif m in PROC_MODULES:
            hit("process", line, f"imports {m}")
        elif m in DYN_MODULES:
            hit("dynamic-code", line, f"imports {m}")
        elif m and m not in STDLIB and m not in allow and m != "__future__":
            hit("undeclared-import", line, f"third-party module {m} isn't in toolbox.allow_imports")

    def resolved(fn: ast.AST) -> str:
        name = _dotted(fn)
        head, _, rest = name.partition(".")
        base = aliases.get(head, head)
        return f"{base}.{rest}" if rest else base

    in_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = resolved(node.func)
            last = name.rsplit(".", 1)[-1]
            if name in DYN_CALLS or (isinstance(node.func, ast.Name) and node.func.id in DYN_CALLS):
                hit("dynamic-code", node, f"calls {last}()")
            elif name.startswith("os.") and last in OS_PROC:
                hit("process", node, f"calls {name}()")
            elif name in ("asyncio.open_connection", "asyncio.start_server", "asyncio.create_subprocess_exec",
                          "asyncio.create_subprocess_shell"):
                hit("network" if "connection" in name or "server" in name else "process", node, f"calls {name}()")
            elif name in ("getattr", "setattr") and len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                hit("dynamic-code", node, f"{name}() with a computed attribute name")
            elif last in DESTRUCTIVE and (name.startswith(("os.", "shutil.")) or isinstance(node.func, ast.Attribute)):
                if last not in ("replace", "rename", "move") or name.startswith(("os.", "shutil.")):
                    hit("destructive", node, f"calls {name or last}()")
            elif last in ("expanduser", "home") and ("path" in name or "Path" in name):
                hit("home-directory", node, f"calls {name}()")
            elif name in ("os.getenv", "os.environ.get", "os.environ.__getitem__", "os.putenv", "os.environ.pop") \
                    or (last == "get" and _dotted(getattr(node.func, "value", node)).endswith("environ")):
                arg = node.args[0] if node.args else None
                key = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
                if key is None:
                    hit("secrets", node, "reads an environment variable chosen at run time")
                elif WATCHED_VARS.search(key):
                    hit("secrets", node, f"reads environment variable {key}")
            elif last in ("b64decode", "b85decode", "a85decode", "decompress", "fromhex", "unhexlify") \
                    or (last == "decode" and node.args and isinstance(node.args[0], ast.Constant)
                        and str(node.args[0].value).lower() in ("rot13", "rot_13", "base64", "zlib", "hex")):
                hit("decoding", node, f"calls {name or last}()")
        elif isinstance(node, ast.Subscript) and _dotted(node.value).endswith("environ"):
            sl = node.slice
            key = sl.value if isinstance(sl, ast.Constant) and isinstance(sl.value, str) else None
            if key is None or WATCHED_VARS.search(key):
                hit("secrets", node, f"reads environment variable {key or '(computed)'}")
        elif isinstance(node, ast.Attribute) and node.attr in ("__globals__", "__code__", "__builtins__",
                                                               "__subclasses__", "__mro__", "f_globals", "f_back"):
            hit("dynamic-code", node, f"uses {node.attr}")
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            hit("dynamic-code", node, "uses __builtins__")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            s = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
            if WATCHED_PATHS.search(s):
                hit("secrets", node, f"mentions a credential location ({WATCHED_PATHS.search(s).group(0)})")
            elif len(s) >= 400 and BLOB.match(s.strip()):
                hit("obfuscation", node, f"a {len(s)}-character encoded blob")
            elif len(s) < 300 and ABS_PATH.match(s) and "\n" not in s and id(node) not in in_fstring:
                hit("absolute-path", node, f"hard-coded path {s[:60]}")
    if any(f["rule"] == "decoding" for f in findings) and any(f["rule"] == "dynamic-code" for f in findings):
        hit("obfuscation", None, "decodes data and runs dynamic code")
    findings.sort(key=lambda f: (f["rule"] not in HARD, f["line"], f["rule"]))
    rules = {f["rule"] for f in findings}
    verdict = "blocked" if rules & HARD else "review" if findings else "clean"
    third = sorted(m for m in imports if m and m not in STDLIB and m != "__future__")
    return {"verdict": verdict, "findings": findings, "imports": sorted(i for i in imports if i), "requires": third,
            "rules": RULES_VERSION}


def render_vet(path: str, v: Dict[str, Any]) -> str:
    out = [f"{path}: {v['verdict'].upper()}"]
    for f in v["findings"][:30]:
        sev = "block" if f["rule"] in HARD else "look"
        out.append(f"  [{sev}] line {f['line']}: {f['rule']}: {f['detail']}")
    if len(v["findings"]) > 30:
        out.append(f"  ... and {len(v['findings']) - 30} more")
    if v.get("requires"):
        out.append(f"  requires: {', '.join(v['requires'])}")
    return "\n".join(out)


# --- finding reusable scripts ----------------------------------------------------------

def reusability(text: str, v: Dict[str, Any]) -> Tuple[int, List[str]]:
    """A 0-6 score of how ready a script is to share, with the reasons."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return 0, ["not valid Python"]
    score, why = 0, []
    if ast.get_docstring(tree):
        score += 1
        why.append("documented")
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_")]
    if len(defs) >= 2:
        score += 1
        why.append(f"{len(defs)} public functions")
    if "argparse" in v["imports"] or re.search(r"__name__\s*==\s*['\"]__main__['\"]", text):
        score += 1
        why.append("has a command line")
    if not any(f["rule"] == "absolute-path" for f in v["findings"]):
        score += 1
        why.append("no hard-coded paths")
    uses_random = re.search(r"\brandom\b|np\.random|default_rng|torch\.manual_seed|rng", text)
    if not uses_random or re.search(r"\bseed\b", text):
        score += 1
        why.append("seeded" if uses_random else "deterministic")
    lines = text.count("\n")
    if 15 <= lines <= 1500:
        score += 1
    else:
        why.append(f"{lines} lines")
    return score, why


def shape(text: str) -> str:
    """Structural fingerprint: the AST with names, strings and docstrings erased."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return ""
    for node in ast.walk(tree):
        for field in ("name", "id", "arg", "attr", "asname"):
            if isinstance(getattr(node, field, None), str) and field != "attr":
                setattr(node, field, "_")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:] or [ast.Pass()]
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()[:16]


def _tokens(text: str) -> set:
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", re.sub(r"#.*", "", text))
    return {" ".join(toks[i:i + 3]) for i in range(max(0, len(toks) - 2))}


def candidates(root: Path, cfg: Dict[str, Any], files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Scripts worth promoting, and clusters of near-duplicates."""
    tb = cfg["toolbox"]
    lib = tb["path"].rstrip("/") + "/"
    paths = files if files is not None else _git_files(root)
    local = {Path(p).stem for p in paths if p.endswith(".py")} | {p.split("/")[-2] for p in paths
                                                                 if p.endswith("/__init__.py") and "/" in p}
    pool = []
    for rel in paths:
        if not rel.endswith(".py") or rel.startswith(lib) or _ignored(rel, tb["ignore"]):
            continue
        if not any(_glob(rel, g) for g in tb["scan"]):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        v = vet_source(text, list(tb["allow_imports"]) + sorted(local), rel)
        v["requires"] = [m for m in v["requires"] if m not in local]
        score, why = reusability(text, v)
        pool.append({"path": rel, "score": score, "why": why, "verdict": v["verdict"], "requires": v["requires"],
                     "shape": shape(text), "_tokens": _tokens(text), "refs": 0})
    stems = {Path(p["path"]).stem: p for p in pool}
    for rel in paths:  # used from elsewhere: imported, or named in docs and scripts
        if rel.endswith((".py", ".md", ".sh", ".yml", ".yaml", ".toml", ".json", ".txt")):
            try:
                body = (root / rel).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for stem, p in stems.items():
                s_ = re.escape(stem)
                if rel != p["path"] and re.search(rf"\b{s_}\.py\b|^\s*(from\s+\S*\b{s_}\s+import|import\s+(\S+\.)?{s_}\b)",
                                                  body, re.M):
                    p["refs"] += 1
    clusters: List[List[str]] = []
    seen = set()
    for i, a in enumerate(pool):
        if a["path"] in seen or not a["_tokens"]:
            continue
        group = [a["path"]]
        for b in pool[i + 1:]:
            if b["path"] in seen or not b["_tokens"]:
                continue
            same = a["shape"] and a["shape"] == b["shape"]
            sim = len(a["_tokens"] & b["_tokens"]) / max(1, len(a["_tokens"] | b["_tokens"]))
            if same or sim >= tb["duplicate_similarity"]:
                group.append(b["path"])
                seen.add(b["path"])
        if len(group) > 1:
            seen.add(a["path"])
            clusters.append(group)
    for p in pool:
        p.pop("_tokens")
    ready = sorted((p for p in pool if p["score"] >= tb["min_score"] and p["verdict"] != "blocked"),
                   key=lambda p: (-p["refs"], -p["score"], p["path"]))
    return {"candidates": ready, "duplicates": clusters, "scanned": len(pool)}


def _glob(path: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))


def _ignored(path: str, ignore: List[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any((g.endswith("/") and (path.startswith(g) or f"/{g}" in f"/{path}")) or _glob(path, g) or _glob(name, g)
               for g in ignore)


def _git_files(root: Path) -> List[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, capture_output=True,
                             text=True, check=True, timeout=60).stdout
        return [x for x in out.splitlines() if x]
    except (OSError, subprocess.SubprocessError):
        return [str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*.py") if ".git" not in p.parts]


# --- the library -----------------------------------------------------------------------

def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def lib_dir(root: Path, cfg: Dict[str, Any]) -> Path:
    return root / cfg["toolbox"]["path"].rstrip("/")


def read_manifest(text: str) -> Dict[str, Any]:
    m = json.loads(text)
    if not isinstance(m, dict) or not isinstance(m.get("tools"), dict):
        raise ValueError("toolbox.json needs a \"tools\" object")
    for name, t in m["tools"].items():
        if not (NAME_RE.match(name) and isinstance(t, dict) and HEX64.match(str(t.get("sha256", "")))
                and VERSION_RE.match(str(t.get("version", ""))) and re.match(r"^[a-z0-9_]+\.py$", str(t.get("file", "")))):
            raise ValueError(f"toolbox.json: entry {name!r} needs name, version x.y.z, file name.py and sha256")
    return m


def load_manifest(root: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    f = lib_dir(root, cfg) / MANIFEST
    if not f.is_file():
        return {"v": 1, "tools": {}}
    return read_manifest(f.read_text())


def check_library(root: Path, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every library entry, with whether its file still matches the pinned hash and vets."""
    m, d, rows = load_manifest(root, cfg), lib_dir(root, cfg), []
    for name, t in sorted(m["tools"].items()):
        f = d / t["file"]
        row = {"name": name, "version": t["version"], "file": str(f.relative_to(root)), "summary": t.get("summary", ""),
               "requires": t.get("requires", [])}
        if not f.is_file():
            row["status"] = "missing"
        elif sha256_file(f) != t["sha256"]:
            row["status"] = "hash mismatch"
        else:
            v = vet_source(f.read_text(encoding="utf-8"), cfg["toolbox"]["allow_imports"], t["file"])
            row["status"] = "ok" if v["verdict"] != "blocked" else "blocked by vetting"
        rows.append(row)
    return rows


def promote(root: Path, cfg: Dict[str, Any], src: str, name: str, version: str = "1.0.0", summary: str = "",
            commit: str = "") -> Dict[str, Any]:
    """Copy a vetted script into the library and pin it in the manifest. The PR needs a maintainer."""
    if not NAME_RE.match(name):
        raise ValueError("tool names are lowercase words joined by hyphens (2-48 characters)")
    if not VERSION_RE.match(version):
        raise ValueError("version must look like 1.0.0")
    p = (root / src).resolve()
    if not p.is_file() or p.suffix != ".py" or root.resolve() not in p.parents:
        raise ValueError(f"{src}: a Python file inside this repository")
    text = p.read_text(encoding="utf-8")
    v = vet_source(text, cfg["toolbox"]["allow_imports"], src)
    if v["verdict"] == "blocked":
        raise ValueError("vetting blocked it:\n" + render_vet(src, v))
    try:
        doc = (ast.get_docstring(ast.parse(text)) or "").strip().splitlines()
    except SyntaxError:
        doc = []
    d = lib_dir(root, cfg)
    d.mkdir(parents=True, exist_ok=True)
    fname = name.replace("-", "_") + ".py"
    m = load_manifest(root, cfg)
    old = m["tools"].get(name)
    if old and [int(x) for x in old["version"].split(".")] >= [int(x) for x in version.split(".")]:
        raise ValueError(f"{name} {old['version']} is already in the library: bump the version")
    if p != (d / fname).resolve():
        shutil.copyfile(p, d / fname)
    entry = {"version": version, "file": fname, "sha256": sha256_file(d / fname),
             "summary": (summary or (doc[0] if doc else ""))[:200], "requires": v["requires"],
             "source": {"path": src, **({"commit": commit} if commit else {})},
             "vet": {"verdict": v["verdict"], "rules": RULES_VERSION,
                     "look": sorted({f["rule"] for f in v["findings"]})}}
    m["tools"][name] = entry
    m.setdefault("v", 1)
    (d / MANIFEST).write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(LIB_README)
    return entry


LIB_README = """# Shared tools

Vetted, reusable scripts for this project and other Workstead projects.
`toolbox.json` pins each file's sha256. Changes need a maintainer's
approval (this is a protected path).

- `./ws tools list`: what's here, and whether each file matches its pin
- `./ws tools scan`: scripts elsewhere in the repository worth promoting
- `./ws tools promote PATH --name NAME`: add one (then submit as usual)
- `./ws tools find` / `./ws tools get NAME`: tools shared by trusted projects

See docs/toolbox.md in the Workstead template.
"""

# --- sharing ---------------------------------------------------------------------------

RAW = "https://raw.githubusercontent.com"
API = "https://api.github.com"


def _http(url: str, limit: int = 2_000_000) -> bytes:
    if not url.startswith(("https://raw.githubusercontent.com/", "https://api.github.com/")):
        raise ValueError("toolbox downloads only come from GitHub")
    # Unauthenticated on purpose: public reads need no token, and no credential ever goes near a download.
    req = urllib.request.Request(url, headers={"User-Agent": "workstead-toolbox", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec: fixed GitHub hosts
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than {limit} bytes")
    return data


def source_repos(cfg: Dict[str, Any], own_repo: str, upstream: str = "") -> List[str]:
    out = [own_repo] if own_repo else []
    for r in ([upstream] if upstream else []) + list(cfg["toolbox"]["sources"]):
        if r and r not in out:
            out.append(r)
    return out


def remote_manifest(repo: str, ref: str = "HEAD", fetch=_http) -> Dict[str, Any]:
    """A project's library at a pinned commit: {repo, commit, path, tools, publishers}."""
    if not re.match(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", repo):
        raise ValueError(f"{repo}: expected owner/name")
    commit = ref
    if not SHA40.match(ref):
        commit = json.loads(fetch(f"{API}/repos/{repo}/commits/{ref}"))["sha"]
    try:
        wj = json.loads(fetch(f"{RAW}/{repo}/{commit}/workstead.json"))
    except Exception:
        wj = {}
    path = str(((wj.get("toolbox") or {}).get("path")) or "tools/lib/").strip("/")
    if ".." in path.split("/"):
        raise ValueError(f"{repo}: bad toolbox.path")
    try:
        m = read_manifest(fetch(f"{RAW}/{repo}/{commit}/{path}/{MANIFEST}").decode())
    except Exception:
        m = {"tools": {}}
    pubs = [p for p in ((wj.get("relay") or {}).get("publishers") or []) if isinstance(p, str)]
    return {"repo": repo, "commit": commit, "path": path, "tools": m["tools"], "publishers": pubs}


def get(cfg: Dict[str, Any], cache: Path, name: str, repo: str, ref: str = "HEAD", fetch=_http) -> Dict[str, Any]:
    """Download a tool at a pinned commit, check its hash, vet it here, and cache it."""
    rm = remote_manifest(repo, ref, fetch)
    t = rm["tools"].get(name)
    if not t:
        raise ValueError(f"{repo} has no tool named {name} at {rm['commit'][:12]}")
    data = fetch(f"{RAW}/{repo}/{rm['commit']}/{rm['path']}/{t['file']}")
    if hashlib.sha256(data).hexdigest() != t["sha256"]:
        raise ValueError(f"{name}: the file doesn't match the sha256 pinned in {repo}'s toolbox.json; not saved")
    v = vet_source(data.decode("utf-8", errors="replace"), cfg["toolbox"]["allow_imports"] + t.get("requires", []),
                   t["file"])
    if v["verdict"] == "blocked":
        raise ValueError("vetting blocked it here:\n" + render_vet(t["file"], v))
    d = cache / repo.replace("/", "--") / name / t["version"]
    d.mkdir(parents=True, exist_ok=True)
    (d / t["file"]).write_bytes(data)
    rec = {"name": name, "repo": repo, "commit": rm["commit"], "file": t["file"], "sha256": t["sha256"],
           "version": t["version"], "requires": t.get("requires", []), "vet": v["verdict"]}
    (d / "receipt.json").write_text(json.dumps(rec, indent=2) + "\n")
    return {**rec, "path": str(d / t["file"]), "findings": v["findings"]}


def cached(cache: Path, name: str) -> List[Dict[str, Any]]:
    out = []
    for r in sorted(cache.glob(f"*/{name}/*/receipt.json")):
        try:
            rec = json.loads(r.read_text())
        except ValueError:
            continue
        out.append({**rec, "path": str(r.parent / rec["file"])})
    return out


# --- relay announcements (kind 7662) ---------------------------------------------------

def release_event(seckey: bytes, repo_url: str, name: str, entry: Dict[str, Any], commit: str, path: str,
                  created_at: int, ttl: int = 365 * 86400, pow_bits: int = 0) -> Dict[str, Any]:
    from . import nostr as N
    content = json.dumps({"summary": entry.get("summary", ""), "requires": entry.get("requires", [])},
                         sort_keys=True, separators=(",", ":"))
    tags = [N.APP_TAG, ["r", repo_url], ["d", name], ["v", entry["version"]], ["x", entry["sha256"]],
            ["commit", commit], ["path", f"{path.strip('/')}/{entry['file']}"]]
    return N.build(seckey, N.K_TOOL, tags, content, created_at, pow_bits=pow_bits, expires_at=created_at + ttl)


def release_ok(ev: Dict[str, Any]) -> bool:
    from . import nostr as N
    r = N.tag(ev, "r", "")
    p = N.tag(ev, "path", "")
    try:
        meta = json.loads(ev["content"])
    except ValueError:
        return False
    return (ev["kind"] == N.K_TOOL and bool(re.match(r"^https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", r))
            and bool(NAME_RE.match(N.tag(ev, "d", ""))) and bool(VERSION_RE.match(N.tag(ev, "v", "")))
            and bool(HEX64.match(N.tag(ev, "x", ""))) and bool(SHA40.match(N.tag(ev, "commit", "")))
            and bool(re.match(r"^[A-Za-z0-9._/-]{1,200}\.py$", p)) and ".." not in p.split("/")
            and isinstance(meta, dict) and len(ev["content"]) < 2000)


def releases(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest well-formed signed release per (repo, name). Whether a signer counts is checked on get."""
    from . import nostr as N
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for ev in events:
        if not (N.verify(ev) and release_ok(ev)):
            continue
        k = (N.tag(ev, "r"), N.tag(ev, "d"))
        if k not in best or ev["created_at"] > best[k]["created_at"]:
            best[k] = ev
    out = []
    for (r, name), ev in sorted(best.items()):
        meta = json.loads(ev["content"])
        out.append({"repo": r.split("github.com/", 1)[1], "name": name, "version": N.tag(ev, "v"),
                    "sha256": N.tag(ev, "x"), "commit": N.tag(ev, "commit"), "summary": meta.get("summary", ""),
                    "signer": ev["pubkey"]})
    return out


def signer_allowed(rel: Dict[str, Any], rm: Dict[str, Any]) -> bool:
    """A release counts when its own project's workstead.json lists the signing key as a publisher."""
    from .schnorr import parse_pubkey
    keys = set()
    for p in rm["publishers"]:
        try:
            keys.add(parse_pubkey(p))
        except ValueError:
            continue
    return rel["signer"] in keys


def announce(root: Path, cfg: Dict[str, Any], relays, seckey: bytes, repo: str, commit: str, now: int,
             already: Iterable[Dict[str, Any]] = ()) -> List[str]:
    """Publish a ToolRelease for every library entry the relays don't carry yet."""
    have = {(r["repo"], r["name"], r["sha256"]) for r in already}
    out = []
    for name, entry in sorted(load_manifest(root, cfg)["tools"].items()):
        if (repo, name, entry["sha256"]) in have:
            continue
        ev = release_event(seckey, f"https://github.com/{repo}", name, entry, commit,
                           cfg["toolbox"]["path"], now, pow_bits=cfg["relay"]["pow_bits"])
        relays.publish(ev)
        out.append(f"{name} {entry['version']}")
    return out


# --- running ---------------------------------------------------------------------------

SAFE_ENV = ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "OMP_NUM_THREADS",
            "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMBA_NUM_THREADS", "CUDA_VISIBLE_DEVICES",
            "HIP_VISIBLE_DEVICES", "PYTORCH_ENABLE_MPS_FALLBACK", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHASHSEED")


def scrubbed_env() -> Dict[str, str]:
    """No tokens, keys or credentials reach a tool: only what numerical code needs."""
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_command(cfg: Dict[str, Any], file: Path, args: List[str], workdir: Path) -> List[str]:
    if cfg["relay"]["verify_sandbox"] == "docker":
        return ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt",
                "no-new-privileges", "--read-only", "--tmpfs", "/tmp", "-v", f"{workdir}:/work", "-w", "/work",
                "-v", f"{file.parent}:/tool:ro", cfg["toolbox"]["image"] or cfg["relay"]["sandbox_image"],
                "python", "-I", "-B", f"/tool/{file.name}", *args]
    return [tool_python(cfg), "-I", "-B", str(file), *args]


def tool_python(cfg: Dict[str, Any], root: Optional[Path] = None) -> str:
    """toolbox.python (a project venv), resolved against the main checkout; else this interpreter."""
    p = cfg["toolbox"].get("python") or ""
    if not p:
        return sys.executable
    path = Path(p)
    if not path.is_absolute():
        try:
            common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    cwd=root or Path.cwd(), capture_output=True, text=True, check=True).stdout.strip()
            path = Path(common).parent / path
        except (OSError, subprocess.SubprocessError):
            path = (root or Path.cwd()) / path
    if os.name == "nt" and path.suffix != ".exe" and path.with_suffix(".exe").exists():
        path = path.with_suffix(".exe")
    if not path.exists():
        raise ValueError(f"toolbox.python {p} doesn't exist yet: set up the project's environment first")
    return str(path)


def show_context(root: Path, cfg: Dict[str, Any], limit: int = 12) -> List[str]:
    """A few lines for `ws show`: the shared tools, so agents reuse instead of rewriting."""
    try:
        tools = load_manifest(root, cfg)["tools"]
    except (OSError, ValueError):
        return []
    if not tools:
        return []
    lines = ["## Shared tools (use one before writing your own)", ""]
    for name, t in sorted(tools.items())[:limit]:
        lines.append(f"- `{name}` {t['version']}: {t.get('summary', '')[:120]}  (`{cfg['toolbox']['path']}{t['file']}`)")
    if len(tools) > limit:
        lines.append(f"- ... {len(tools) - limit} more: `ws tools list`")
    lines += ["", "Reusable code you write: mention it in your summary; `ws tools vet FILE` checks it.", ""]
    return lines


# --- command line ----------------------------------------------------------------------

def cmd_tools(ctx, args, home: Path) -> int:
    """ws tools scan|vet|promote|list|find|get|run|announce."""
    root, cfg = ctx.root, ctx.cfg
    cache = root / ".ws" / "tools"
    what = args.what
    if what == "vet":
        worst = 0
        for f in args.paths or []:
            v = vet_source(Path(f).read_text(encoding="utf-8", errors="replace"), cfg["toolbox"]["allow_imports"], f)
            print(json.dumps({"path": f, **v}) if args.json else render_vet(f, v))
            worst = max(worst, 2 if v["verdict"] == "blocked" else 0)
        return worst
    if what == "scan":
        res = candidates(root, cfg)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        print(f"Scanned {res['scanned']} scripts outside {cfg['toolbox']['path']}.")
        if res["candidates"]:
            print("\nWorth promoting (most used first):")
            for c in res["candidates"][:25]:
                note = "" if c["verdict"] == "clean" else "  (vetting: needs a look)"
                print(f"  {c['path']}  score {c['score']}/6, used by {c['refs']}{note}")
                print(f"      {', '.join(c['why'])}" + (f"; requires {', '.join(c['requires'])}" if c["requires"] else ""))
            print("\nPromote one: ./ws tools promote PATH --name NAME, then submit it in a task's pull request.")
        else:
            print("Nothing ready to promote.")
        for g in res["duplicates"]:
            print(f"\nNear-identical scripts (make them one tool): {', '.join(g)}")
        return 0
    if what == "promote":
        if not (args.paths and args.name):
            print("ws tools promote PATH --name NAME [--version 1.0.0] [--summary TEXT]", file=sys.stderr)
            return 1
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
        try:
            e = promote(root, cfg, args.paths[0], args.name, args.version or "1.0.0", args.summary or "", commit)
        except ValueError as err:
            print(f"ws: {err}", file=sys.stderr)
            return 1
        print(f"Added {args.name} {e['version']} to {cfg['toolbox']['path']} (vetting: {e['vet']['verdict']}).")
        print("Commit it in your task's worktree and submit. The library is protected: a maintainer approves it.")
        return 0
    if what == "list":
        rows = check_library(root, cfg)
        if args.json:
            print(json.dumps(rows, indent=2))
        for r in [] if args.json else rows:
            print(f"  {r['name']} {r['version']}  [{r['status']}]  {r['summary']}")
        if not rows and not args.json:
            print(f"No tools in {cfg['toolbox']['path']} yet. ./ws tools scan suggests some.")
        return 1 if any(r["status"] != "ok" for r in rows) else 0
    upstream = cfg["give_back"]["upstream"] or ""
    sources = source_repos(cfg, ctx.repo, upstream)
    if what == "find":
        q = (args.paths or [""])[0].lower()
        rows = []
        for repo in sources:
            try:
                rm = remote_manifest(repo)
            except Exception as e:
                print(f"  ({repo}: unavailable: {e})", file=sys.stderr)
                continue
            for name, t in sorted(rm["tools"].items()):
                rows.append({"repo": repo, "name": name, "version": t["version"], "summary": t.get("summary", ""),
                             "listed": True})
        if cfg["driver"] == "nostr" and not args.offline:
            try:
                from .driver import relays_for
                from . import nostr as N
                pool = relays_for(cfg, home, probe=False)
                try:
                    evs = pool.milestone.query([{"kinds": [N.K_TOOL], "limit": 500}])
                finally:
                    pool.close()
                known = {(r["repo"], r["name"]) for r in rows}
                for r in releases(evs):
                    if (r["repo"], r["name"]) not in known:
                        rows.append({**r, "listed": r["repo"] in sources})
            except Exception as e:  # relays unreachable: GitHub sources still listed
                print(f"  (relays unavailable: {e})", file=sys.stderr)
        rows = [r for r in rows if not q or q in r["name"] or q in r["summary"].lower()]
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        for r in rows:
            mark = "" if r["listed"] else "  [not in toolbox.sources]"
            print(f"  {r['repo']}  {r['name']} {r['version']}  {r['summary']}{mark}")
        if not rows:
            print("No shared tools found.")
        return 0
    if what == "get":
        if not args.paths:
            print("ws tools get NAME [--from owner/repo] [--ref COMMIT]", file=sys.stderr)
            return 1
        name = args.paths[0]
        repo = args.source or ""
        if not repo:
            for r in sources:
                try:
                    if name in remote_manifest(r)["tools"]:
                        repo = r
                        break
                except Exception:
                    continue
        if not repo:
            print(f"ws: no trusted project shares {name} (sources: {', '.join(sources)})", file=sys.stderr)
            return 1
        if repo not in sources:
            print(f"ws: {repo} isn't a trusted source. A maintainer adds it to toolbox.sources in workstead.json.",
                  file=sys.stderr)
            return 1
        try:
            rec = get(cfg, cache, name, repo, args.ref or "HEAD")
        except Exception as e:
            print(f"ws: {e}", file=sys.stderr)
            return 1
        print(f"{name} {rec['version']} from {repo}@{rec['commit'][:12]}: sha256 matches, vetting {rec['vet']}.")
        print(f"Saved to {rec['path']}. Run it with: ./ws tools run {name} -- ARGS")
        return 0
    if what == "run":
        if not args.paths:
            print("ws tools run NAME -- ARGS", file=sys.stderr)
            return 1
        name, extra = args.paths[0], list(args.paths[1:])
        m = load_manifest(root, cfg)["tools"].get(name)
        if m:
            f, pin = lib_dir(root, cfg) / m["file"], m["sha256"]
        else:
            got = cached(cache, name)
            if not got:
                print(f"ws: {name} isn't in the library or fetched; try ./ws tools get {name}", file=sys.stderr)
                return 1
            f, pin = Path(got[-1]["path"]), got[-1]["sha256"]
        if not f.is_file() or sha256_file(f) != pin:
            print(f"ws: {f} doesn't match its pinned sha256; refusing to run it", file=sys.stderr)
            return 1
        if cfg["relay"]["verify_sandbox"] != "docker":
            print("ws: running without a container (set relay.verify_sandbox: docker for no network); "
                  "tokens and keys are removed from its environment", file=sys.stderr)
        try:
            cmd = run_command(cfg, f, extra, Path.cwd())
        except ValueError as e:
            print(f"ws: {e}", file=sys.stderr)
            return 1
        return subprocess.run(cmd, env=scrubbed_env()).returncode
    if what == "announce":
        from .settle import gate_key_from_env
        from .driver import relays_for
        from . import nostr as N
        key = gate_key_from_env()
        if not key:
            print("ws: announcing needs the gate key (WS_NOSTR_KEY), so it runs in the sweep workflow", file=sys.stderr)
            return 1
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
        pool = relays_for(cfg, home, probe=False)
        try:
            seen = releases(pool.milestone.query([{"kinds": [N.K_TOOL], "#r": [f"https://github.com/{ctx.repo}"]}]))
            done = announce(root, cfg, pool, key, ctx.repo, commit, int(time.time()), seen)
        finally:
            pool.close()
        print("Announced: " + (", ".join(done) if done else "nothing new"))
        return 0
    print(f"ws tools: unknown action {what}", file=sys.stderr)
    return 1

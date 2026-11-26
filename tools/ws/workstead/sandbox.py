"""The sandbox: the only way Workstead runs project code (scripts, tools, checks).

AI agents write the code a fleet runs, so none of it runs directly on a
volunteer's machine. Two backends, lightest first:

**WebAssembly (default for Python scripts).** Pyodide (CPython compiled to
WebAssembly) inside Deno, offline. Deno grants read access to the Pyodide
cache and the job directory and write access to the job's output directory,
nothing else: no network, environment, subprocesses, FFI or system
information. Inside, Python sees an in-memory filesystem with /input
(read-only copies of the inputs), /output (copied back out at the end) and
/job (the script and its sibling modules); its JavaScript bridge holds
timers only. A watchdog kills the job at the memory limit or the wall-clock
limit. About a second to start and a little over 100 MB of memory; NumPy,
SciPy, pandas, scikit-learn and statsmodels are available as WebAssembly
builds, verified against the sha256 in Pyodide's lock file.

**A rootless container (fallback).** For scripts that need native
extensions Pyodide doesn't have (RDKit, PyTorch, Numba, libRoadRunner...)
and for the project's checks (which need git, node and the like):
`--network none --read-only --cap-drop ALL --security-opt no-new-privileges`,
a non-root user, cgroup memory, CPU and process limits, a size-limited
tmpfs, inputs mounted read-only, and a wall-clock limit. Podman (rootless by
design) is preferred; Docker works rootless, or inside the VM Docker Desktop
and OrbStack use on macOS and Windows. Rootful Docker on Linux is refused
unless `sandbox.allow_rootful` is set.

Nothing falls back to the host. When no backend can run a job, it fails and
says what to install.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PYODIDE_VERSION = "314.0.7"
PYODIDE_CORE_SHA256 = "2abdcc2e35208af406e07724cffa85bc582ced97e9028383ecf5462541393f95"
PYODIDE_CORE_URL = (f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE_VERSION}/"
                    f"pyodide-core-{PYODIDE_VERSION}.tar.bz2")
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
RUNNER = Path(__file__).with_name("sandbox_runner.mjs")
DEFAULT_IMAGE = "python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b"


class SandboxError(RuntimeError):
    """No sandbox can run this job; the message says what to install."""


def home() -> Path:
    return Path(os.environ.get("WS_HOME") or Path.home() / ".workstead")


def pyodide_dir() -> Path:
    return home() / "sandbox" / f"pyodide-{PYODIDE_VERSION}"


# --- setup (the only step that uses the network) -----------------------------------------

def _download(url: str, limit: int = 600 * 1024 * 1024) -> bytes:
    if not url.startswith(("https://github.com/pyodide/", "https://cdn.jsdelivr.net/pyodide/")):
        raise SandboxError(f"refusing to download from {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "workstead-sandbox"})
        with urllib.request.urlopen(req, timeout=120) as r:  # nosec: fixed hosts, content hash-checked
            data = r.read(limit + 1)
    except OSError as e:
        if not shutil.which("curl"):
            raise SandboxError(f"download failed: {e}") from None
        data = subprocess.run(["curl", "-fsSL", "--max-time", "300", url], capture_output=True, check=True).stdout
    if len(data) > limit:
        raise SandboxError(f"{url}: too large")
    return data


def setup(packages: Iterable[str] = (), quiet: bool = False) -> Path:
    """Fetch Pyodide's core and the requested packages, every file checked against a pinned sha256."""
    d = pyodide_dir()
    if not (d / "pyodide-lock.json").is_file():
        data = _download(PYODIDE_CORE_URL)
        if hashlib.sha256(data).hexdigest() != PYODIDE_CORE_SHA256:
            raise SandboxError("Pyodide core doesn't match its pinned sha256; not installed")
        tmp = Path(tempfile.mkdtemp(dir=str(d.parent) if d.parent.exists() else None))
        d.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as t:
            for m in t.getmembers():
                name = m.name.replace("\\", "/")
                if not name.startswith("pyodide/") or ".." in name.split("/") or not (m.isfile() or m.isdir()):
                    continue
                target = tmp / name[len("pyodide/"):]
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(t.extractfile(m).read())  # type: ignore[union-attr]
        if d.exists():
            shutil.rmtree(d)
        tmp.rename(d)
        if not quiet:
            print(f"Pyodide {PYODIDE_VERSION} core installed in {d}")
    lock = json.loads((d / "pyodide-lock.json").read_text())["packages"]
    want = resolve(lock, packages)
    for name in want:
        info = lock[name]
        f = d / info["file_name"]
        if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == info["sha256"]:
            continue
        data = _download(PYODIDE_CDN + info["file_name"])
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise SandboxError(f"{info['file_name']} doesn't match the sha256 in pyodide-lock.json; not installed")
        f.write_bytes(data)
        if not quiet:
            print(f"  {name} {info['version']}")
    return d


def resolve(lock: Dict[str, Any], names: Iterable[str]) -> List[str]:
    """Packages plus their dependencies, by Pyodide package name or import name."""
    by_import = {imp: n for n, p in lock.items() for imp in p.get("imports", [])}
    out: List[str] = []
    todo = [by_import.get(n, n) for n in names]
    while todo:
        n = todo.pop()
        if n in out or n not in lock:
            continue
        out.append(n)
        todo.extend(lock[n].get("depends", []))
    return sorted(out)


def available_imports() -> Dict[str, str]:
    """Import name -> Pyodide package, for packages present in the local cache."""
    d = pyodide_dir()
    try:
        lock = json.loads((d / "pyodide-lock.json").read_text())["packages"]
    except (OSError, ValueError):
        return {}
    return {imp: n for n, p in lock.items() if (d / p["file_name"]).is_file() for imp in p.get("imports", [])}


# --- what can run here -------------------------------------------------------------------

def deno() -> Optional[str]:
    exe = shutil.which("deno")
    if not exe:
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"deno (\d+)\.", out)
    return exe if m and int(m.group(1)) >= 2 else None


def container_engine(cfg: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(executable, kind) for a container engine allowed here, or None. kind: rootless | vm | rootful."""
    want = cfg["sandbox"]["engine"]
    for name in (["podman", "docker"] if want == "auto" else [want]):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "info", "--format", "{{json .}}"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        info = r.stdout.lower()
        if name == "podman":
            kind = "rootless" if '"rootless":true' in info.replace(" ", "") else "rootful"
        elif "rootless" in info:
            kind = "rootless"
        elif platform.system() in ("Darwin", "Windows"):
            kind = "vm"  # Docker Desktop, OrbStack, Colima: the engine runs in a VM, not on the host
        else:
            kind = "rootful"
        if kind == "rootful" and not cfg["sandbox"]["allow_rootful"]:
            continue
        return exe, kind
    return None


def backends(cfg: Dict[str, Any]) -> Dict[str, Any]:
    eng = container_engine(cfg)
    return {"wasm": bool(deno() and (pyodide_dir() / "pyodide-lock.json").is_file()),
            "deno": deno(), "pyodide": str(pyodide_dir()) if (pyodide_dir() / "pyodide-lock.json").is_file() else None,
            "container": eng[1] if eng else None, "engine": eng[0] if eng else None}


def imports_of(text: str) -> List[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module.split(".")[0])
    return sorted(names)


def choose(script: Path, cfg: Dict[str, Any], prefer: str = "auto") -> str:
    """wasm when every import is in the standard library, beside the script or in Pyodide's cache; else container."""
    if prefer in ("wasm", "container"):
        return prefer
    from .toolbox import STDLIB
    local = {p.stem for p in script.parent.glob("*.py")}
    have = available_imports()
    missing = [m for m in imports_of(script.read_text(encoding="utf-8", errors="replace"))
               if m not in STDLIB and m not in local and m not in have and m != "__future__"]
    return "container" if missing else "wasm"


# --- limits ------------------------------------------------------------------------------

def _rss_mb(pid: int) -> float:
    """Resident memory of a process in MB (0 when unknown). Cheap: one syscall, no child process."""
    try:
        if sys.platform.startswith("linux"):
            pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
        if sys.platform == "darwin":
            import ctypes
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            buf = ctypes.create_string_buffer(1024)  # rusage_info_v2 and later fit; phys_footprint is at 72
            if lib.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(2), buf) == 0:
                return int.from_bytes(buf.raw[72:80], "little") / 2 ** 20
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                            ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            if h:
                try:
                    c = PMC()
                    c.cb = ctypes.sizeof(PMC)
                    if k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                        return max(c.WorkingSetSize, c.PagefileUsage) / 2 ** 20
                finally:
                    k32.CloseHandle(h)
    except (OSError, ValueError, AttributeError):
        return 0.0
    return 0.0


class Result:
    def __init__(self, code: int, stdout: str, stderr: str, backend: str, seconds: float, peak_mb: float,
                 killed: str = ""):
        self.code, self.stdout, self.stderr, self.backend = code, stdout, stderr, backend
        self.seconds, self.peak_mb, self.killed = seconds, peak_mb, killed

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.backend, "exit": self.code, "seconds": round(self.seconds, 3),
                "peak_mb": round(self.peak_mb, 1), "killed": self.killed or None}


def _supervise(argv: List[str], memory_mb: int, timeout: float, env: Optional[Dict[str, str]] = None,
               on_kill=None, watch_memory: bool = True) -> Tuple[int, str, str, float, float, str]:
    """Run argv with a wall-clock limit and (for the WebAssembly backend) a memory watchdog."""
    start = time.monotonic()
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out: List[bytes] = []
    err: List[bytes] = []
    readers = [threading.Thread(target=lambda s=s, b=b: b.append(s.read()), daemon=True)
               for s, b in ((p.stdout, out), (p.stderr, err))]
    for t in readers:
        t.start()
    peak, killed = 0.0, ""
    while p.poll() is None:
        if watch_memory:
            peak = max(peak, _rss_mb(p.pid))
            if peak > memory_mb:
                killed = f"memory limit ({memory_mb} MB)"
        if time.monotonic() - start > timeout:
            killed = f"time limit ({int(timeout)} s)"
        if killed:
            if on_kill:
                on_kill()
            p.kill()
            break
        time.sleep(0.05 if time.monotonic() - start < 2 else 0.2)
    p.wait()
    for t in readers:
        t.join(5)
    return (p.returncode if not killed else 137, b"".join(out).decode("utf-8", "replace"),
            b"".join(err).decode("utf-8", "replace"), time.monotonic() - start, peak, killed)


# --- jobs --------------------------------------------------------------------------------

def _copy_tree(src: Path, dst: Path, limit: int = 2 * 1024 ** 3) -> None:
    """Copy regular files only (no symlinks, devices or sockets), refusing anything over the limit."""
    total = 0
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [x for x in dirs if not (Path(root) / x).is_symlink() and x != ".git"]
        for f in files:
            p = Path(root) / f
            if p.is_symlink() or not p.is_file():
                continue
            total += p.stat().st_size
            if total > limit:
                raise SandboxError("inputs larger than 2 GB")
            t = dst / p.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, t)


def make_job(script: Path, inputs: Sequence[Path]) -> Path:
    """An ephemeral job directory: code/ (script and sibling modules), input/, output/."""
    job = Path(tempfile.mkdtemp(prefix="ws-job-"))
    for sub in ("code", "input", "output"):
        (job / sub).mkdir()
    for p in sorted(script.parent.glob("*.py")):
        if p.is_file() and not p.is_symlink() and p.stat().st_size < 5 * 1024 * 1024:
            shutil.copyfile(p, job / "code" / p.name)
    shutil.copyfile(script, job / "code" / script.name)
    for i in inputs:
        _copy_tree(i, job / "input" / i.name)
    if "," in str(job):
        raise SandboxError("temporary directory path contains a comma")
    return job


def run_python(script: Path, inputs: Sequence[Path], output: Path, argv: Sequence[str], cfg: Dict[str, Any],
               backend: str = "auto", memory_mb: Optional[int] = None, timeout: Optional[float] = None,
               packages: Sequence[str] = ()) -> Result:
    """Run one Python script in the sandbox. Inputs appear at /input/<name>; files written to /output/ land in output."""
    sb = cfg["sandbox"]
    memory_mb = int(memory_mb or sb["memory_mb"])
    timeout = float(timeout or sb["timeout_seconds"])
    script = script.resolve()
    kind = choose(script, cfg, backend)
    job = make_job(script, [Path(i).resolve() for i in inputs])
    try:
        if kind == "wasm":
            res = _run_wasm(job, script.name, list(argv), memory_mb, timeout, packages)
        else:
            res = _run_container_python(job, script.name, list(argv), memory_mb, timeout, cfg)
        output.mkdir(parents=True, exist_ok=True)
        _copy_tree(job / "output", output)
        return res
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _run_wasm(job: Path, entry: str, argv: List[str], memory_mb: int, timeout: float,
              packages: Sequence[str]) -> Result:
    exe = deno()
    if not exe:
        raise SandboxError("the WebAssembly sandbox needs Deno 2 or newer (https://deno.com); then run `ws sandbox setup`")
    pdir = pyodide_dir()
    if not (pdir / "pyodide-lock.json").is_file():
        raise SandboxError("Pyodide isn't installed: run `ws sandbox setup`")
    (job / "job.json").write_text(json.dumps({"entry": entry, "argv": argv, "packages": list(packages)}))
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")}
    env.update({"DENO_NO_UPDATE_CHECK": "1", "DENO_NO_PROMPT": "1", "NO_COLOR": "1",
                "DENO_DIR": str(home() / "sandbox" / "deno-cache")})
    cmd = [exe, "run", "--quiet", "--no-prompt", "--no-remote", "--no-npm", "--no-config", "--no-lock",
           "--cached-only", f"--v8-flags=--max-old-space-size={max(64, memory_mb // 2)}",
           f"--allow-read={pdir},{job}", f"--allow-write={job / 'output'}", str(RUNNER), str(pdir), str(job)]
    code, out, err, secs, peak, killed = _supervise(cmd, memory_mb, timeout, env)
    return Result(code, out, err, "wasm", secs, peak, killed)


def _container_base(exe: str, cfg: Dict[str, Any], memory_mb: int, name: str, network: bool = False) -> List[str]:
    sb = cfg["sandbox"]
    base = [exe, "run", "--rm", "--name", name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m", "--cpus", str(sb["cpus"]),
            "--tmpfs", f"/tmp:rw,size={int(sb['tmpfs_mb'])}m,mode=1777", "--ipc", "none",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONHASHSEED=0",
            "-e", f"OMP_NUM_THREADS={sb['cpus']}", "-e", f"OPENBLAS_NUM_THREADS={sb['cpus']}"]
    if not network:
        base += ["--network", "none"]
    if sb["gpus"] and platform.system() == "Linux" and os.path.basename(exe) == "docker":
        base += ["--gpus", "all"]
    return base


def _image(cfg: Dict[str, Any]) -> str:
    return cfg["sandbox"]["image"] or DEFAULT_IMAGE


def _run_container_python(job: Path, entry: str, argv: List[str], memory_mb: int, timeout: float,
                          cfg: Dict[str, Any]) -> Result:
    eng = container_engine(cfg)
    if not eng:
        raise SandboxError("this script needs native packages the WebAssembly sandbox doesn't have, and no rootless "
                           "container engine is available: install Podman (or Docker Desktop / OrbStack / rootless "
                           "Docker)")
    exe, kind = eng
    name = "ws-job-" + secrets.token_hex(6)
    cmd = _container_base(exe, cfg, memory_mb, name) + [
        "--user", "65534:65534", "-v", f"{job / 'code'}:/job:ro", "-v", f"{job / 'input'}:/input:ro",
        "-v", f"{job / 'output'}:/output:rw", "-w", "/job", _image(cfg), "python", "-I", "-B", f"/job/{entry}", *argv]
    os.chmod(job / "output", 0o777)  # the container's unprivileged user writes here; the directory is ephemeral
    code, out, err, secs, peak, killed = _supervise(
        cmd, memory_mb, timeout, None, on_kill=lambda: subprocess.run([exe, "kill", name], capture_output=True),
        watch_memory=False)
    return Result(code, out, err, f"container ({os.path.basename(exe)}, {kind})", secs, peak, killed)


def _git_common_dir(worktree: Path) -> Optional[Path]:
    try:
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(worktree),
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out)
    except (OSError, subprocess.SubprocessError):
        return None


def run_command(cmd: str, worktree: Path, cfg: Dict[str, Any], timeout: Optional[float] = None,
                network: bool = False, writable: bool = False) -> Result:
    """A shell command (the project's checks, a must-fail control) in a container.

    The worktree is mounted read-only and copied into a size-limited tmpfs, so
    the command can write as usual but nothing reaches the host. Git metadata
    is mounted read-only at its own path so `git` works inside. `writable`
    (the setup command only) mounts the worktree read-write instead, so
    installed dependencies stay for later checks.
    """
    eng = container_engine(cfg)
    if not eng:
        raise SandboxError("checks run only in a sandbox, and no rootless container engine is available here: "
                           "install Podman (or Docker Desktop / OrbStack / rootless Docker)")
    exe, kind = eng
    sb = cfg["sandbox"]
    worktree = worktree.resolve()
    name = "ws-chk-" + secrets.token_hex(6)
    base = _container_base(exe, cfg, int(sb["memory_mb"]), name, network=network)
    git_dir = _git_common_dir(worktree)
    mounts = ["-v", f"{git_dir}:{git_dir}:ro"] if git_dir and git_dir.exists() and worktree not in (git_dir.parents) \
        and git_dir.parent != worktree else []
    if os.name != "nt" and hasattr(os, "getuid"):
        user = ["--user", f"{os.getuid()}:{os.getgid()}"] if writable else ["--user", "65534:65534"]
    else:
        user = []
    env = ["-e", "GIT_OPTIONAL_LOCKS=0", "-e", "GIT_CONFIG_NOSYSTEM=1", "-e", f"WS_CMD={cmd}",
           "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*"]
    if writable:
        argv = base + user + mounts + env + ["-v", f"{worktree}:{worktree}:rw", "-w", str(worktree), _image(cfg),
                                             "sh", "-c", 'exec sh -c "$WS_CMD"']
    else:
        argv = base + user + mounts + env + [
            "-v", f"{worktree}:/src:ro", "--tmpfs", f"/work:rw,exec,size={int(sb['tmpfs_mb'])}m,mode=1777",
            "-w", "/work", _image(cfg), "sh", "-c", 'cp -R /src/. /work/ && exec sh -c "$WS_CMD"']
    code, out, err, secs, peak, killed = _supervise(
        argv, int(sb["memory_mb"]), float(timeout or sb["timeout_seconds"]), None,
        on_kill=lambda: subprocess.run([exe, "kill", name], capture_output=True), watch_memory=False)
    return Result(code, out, err, f"container ({os.path.basename(exe)}, {kind})", secs, peak, killed)


def features(cfg: Dict[str, Any]) -> List[str]:
    b = backends(cfg)
    return (["sandbox:wasm"] if b["wasm"] else []) + (["sandbox:container"] if b["container"] else [])


# --- command line ------------------------------------------------------------------------

def cmd_sandbox(ctx, args) -> int:
    cfg = ctx.cfg if ctx is not None else None
    from . import config as C
    cfg = cfg or C.load()
    if args.what == "setup":
        try:
            setup(list(cfg["sandbox"]["packages"]) + list(args.packages or []))
        except (SandboxError, subprocess.CalledProcessError) as e:
            print(f"ws: {e}", file=sys.stderr)
            return 1
        b = backends(cfg)
        print("WebAssembly sandbox: " + ("ready" if b["wasm"] else "needs Deno 2+ (https://deno.com)"))
        print("Container sandbox: " + (f"{b['engine']} ({b['container']})" if b["container"] else
                                       "none (install Podman, Docker Desktop or OrbStack for native packages and checks)"))
        return 0
    if args.what == "doctor":
        b = backends(cfg)
        print(json.dumps(b, indent=2) if args.json else
              f"wasm: {'ready' if b['wasm'] else 'not ready'} (deno {b['deno'] or 'missing'}, pyodide "
              f"{PYODIDE_VERSION if b['pyodide'] else 'missing: ws sandbox setup'})\n"
              f"container: {b['engine'] + ' (' + b['container'] + ')' if b['container'] else 'none'}")
        return 0 if (b["wasm"] or b["container"]) else 1
    if args.what == "run":
        if not args.paths:
            print("ws sandbox run SCRIPT [--input PATH]... [--output DIR] [-- ARGS]", file=sys.stderr)
            return 1
        script, rest = Path(args.paths[0]), list(args.paths[1:])
        if rest[:1] == ["--"]:
            rest = rest[1:]
        try:
            res = run_python(script, [Path(i) for i in (args.input or [])], Path(args.output or "sandbox-output"),
                             rest, cfg, args.backend, args.memory, args.timeout)
        except SandboxError as e:
            print(f"ws: {e}", file=sys.stderr)
            return 1
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        print(json.dumps({"sandbox": res.describe()}), file=sys.stderr)
        return res.code
    return 1

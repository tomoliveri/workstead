"""The sandbox: the only execution path for project code. Unit tests run anywhere; integration tests need Deno
(WebAssembly backend, after `ws sandbox setup`) or a container engine (WS_TEST_CONTAINER=1)."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import harness  # noqa: F401  (sets WS_HOME and friends)
from workstead import config as C, sandbox as SB

CFG = C.load(data={"repo": "a/b"})
WS = Path(__file__).resolve().parents[1] / "workstead"
HAVE_WASM = bool(SB.deno()) and (SB.pyodide_dir() / "pyodide-lock.json").is_file()


def script(text, name="main.py"):
    d = Path(tempfile.mkdtemp())
    (d / name).write_text(text)
    return d / name


class NothingRunsOnTheHost(unittest.TestCase):
    def test_no_shell_execution_in_workstead(self):
        """Project code reaches the machine only through sandbox.py: no shell=True, no os.system anywhere."""
        for f in WS.glob("*.py"):
            src = f.read_text()
            self.assertNotRegex(src, r"shell\s*=\s*True", f.name)
            self.assertNotRegex(src, r"os\.system\(|os\.popen\(", f.name)

    def test_checks_controls_setup_and_tools_use_the_sandbox(self):
        drv = (WS / "driver.py").read_text()
        self.assertIn("SB.run_command", drv)
        wrk = (WS / "worker.py").read_text()
        self.assertEqual(len(re.findall(r"SB\.run_command\(", wrk)), 2)  # setup and validation
        self.assertIn("SB.run_python", (WS / "toolbox.py").read_text())

    def test_runner_flags_deny_everything_by_default(self):
        runner = (WS / "sandbox_runner.mjs").read_text()
        self.assertIn("jsglobals: { setTimeout, clearTimeout, setInterval, clearInterval }", runner)
        src = (WS / "sandbox.py").read_text()
        cmd = src[src.index('cmd = [exe, "run"'):src.index("code, out, err, secs, peak, killed = _supervise(cmd")]
        for flag in ("--no-prompt", "--no-remote", "--no-npm", "--cached-only"):
            self.assertIn(flag, cmd)
        for forbidden in ("--allow-net", "--allow-env", "--allow-run", "--allow-ffi", "--allow-sys", "-A", "--allow-all"):
            self.assertNotIn(f'"{forbidden}', cmd)


class Choosing(unittest.TestCase):
    def test_backend_follows_the_imports(self):
        self.assertEqual(SB.choose(script("import json, math\n"), CFG), "wasm")
        self.assertEqual(SB.choose(script("import torch\n"), CFG), "container")
        s = script("import helper\n")
        (s.parent / "helper.py").write_text("x = 1\n")
        self.assertEqual(SB.choose(s, CFG), "wasm")  # a sibling module travels with the script
        self.assertEqual(SB.choose(script("import torch\n"), CFG, "wasm"), "wasm")  # an explicit choice wins

    def test_resolve_follows_dependencies(self):
        lock = {"scipy": {"depends": ["numpy"], "imports": ["scipy"]}, "numpy": {"depends": [], "imports": ["numpy"]},
                "scikit-learn": {"depends": ["scipy"], "imports": ["sklearn"]}}
        self.assertEqual(SB.resolve(lock, ["sklearn"]), ["numpy", "scikit-learn", "scipy"])


class ContainerFlags(unittest.TestCase):
    def capture(self, fn, *a, **kw):
        seen = {}
        old = (SB.container_engine, SB._supervise)
        SB.container_engine = lambda cfg: ("/usr/bin/podman", "rootless")
        SB._supervise = lambda argv, *x, **y: (seen.setdefault("argv", argv), (0, "", "", 0.1, 0.0, ""))[1]
        try:
            fn(*a, **kw)
        finally:
            SB.container_engine, SB._supervise = old
        return seen["argv"]

    def test_checks_container_is_locked_down(self):
        wt = Path(tempfile.mkdtemp())
        argv = self.capture(SB.run_command, "make test", wt, CFG)
        joined = " ".join(argv)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "--pids-limit 256", "--memory 2048m", "--memory-swap 2048m", "--ipc none",
                     f"{wt.resolve()}:/src:ro", "--user 65534:65534" if os.name != "nt" else "--rm"):
            self.assertIn(flag, joined)
        self.assertNotIn(":rw", joined.replace("/tmp:rw", "").replace("/work:rw", ""))
        self.assertIn("WS_CMD=make test", argv)

    def test_script_container_mounts_input_read_only(self):
        s = script("print(1)\n")
        old = SB.choose
        SB.choose = lambda *a, **k: "container"
        try:
            argv = self.capture(SB.run_python, s, [], Path(tempfile.mkdtemp()), ["--x"], CFG)
        finally:
            SB.choose = old
        joined = " ".join(argv)
        self.assertRegex(joined, r":/input:ro")
        self.assertRegex(joined, r":/job:ro")
        self.assertIn("--network none", joined)
        self.assertEqual(argv[-4:], ["-B", "/job/main.py", "--x"][-3:] and argv[-4:])

    def test_setup_may_use_the_network_but_still_in_a_container(self):
        argv = self.capture(SB.run_command, "npm ci", Path(tempfile.mkdtemp()), CFG, network=True, writable=True)
        self.assertNotIn("--network", argv)
        self.assertIn("--read-only", argv)

    def test_rootful_docker_on_linux_is_refused(self):
        self.assertFalse(CFG["sandbox"]["allow_rootful"])


class Limits(unittest.TestCase):
    def test_memory_reading_works_here(self):
        self.assertGreater(SB._rss_mb(os.getpid()), 1)

    def test_wall_clock_limit_kills(self):
        code, _, _, secs, _, killed = SB._supervise([sys.executable, "-c", "import time; time.sleep(30)"], 4096, 1.0)
        self.assertEqual(code, 137)
        self.assertIn("time limit", killed)
        self.assertLess(secs, 10)

    def test_memory_limit_kills(self):
        code, _, _, _, peak, killed = SB._supervise(
            [sys.executable, "-c", "b = []\nwhile True: b.append(bytearray(20 * 2**20))"], 200, 60.0)
        self.assertEqual(code, 137)
        self.assertIn("memory limit", killed)
        self.assertGreater(peak, 200)

    def test_setup_refuses_a_tampered_download(self):
        old = SB._download
        SB._download = lambda url, limit=0: b"not pyodide"
        home = os.environ.get("WS_HOME")
        os.environ["WS_HOME"] = tempfile.mkdtemp()
        try:
            with self.assertRaises(SB.SandboxError):
                SB.setup()
        finally:
            SB._download = old
            os.environ["WS_HOME"] = home or ""


@unittest.skipUnless(HAVE_WASM, "needs Deno and `ws sandbox setup`")
class WebAssembly(unittest.TestCase):
    def test_escape_attempts_fail_and_output_comes_back(self):
        inp = Path(tempfile.mkdtemp()) / "data.csv"
        inp.write_text("1,2,3\n")
        s = script('''import json, os, socket, subprocess, sys
r = {}
for n, f in {"net": lambda: socket.create_connection(("1.1.1.1", 80), 2),
             "write_input": lambda: open("/input/data.csv", "a").write("x"),
             "host": lambda: open("/etc/hosts").read(),
             "run": lambda: subprocess.run(["id"]),
             "js": lambda: __import__("js").Deno.readTextFile("/etc/hosts")}.items():
    try:
        f(); r[n] = "allowed"
    except BaseException as e:
        r[n] = "blocked"
json.dump({"r": r, "sum": sum(map(int, open("/input/data.csv").read().split(",")))}, open("/output/o.json", "w"))
sys.exit(4)
''')
        out = Path(tempfile.mkdtemp())
        res = SB.run_python(s, [inp], out, [], CFG, backend="wasm", timeout=120)
        self.assertEqual(res.code, 4, res.stderr)
        got = json.loads((out / "o.json").read_text())
        self.assertEqual(got["sum"], 6)
        self.assertEqual(set(got["r"].values()), {"blocked"}, got)
        self.assertEqual(inp.read_text(), "1,2,3\n")

    def test_limits_hold(self):
        res = SB.run_python(script("b = []\nwhile True: b.append(bytearray(50 * 2**20))\n"), [],
                            Path(tempfile.mkdtemp()), [], CFG, backend="wasm", memory_mb=400, timeout=120)
        self.assertIn("memory limit", res.killed)
        res = SB.run_python(script("while True: pass\n"), [], Path(tempfile.mkdtemp()), [], CFG, backend="wasm",
                            timeout=3)
        self.assertIn("time limit", res.killed)


@unittest.skipUnless(os.environ.get("WS_TEST_CONTAINER") == "1" and SB.container_engine(CFG), "set WS_TEST_CONTAINER=1")
class Container(unittest.TestCase):
    def test_no_network_and_read_only_input(self):
        inp = Path(tempfile.mkdtemp()) / "data.csv"
        inp.write_text("x\n")
        s = script('''import socket, json
r = {}
for n, f in {"net": lambda: socket.create_connection(("1.1.1.1", 80), 2),
             "write_input": lambda: open("/input/data.csv", "a").write("x"),
             "write_root": lambda: open("/etc/x", "w").write("x")}.items():
    try:
        f(); r[n] = "allowed"
    except OSError:
        r[n] = "blocked"
json.dump(r, open("/output/o.json", "w"))
''')
        out = Path(tempfile.mkdtemp())
        res = SB.run_python(s, [inp], out, [], CFG, backend="container", timeout=300)
        self.assertEqual(res.code, 0, res.stderr)
        self.assertEqual(set(json.loads((out / "o.json").read_text()).values()), {"blocked"})


if __name__ == "__main__":
    unittest.main()

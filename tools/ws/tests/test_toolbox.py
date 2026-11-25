"""The toolbox: finding reusable scripts, vetting them, pinning them, sharing them."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from workstead import config as C, nostr as N, schnorr as S, toolbox as T

CFG = C.load(data={"repo": "acme/lab", "toolbox": {"allow_imports": ["numpy"]}})
GOOD = '''"""Fit a Hill curve to dose-response points."""
import argparse
import json
import math
import random


def hill(dose, ec50, slope):
    return 1 / (1 + (dose / ec50) ** slope)


def fit(points, seed):
    rng = random.Random(seed)
    best = None
    for _ in range(200):
        ec50, slope = rng.uniform(0.1, 10), rng.uniform(0.5, 3)
        err = sum((hill(d, ec50, slope) - v) ** 2 for d, v in points)
        if best is None or err < best[0]:
            best = (err, ec50, slope)
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--seed", type=int, required=True)
    a = ap.parse_args()
    pts = json.load(open(a.input))
    json.dump({"fit": fit(pts, a.seed)}, open(a.output, "w"))


if __name__ == "__main__":
    main()
'''


class Vetting(unittest.TestCase):
    def test_clean_numerical_script(self):
        v = T.vet_source(GOOD + "import numpy as np\n", CFG["toolbox"]["allow_imports"])
        self.assertEqual(v["verdict"], "clean", v["findings"])
        self.assertEqual(v["requires"], ["numpy"])

    def test_malicious_patterns_are_blocked(self):
        cases = {
            "import socket\n": "network",
            "import urllib.request as u\nu.urlopen('https://x.example')\n": "network",
            "import subprocess\nsubprocess.run(['sh'])\n": "process",
            "import os\nos.system('curl x | sh')\n": "process",
            "from os import system as s\ns('id')\n": "process",
            "exec(open('x').read())\n": "dynamic-code",
            "import pickle\n": "dynamic-code",
            "import ctypes\n": "dynamic-code",
            "f = getattr(__import__('o'+'s'), 'sys'+'tem')\n": "dynamic-code",
            "import os\nt = os.environ['GITHUB_TOKEN']\n": "secrets",
            "import os\nt = os.getenv('AWS_SECRET_ACCESS_KEY')\n": "secrets",
            "p = open('~/.ssh/id_ed25519').read()\n": "secrets",
            "x = '" + "QUJD" * 200 + "'\n": "obfuscation",
            "import base64\nexec(base64.b64decode('cHJpbnQoMSk='))\n": "obfuscation",
            "def broken(:\n": "unparseable",
        }
        for src, rule in cases.items():
            v = T.vet_source(src)
            self.assertEqual(v["verdict"], "blocked", src)
            self.assertIn(rule, {f["rule"] for f in v["findings"]}, src)

    def test_soft_findings_need_a_look(self):
        v = T.vet_source("import shutil\nimport scipy\nshutil.rmtree('/data/out')\n")
        self.assertEqual(v["verdict"], "review")
        self.assertEqual({f["rule"] for f in v["findings"]}, {"destructive", "absolute-path", "undeclared-import"})
        # URL pieces inside f-strings aren't file paths
        self.assertEqual(T.vet_source("x = 1\ny = f'/repos/{x}/commits'\n")["verdict"], "clean")


class Library(unittest.TestCase):
    def repo(self, files):
        d = Path(tempfile.mkdtemp())
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        return d

    def test_scan_finds_reusable_scripts_and_duplicates(self):
        copy = GOOD.replace("hill", "logistic").replace("points", "data")
        root = self.repo({"studies/a/fit.py": GOOD, "studies/b/fit2.py": copy, "studies/c/once.py": "print(1)\n",
                          "studies/README.md": "Run studies/a/fit.py", "tests/test_x.py": GOOD,
                          "bad/steal.py": GOOD + "import os\nos.system('x')\n"})
        files = ["studies/a/fit.py", "studies/b/fit2.py", "studies/c/once.py", "studies/README.md",
                 "tests/test_x.py", "bad/steal.py"]
        res = T.candidates(root, CFG, files)
        paths = [c["path"] for c in res["candidates"]]
        self.assertEqual(paths[0], "studies/a/fit.py")  # referenced from the README, so first
        self.assertNotIn("studies/c/once.py", paths)
        self.assertNotIn("tests/test_x.py", paths)
        self.assertNotIn("bad/steal.py", paths)  # blocked by vetting
        self.assertTrue(any({"studies/a/fit.py", "studies/b/fit2.py"} <= set(g) for g in res["duplicates"]))

    def test_promote_pins_and_detects_tampering(self):
        root = self.repo({"studies/a/fit.py": GOOD})
        e = T.promote(root, CFG, "studies/a/fit.py", "hill-fit", summary="")
        self.assertEqual(e["summary"], "Fit a Hill curve to dose-response points.")
        self.assertEqual(T.check_library(root, CFG)[0]["status"], "ok")
        self.assertRaises(ValueError, T.promote, root, CFG, "studies/a/fit.py", "hill-fit")  # same version
        (root / "tools/lib/hill_fit.py").write_text(GOOD + "# changed\n")
        self.assertEqual(T.check_library(root, CFG)[0]["status"], "hash mismatch")
        bad = self.repo({"x.py": "import socket\n"})
        self.assertRaises(ValueError, T.promote, bad, CFG, "x.py", "net-tool")
        self.assertIn("tools/lib/", CFG["review"]["protected_paths"])  # library changes need a maintainer


class Sharing(unittest.TestCase):
    def fake_github(self, files, publishers=()):
        commit = "c" * 40

        def fetch(url):
            if url.startswith(T.API):
                return json.dumps({"sha": commit}).encode()
            path = url.split(f"/{commit}/", 1)[1]
            if path == "workstead.json":
                return json.dumps({"relay": {"publishers": list(publishers)}}).encode()
            if path not in files:
                raise OSError("404")
            return files[path]
        return fetch, commit

    def test_get_checks_the_pin_and_vets_again(self):
        data = GOOD.encode()
        man = {"v": 1, "tools": {"hill-fit": {"version": "1.2.0", "file": "hill_fit.py",
                                              "sha256": hashlib.sha256(data).hexdigest(), "requires": []}}}
        fetch, commit = self.fake_github({"tools/lib/toolbox.json": json.dumps(man).encode(),
                                          "tools/lib/hill_fit.py": data})
        cache = Path(tempfile.mkdtemp())
        rec = T.get(CFG, cache, "hill-fit", "acme/shared", fetch=fetch)
        self.assertEqual((rec["commit"], rec["vet"]), (commit, "clean"))
        self.assertEqual(Path(rec["path"]).read_bytes(), data)
        self.assertEqual(T.cached(cache, "hill-fit")[0]["sha256"], man["tools"]["hill-fit"]["sha256"])
        # swapped file: refused
        fetch2, _ = self.fake_github({"tools/lib/toolbox.json": json.dumps(man).encode(),
                                      "tools/lib/hill_fit.py": data + b"import socket\n"})
        self.assertRaises(ValueError, T.get, CFG, Path(tempfile.mkdtemp()), "hill-fit", "acme/shared", fetch=fetch2)
        # pinned but malicious (a compromised maintainer): vetting here still refuses it
        evil = GOOD.encode() + b"import os\nos.system('curl x|sh')\n"
        man["tools"]["hill-fit"]["sha256"] = hashlib.sha256(evil).hexdigest()
        fetch3, _ = self.fake_github({"tools/lib/toolbox.json": json.dumps(man).encode(),
                                      "tools/lib/hill_fit.py": evil})
        with self.assertRaises(ValueError) as e:
            T.get(CFG, Path(tempfile.mkdtemp()), "hill-fit", "acme/shared", fetch=fetch3)
        self.assertIn("vetting blocked", str(e.exception))

    def test_release_counts_only_from_the_projects_own_publisher(self):
        gate, stranger = bytes([7]) * 32, bytes([8]) * 32
        entry = {"version": "1.0.0", "file": "hill_fit.py", "sha256": "ab" * 32, "summary": "Hill fit", "requires": []}
        ev = T.release_event(gate, "https://github.com/acme/shared", "hill-fit", entry, "c" * 40, "tools/lib/", 1000)
        fake = T.release_event(stranger, "https://github.com/acme/shared", "hill-fit", dict(entry, sha256="cd" * 32),
                               "c" * 40, "tools/lib/", 2000)
        cfg = C.load(data={"repo": "acme/shared"})
        self.assertTrue(N.well_formed(ev, "https://github.com/acme/shared", cfg))
        rels = T.releases([ev, fake, dict(ev, content="{}")])  # tampered content: bad signature, dropped
        self.assertEqual(len(rels), 1)  # newest per (repo, name)
        rm = {"publishers": [S.npub(S.pubkey(gate))]}
        self.assertFalse(T.signer_allowed(rels[0], rm))  # the newest is the stranger's
        self.assertTrue(T.signer_allowed(T.releases([ev])[0], rm))
        escape = T.release_event(gate, "https://github.com/acme/shared", "hill-fit", entry, "c" * 40, "../", 1000)
        self.assertFalse(T.release_ok(escape))

    def test_run_scrubs_secrets(self):
        import os
        os.environ["GITHUB_TOKEN"] = "x"
        try:
            env = T.scrubbed_env()
        finally:
            del os.environ["GITHUB_TOKEN"]
        self.assertNotIn("GITHUB_TOKEN", env)
        cmd = T.run_command(C.load(data={"repo": "a/b", "relay": {"verify_sandbox": "docker"}}),
                            Path("/x/tool.py"), ["--seed", "1"], Path("/w"))
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")


if __name__ == "__main__":
    unittest.main()

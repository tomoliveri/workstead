// Workstead's WebAssembly sandbox: one Python job in Pyodide, inside Deno.
//
// Started by sandbox.py as:
//   deno run --no-prompt --no-remote --no-npm --no-config --cached-only
//            --allow-read=<pyodide>,<job> --allow-write=<job>/output  sandbox_runner.mjs <pyodide> <job>
//
// Deno grants nothing else: no network, no environment, no subprocesses,
// no FFI, no system information. Inside, Python sees only an in-memory
// filesystem: /input (copied in, read-only), /output (copied out at the end)
// and /job (the script and its sibling modules). Its JavaScript bridge
// (`import js`) holds timers and nothing else, so Python can't reach Deno's
// APIs even where Deno would allow them.
const [pyodideDir, jobDir] = Deno.args;
const sep = Deno.build.os === "windows" ? "\\" : "/";
const join = (...p) => p.join(sep);
const toUrl = (p) => "file://" + (Deno.build.os === "windows" ? "/" + p.replaceAll("\\", "/") : p);
const MAX_OUTPUT = 512 * 1024 * 1024;

const job = JSON.parse(await Deno.readTextFile(join(jobDir, "job.json")));
const enc = new TextEncoder();
const write = (fd, s) => { try { fd.writeSync(enc.encode(s)); } catch (_) { /* closed pipe */ } };

const { loadPyodide } = await import(toUrl(join(pyodideDir, "pyodide.mjs")));
const py = await loadPyodide({
  indexURL: pyodideDir + sep,
  lockFileURL: join(pyodideDir, "pyodide-lock.json"),
  stdin: () => null,
  stdout: (s) => write(Deno.stdout, s + "\n"),
  stderr: (s) => write(Deno.stderr, s + "\n"),
  jsglobals: { setTimeout, clearTimeout, setInterval, clearInterval },
  env: { HOME: "/tmp", PYTHONHASHSEED: "0", PYTHONDONTWRITEBYTECODE: "1" },
  checkAPIVersion: true,
});

// Copy a host directory into the in-memory filesystem.
async function copyIn(host, guest) {
  py.FS.mkdirTree(guest);
  for await (const e of Deno.readDir(host)) {
    const h = join(host, e.name), g = guest + "/" + e.name;
    if (e.isDirectory) await copyIn(h, g);
    else if (e.isFile) py.FS.writeFile(g, await Deno.readFile(h));
  }
}
await copyIn(join(jobDir, "input"), "/input");
await copyIn(join(jobDir, "code"), "/job");
py.FS.mkdirTree("/output");
py.FS.mkdirTree("/tmp");

// Only packages vetted into the local cache (sha256 from the lock) load; nothing is fetched.
const code = new TextDecoder().decode(py.FS.readFile("/job/" + job.entry));
if (job.packages.length) await py.loadPackage(job.packages, { messageCallback: () => {}, checkIntegrity: true });
await py.loadPackagesFromImports(code, { messageCallback: () => {}, checkIntegrity: true });

py.globals.set("WS_ARGV", py.toPy([job.entry, ...job.argv]));
const status = await py.runPythonAsync(`
import os, runpy, sys, traceback
for d in ("/input",):
    for root, dirs, files in os.walk(d):
        for f in files:
            os.chmod(os.path.join(root, f), 0o444)
sys.argv = list(WS_ARGV)
sys.path.insert(0, "/job")
os.chdir("/job")
try:
    runpy.run_path("/job/" + sys.argv[0], run_name="__main__")
    _ws_status = 0
except SystemExit as e:
    _ws_status = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
except BaseException:
    traceback.print_exc()
    _ws_status = 1
sys.stdout.flush(); sys.stderr.flush()
_ws_status
`);

// Copy /output out: regular files only, names kept inside the output directory, size-capped.
let total = 0;
async function copyOut(guest, host) {
  for (const name of py.FS.readdir(guest)) {
    if (name === "." || name === ".." || name.includes("/") || name.includes("\\") || name.includes(":")) continue;
    const g = guest + "/" + name, st = py.FS.lstat(g);
    if (py.FS.isDir(st.mode)) {
      await Deno.mkdir(join(host, name), { recursive: true });
      await copyOut(g, join(host, name));
    } else if (py.FS.isFile(st.mode)) {
      const data = py.FS.readFile(g);
      total += data.length;
      if (total > MAX_OUTPUT) throw new Error("output larger than 512 MB");
      await Deno.writeFile(join(host, name), data);
    }
  }
}
await copyOut("/output", join(jobDir, "output"));
Deno.exit(typeof status === "number" ? status : 1);

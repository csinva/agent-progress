#!/usr/bin/env python3
"""Lifecycle and robustness tests.

run_tests.py checks the CLI, test_units.py checks the internals; this checks the
things that only go wrong over time or across versions - the watcher's own
loop, job records written by an older build, log files that move under us, and
the installer.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_tqdm.py")
HOOKS = os.path.join(ROOT, "hooks")
spec = importlib.util.spec_from_file_location("agent_tqdm", ENGINE)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

FAILS = []
CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


def cli(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


cli("rm", "--all")
cfg = cc.load_config()

print("=== a job record from an older build still renders ===")
legacy = {"id": "old", "state": "running", "started": time.time() - 300}
for name, fn in [("estimate", lambda: cc.estimate(legacy, None, cfg)),
                 ("render_line", lambda: cc.render_line(legacy, cfg, width=100)),
                 ("job_visible", lambda: cc.job_visible(legacy, cfg)),
                 ("describe_monitor", lambda: cc.describe_monitor(legacy)),
                 ("tone_for", lambda: cc.tone_for(legacy)),
                 ("render_block", lambda: cc.render_block(legacy, cfg, 100))]:
    try:
        fn()
        ck("%s on a bare job record" % name, True)
    except Exception as ex:
        ck("%s on a bare job record" % name, False, "%s: %s" % (type(ex).__name__, ex))
empty = {}
try:
    cc.render_line(empty, cfg, width=80)
    ck("render_line on an empty record", True)
except Exception as ex:
    ck("render_line on an empty record", False, "%s: %s" % (type(ex).__name__, ex))

print()
print("=== the watcher's stall rule ===")
scratch = tempfile.mkdtemp(prefix="agent-tqdm-life-")
log = os.path.join(scratch, "quiet.log")
open(log, "w").write("starting up\n")
# a real, living process that will never print anything again
proc = subprocess.Popen(["/bin/sh", "-c", "sleep 20"], start_new_session=True)
with cc.state_rw() as st:
    st["jobs"]["quiet"] = {
        "id": "quiet", "state": "running", "started": time.time(), "updated": time.time(),
        "log": log, "pid": proc.pid, "unit": "it", "monitor": {"kind": "auto"},
        "samples": [], "log_offset": 0, "eta_end": time.time() + 7200,
    }
watcher = subprocess.Popen([sys.executable, ENGINE, "_watch", "quiet",
                            "--interval", "1", "--max-idle", "3"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(8)                                  # well past max-idle
state = json.loads(open(cc.STATE).read())["jobs"].get("quiet", {}).get("state")
ck("a live process that prints nothing is not called stalled",
   state == "running", "state=%s" % state)
proc.kill(); proc.wait()
time.sleep(9)                                  # let the watcher notice
state = json.loads(open(cc.STATE).read())["jobs"].get("quiet", {}).get("state")
ck("once it really dies the watcher notices", state != "running", "state=%s" % state)
try:
    watcher.wait(timeout=20)
    ck("the watcher exits when the job ends", True)
except subprocess.TimeoutExpired:
    watcher.kill()
    ck("the watcher exits when the job ends", False, "still running")
cli("rm", "--all")

print()
print("=== a log that is truncated under the watcher ===")
log2 = os.path.join(scratch, "rot.log")
open(log2, "w").write("Epoch 5/10\n" * 50)
job = {"log": log2, "log_offset": os.path.getsize(log2), "monitor": {"kind": "auto"}}
open(log2, "w").write("Epoch 9/10\n")          # truncated and rewritten
r = cc.monitor_reading(job, time.time())
ck("progress is re-read after truncation", r and r["step"] == 9, repr(r))

print()
print("=== update flags ===")
cli("start", "upd", "--eta", "1h", "--monitor", "time", "--no-watch")
cli("update", "upd", "--step", "5", "--total", "20", "--unit", "ep", "--quiet")
d = json.loads(cli("ls", "--json").stdout)[0]
ck("step and total applied", d["step"] == 5 and d["total"] == 20 and d["unit"] == "ep", str(d))
cli("update", "upd", "--pct", "150", "--quiet")
d = json.loads(cli("ls", "--json").stdout)[0]
ck("a percentage over 100 is clamped", d["percent"] == 100.0, str(d["percent"]))
cli("update", "upd", "--eta", "10m", "--reset-rate", "--quiet")
raw = json.loads(open(cc.STATE).read())["jobs"]["upd"]
ck("--reset-rate clears the samples", raw["samples"] == [], str(raw["samples"]))
r = cli("update", "upd", "--note", "", "--quiet")
ck("an empty note clears it", r.returncode == 0)
cli("rm", "--all")

print()
print("=== pruning old jobs ===")
with cc.state_rw() as st:
    st["jobs"]["ancient"] = {"id": "ancient", "state": "done", "started": 1,
                             "ended": time.time() - 96 * 3600, "samples": []}
    st["jobs"]["recent"] = {"id": "recent", "state": "done", "started": 1,
                            "ended": time.time() - 60, "samples": []}
with cc.state_rw() as st:
    pass                                     # a write triggers the prune
ids = list(json.loads(open(cc.STATE).read())["jobs"])
ck("a long-finished job is forgotten", "ancient" not in ids, str(ids))
ck("a recent one is kept", "recent" in ids, str(ids))
cli("rm", "--all")

print()
print("=== classification is not quadratic on a long command ===")
huge = "python3 " + "a" * 200000 + " --flag"
t = time.time()
cc.classify_command(huge, {}, cfg)
dt = time.time() - t
ck("a 200KB command classifies quickly", dt < 2.0, "%.2fs" % dt)
t = time.time()
cc.classify_command("python3 " + "x/" * 40000 + "train.py", {}, cfg)
ck("a deeply nested path classifies quickly", time.time() - t < 2.0,
   "%.2fs" % (time.time() - t))

print()
print("=== names with characters that are not ascii ===")
cli("start", "трейн", "--eta", "1h", "--monitor", "time", "--no-watch")
out = cli("ls", "--json").stdout
ck("a non-ascii name does not break the CLI", out.strip().startswith("["), out[:80])
ck("statusline renders it",
   subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                  capture_output=True, text=True).returncode == 0)
cli("rm", "--all")
shutil.rmtree(scratch, ignore_errors=True)

print()
print("=== config survives being read while written ===")
import threading
bad = []


def hammer_reads():
    for _ in range(400):
        c = cc.load_config(force=True)
        if c["bar_width"] not in (22, 30, 40):
            bad.append(c["bar_width"])


def hammer_writes():
    for i in range(30):
        cli("config", "--set", "bar_width=%d" % (30 if i % 2 else 40))


tr = threading.Thread(target=hammer_reads)
tw = threading.Thread(target=hammer_writes)
tr.start(); tw.start(); tr.join(); tw.join()
ck("no reader saw a half-written config", not bad, "saw %s" % bad[:5])
cli("config", "--reset")

print()
print("=== the installer round-trips ===")
fake = tempfile.mkdtemp(prefix="agent-tqdm-home-")
os.makedirs(os.path.join(fake, ".claude"))
settings = os.path.join(fake, ".claude", "settings.json")
json.dump({"model": "opus", "statusLine": {"type": "command", "command": "mine"}},
          open(settings, "w"))
env = dict(os.environ, HOME=fake)
script = os.path.join(ROOT, "scripts", "install-statusline.sh")
r = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
d = json.load(open(settings))
ck("install wires the statusline", "agent_tqdm" in json.dumps(d.get("statusLine")), str(d))
ck("install keeps other settings", d.get("model") == "opus", str(d))
ck("install backs the old file up",
   any(n.startswith("settings.json.bak-") for n in os.listdir(os.path.dirname(settings))))
ck("install warns before replacing an existing statusLine", "replacing" in r.stdout, r.stdout[-200:])
r2 = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
ck("installing twice is harmless", r2.returncode == 0 and
   "agent_tqdm" in json.dumps(json.load(open(settings)).get("statusLine")))
subprocess.run(["bash", script, "--uninstall"], capture_output=True, text=True, env=env)
d = json.load(open(settings))
ck("uninstall removes the statusline", "statusLine" not in d, str(d))
ck("uninstall keeps other settings", d.get("model") == "opus", str(d))
shutil.rmtree(fake, ignore_errors=True)

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

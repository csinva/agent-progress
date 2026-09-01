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
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
HOOKS = os.path.join(ROOT, "hooks")
# a state directory of this test run's own; must precede loading the engine,
# which reads AGENT_PROGRESS_HOME once at import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402

spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
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
scratch = tempfile.mkdtemp(prefix="agent-progress-life-")
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
print("=== only one watcher, however many sessions start at once ===")
import threading
sandbox.kill_watchers(cc)
time.sleep(1)
orph = "orph-" + sandbox.TAG
cli("start", orph, "--eta", "2h", "--monitor", "time", "--no-watch")
with cc.state_rw() as st:
    st["jobs"][orph]["watcher_pid"] = 999999      # as a reboot would leave it
th = [threading.Thread(target=lambda i=i: subprocess.run(
    [sys.executable, os.path.join(HOOKS, "inject_status.py"), "SessionStart"],
    input=json.dumps({"session_id": "s%d" % i}), capture_output=True, text=True))
    for i in range(5)]
[t.start() for t in th]; [t.join() for t in th]
time.sleep(1.5)
n = len(subprocess.run(["pgrep", "-f", "_watch " + orph],
                       capture_output=True, text=True).stdout.split())
ck("five concurrent revivals leave one watcher", n == 1, "%d watchers" % n)

cli("rm", "--all")
gone = False
for _ in range(20):
    time.sleep(1)
    if not subprocess.run(["pgrep", "-f", "_watch " + orph],
                          capture_output=True, text=True).stdout.split():
        gone = True
        break
ck("a watcher notices its job was removed within ~15s", gone, "still running")
sandbox.kill_watchers(cc)

print()
print("=== a pid that has been handed to something else ===")
cli("rm", "--all")
cli("run", "--name", "reused", "--eta", "1h", "--", "sh", "-c", "echo hi; sleep 2")
time.sleep(4)                       # it finishes, and writes its exit file
with cc.state_rw() as st:           # now the pid belongs to something very alive
    st["jobs"]["reused"]["pid"] = os.getpid()
    st["jobs"]["reused"]["state"] = "running"
    st["jobs"]["reused"]["ended"] = None
sandbox.kill_watchers(cc, jobs={"reused"})
time.sleep(0.5)
subprocess.Popen([sys.executable, ENGINE, "_watch", "reused"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
state = None
for _ in range(25):
    time.sleep(1)
    state = json.loads(open(cc.STATE).read())["jobs"]["reused"]["state"]
    if state != "running":
        break
ck("a finished job is not held open by a recycled pid", state == "done", str(state))
sandbox.kill_watchers(cc, jobs={"reused"})
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
print("=== a job handed to a scheduler ===")
sched = tempfile.mkdtemp(prefix="agent-progress-sched-")
os.makedirs(os.path.join(sched, "bin"))


def stub(name, body):
    f = os.path.join(sched, "bin", name)
    open(f, "w").write("#!/bin/sh\n" + body)
    os.chmod(f, 0o755)


stub("sbatch", 'JOB=4242\n'
     '( sleep 1; i=1; while [ $i -le 8 ]; do echo "Epoch $i/8" >> "$PWD/slurm-$JOB.out";'
     ' sleep 1; i=$((i+1)); done; echo "$FINAL" > "$PWD/.state-$JOB" ) >/dev/null 2>&1 &\n'
     'echo RUNNING > "$PWD/.state-$JOB"\necho "Submitted batch job $JOB"\n')
stub("sacct", 'for a in "$@"; do case "$a" in [0-9]*) J="$a";; esac; done\n'
     'cat "$PWD/.state-$J" 2>/dev/null || true\n')
stub("squeue", "exit 0")
stub("scontrol", "exit 1")
env = dict(os.environ, PATH=os.path.join(sched, "bin") + ":" + os.environ["PATH"])

ck("a submission command is recognised",
   cc.classify_command("sbatch train.sbatch", {}, cfg)["track"])
ck("the id is read out of what it prints",
   cc.detect_submission("Submitted batch job 4242") == ("slurm", "4242"),
   str(cc.detect_submission("Submitted batch job 4242")))
ck("other schedulers too",
   cc.detect_submission("Job <991> is submitted to default queue.") == ("lsf", "991"))
ck("a bare number is not a submission on its own",
   cc.detect_submission("10", "echo $((X*2))") == (None, None),
   str(cc.detect_submission("10", "echo $((X*2))")))
ck("but it is when qsub printed it",
   cc.detect_submission("1234.head", "qsub job.pbs") == ("pbs", "1234.head"),
   str(cc.detect_submission("1234.head", "qsub job.pbs")))

for label, final, want in [("that succeeds", "COMPLETED", "done"),
                           ("that is killed", "OUT_OF_MEMORY", "failed")]:
    cli("rm", "--all")
    for f in os.listdir(sched):
        if f.startswith((".state-", "slurm-")):
            os.remove(os.path.join(sched, f))
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "auto_track.py")],
                         input=json.dumps({"tool_name": "Bash", "session_id": "sch",
                                           "tool_input": {"command": "sbatch train.sbatch"}}),
                         capture_output=True, text=True).stdout
    wrapped = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
    t = time.time()
    subprocess.run(["/bin/sh", "-c", wrapped], capture_output=True, text=True,
                   cwd=sched, env=dict(env, FINAL=final))
    ck("submitting returns at once (%s)" % label, time.time() - t < 8,
       "%.1fs" % (time.time() - t))
    jobs = json.loads(cli("ls", "--json").stdout or "[]")
    ck("the queued job is tracked (%s)" % label,
       len(jobs) == 1 and jobs[0]["id"] == "slurm-4242", str([j["id"] for j in jobs]))
    cli("update", "slurm-4242", "--interval", "2s", "--quiet")
    sandbox.kill_watchers(cc, jobs={"slurm-4242"})
    time.sleep(0.5)
    subprocess.Popen([sys.executable, ENGINE, "_watch", "slurm-4242"], cwd=sched, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline, seen = time.time() + 150, None
    while time.time() < deadline:
        time.sleep(2)
        jobs = json.loads(cli("ls", "--json").stdout or "[]")
        if jobs and jobs[0]["state"] != "running":
            seen = jobs[0]
            break
    ck("it ends by itself, %s" % label, seen and seen["state"] == want,
       str(seen and seen["state"]))
    ck("progress came from the scheduler's log (%s)" % label,
       seen and (seen["step"] or 0) > 0, str(seen and seen.get("step")))
    sandbox.kill_watchers(cc, jobs={"slurm-4242"})

raw = json.loads(open(cc.STATE).read())["jobs"].get("slurm-4242", {})
ck("the scheduler's own word is kept", raw.get("scheduler_state") == "OUT_OF_MEMORY",
   str(raw.get("scheduler_state")))
with cc.state_rw() as st:
    st["inbox"] = []
cli("rm", "--all")
shutil.rmtree(sched, ignore_errors=True)

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
fake = tempfile.mkdtemp(prefix="agent-progress-home-")
os.makedirs(os.path.join(fake, ".claude"))
settings = os.path.join(fake, ".claude", "settings.json")
json.dump({"model": "opus", "statusLine": {"type": "command", "command": "mine"}},
          open(settings, "w"))
env = dict(os.environ, HOME=fake)
script = os.path.join(ROOT, "scripts", "install-statusline.sh")
r = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
d = json.load(open(settings))
ck("install wires the statusline", "agent_progress" in json.dumps(d.get("statusLine")), str(d))
ck("install keeps other settings", d.get("model") == "opus", str(d))
ck("install backs the old file up",
   any(n.startswith("settings.json.bak-") for n in os.listdir(os.path.dirname(settings))))
ck("install warns before replacing an existing statusLine", "replacing" in r.stdout, r.stdout[-200:])
r2 = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
ck("installing twice is harmless", r2.returncode == 0 and
   "agent_progress" in json.dumps(json.load(open(settings)).get("statusLine")))
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

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
import signal
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


cli("rm", "--all", "--force")
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
cli("rm", "--all", "--force")

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

cli("rm", "--all", "--force")
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
print("=== the inbox tells a finish from a death ===")
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []
cli("run", "--name", "wentwell", "--eta", "1h", "--", "sh", "-c", "echo the answer is 42")
cli("run", "--name", "wentbadly", "--eta", "1h", "--", "sh", "-c", "exit 9")
for _ in range(30):
    time.sleep(1)
    _jobs = json.loads(open(cc.STATE).read())["jobs"]
    if all(j.get("state") not in ("running", None) for j in _jobs.values()) and len(_jobs) == 2:
        break
_listing = cli("inbox").stdout
_kinds = {e.get("job"): e.get("kind") for e in json.loads(open(cc.STATE).read())["inbox"]}
ck("the one that finished is recorded as finished", _kinds.get("wentwell") == "done", str(_kinds))
ck("and the one that died as a crash", _kinds.get("wentbadly") == "crash", str(_kinds))
ck("the listing does not call a success a crash",
   "wentwell" in _listing and "status 0" not in _listing, _listing[:100])
ck("and still calls a death a death", "wentbadly" in _listing, _listing[:100])
_report = cli("inbox", "--drain").stdout
ck("a drained report carries the job's own output",
   "the answer is 42" in _report or "FINISHED" in _report, _report[:90])
sandbox.kill_watchers(cc)
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []

print()
print("=== an exit file that is not ours ===")
# Trusting any file called <log>.exit ended live jobs: a job attached to a log
# that already existed is not finished just because something once left a file
# beside it. Only an exit file this plugin wrote itself is news.
cli("rm", "--all", "--force")
watched = os.path.join(sandbox.HOME, "someone-elses.log")
open(watched, "w").write("step 1/10\n")
open(watched + ".exit", "w").write("0\n")
cli("start", "attached", "--eta", "1h", "--log", watched)
time.sleep(3)
j = json.loads(open(cc.STATE).read())["jobs"]["attached"]
ck("a stray .exit does not end an attached job", j["state"] == "running", j["state"])
ck("and the attached job records no exit file of its own", not j.get("exit_file"))
sandbox.kill_watchers(cc, jobs={"attached"})
cli("rm", "--all", "--force")

r = cli("exec", "--name", "deferred", "--after", "1",
        "--shell", "echo start; sleep 5; echo end")
ck("the deferred handoff still works", r.returncode == 0 and "Traceback" not in r.stderr,
   r.stderr.strip()[-70:])
handed = json.loads(open(cc.STATE).read())["jobs"].get("deferred", {})
ck("and the job it makes knows its own exit file", bool(handed.get("exit_file")))
state = None
for _ in range(25):
    time.sleep(1)
    state = json.loads(open(cc.STATE).read())["jobs"].get("deferred", {}).get("state")
    if state != "running":
        break
ck("and finishes by it", state == "done", str(state))
sandbox.kill_watchers(cc)
cli("rm", "--all", "--force")

print()
print("=== forgetting a job that is still running ===")
# Removing the record of a live job does not stop the work; it strips it of its
# watcher and its bar and leaves it running with nothing following it. That is
# almost never what a cleanup meant, and it is easy to type by accident.
cli("rm", "--all", "--force")
_marker = "agent-progress-alive-" + sandbox.TAG
cli("run", "--name", "alive", "--eta", "1h", "--",
    "sh", "-c", "exec sleep 60  # " + _marker)
cli("start", "corpse", "--eta", "1h", "--monitor", "time", "--no-watch")
with cc.state_rw() as st:
    st["jobs"]["corpse"]["pid"] = 999999          # a pid that is long gone
time.sleep(2)
out = cli("rm", "--all").stdout          # deliberately not --force: that is the point
left = sorted(json.loads(open(cc.STATE).read())["jobs"])
ck("rm --all keeps the job that is running", left == ["alive"], str(left))
ck("and says how many it kept", "still running" in out, out.strip()[:70])
ck("a record with no live process goes freely", "corpse" not in left)
r = cli("rm", "alive")
ck("naming it directly is refused too", r.returncode != 0)
ck("and the refusal says what to do instead",
   "cancel" in (r.stderr + r.stdout), (r.stderr + r.stdout).strip()[:70])
ck("--force forgets it", cli("rm", "alive", "--force").returncode == 0)
ck("and then it is gone", "alive" not in json.loads(open(cc.STATE).read())["jobs"])
sandbox.kill_watchers(cc)
subprocess.run(["pkill", "-f", _marker], capture_output=True)
cli("rm", "--all", "--force")

print()
print("=== a command cut short is not called done ===")
# The wrapper waits for its command now, so a caller with a timeout - which is
# every tool call - kills both when the time runs out. The command never gets to
# write its exit status, and reading that silence as success put a tick against
# a job that had been cut off mid-run.
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []
_p = subprocess.Popen([sys.executable, ENGINE, "exec", "--name", "cut", "--after", "1",
                       "--shell", "sleep 30"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True)
time.sleep(3)
_p.send_signal(signal.SIGTERM)           # what a tool timeout sends
try:
    _p.wait(timeout=20)
except subprocess.TimeoutExpired:
    _p.kill()
_j = {}
for _ in range(50):
    time.sleep(1)
    _j = json.loads(open(cc.STATE).read())["jobs"].get("cut", {})
    if _j.get("state") not in ("running", None):
        break
ck("a job whose command was killed is not called done", _j.get("state") == "failed",
   "%s (exit %s)" % (_j.get("state"), _j.get("exit_code")))
ck("and it says what happened to it", "killed" in (_j.get("note") or ""),
   repr(_j.get("note")))
_inbox = json.loads(open(cc.STATE).read()).get("inbox", [])
ck("and it is reported as a death, not a finish",
   [e.get("kind") for e in _inbox] == ["crash"], str([e.get("kind") for e in _inbox]))
sandbox.kill_watchers(cc)
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []

print()
print("=== a pid that has been handed to something else ===")
cli("rm", "--all", "--force")
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
cli("rm", "--all", "--force")

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
    cli("rm", "--all", "--force")
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
cli("rm", "--all", "--force")
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
cli("rm", "--all", "--force")

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
cli("rm", "--all", "--force")

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
cli("rm", "--all", "--force")
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

# ---------------------------------------------------------------- fast deaths
# A watcher notices a death on its next tick. A job that fails a second after
# it starts is therefore still marked running when the turn ends, so the turn
# ending has to look for itself - otherwise the one thing the person asked
# always to hear about arrives only after they have spoken again.
INJECT = os.path.join(HOOKS, "inject_status.py")


def reap_home():
    h = tempfile.mkdtemp(prefix="agent-progress-reap-")
    return h, dict(os.environ, AGENT_PROGRESS_HOME=h, AGENT_PROGRESS_NOTIFY="false")


def hook(env, event, sid, **extra):
    payload = dict({"session_id": sid, "stop_hook_active": False}, **extra)
    r = subprocess.run([sys.executable, INJECT, event], input=json.dumps(payload),
                       capture_output=True, text=True,
                       env=dict(env, CLAUDE_CODE_SESSION_ID=sid))
    said = ""
    if r.stdout.strip():
        try:
            said = json.loads(r.stdout).get("systemMessage", "")
        except ValueError:
            said = r.stdout
    return r.returncode, said


def records(home):
    st = json.load(open(os.path.join(home, "state.json")))
    return [j for j in st["jobs"].values() if isinstance(j, dict)]


home, renv = reap_home()
subprocess.run([sys.executable, ENGINE, "run", "--name", "quickfail", "--eta", "1h", "--",
                "sh", "-c", "sleep 1; exit 3"], capture_output=True,
               env=dict(renv, CLAUDE_CODE_SESSION_ID="reap1"))
time.sleep(2.5)
rc, said = hook(renv, "Stop", "reap1")
ck("a job that dies fast is reported on the same turn", "quickfail" in said, repr(said[:120]))
ck("reporting it does not block the turn", rc == 0, str(rc))
job = [j for j in records(home) if j.get("id") == "quickfail"]
ck("the fast death is written down as a failure",
   bool(job) and job[0].get("state") == "failed" and job[0].get("exit_code") == 3,
   str(job[:1]))
_rc, again = hook(renv, "Stop", "reap1")
ck("and is not reported a second time", "quickfail" not in again, repr(again[:120]))
sandbox.kill_watchers(cc)
subprocess.run(["pkill", "-f", os.path.join(home, "")], capture_output=True)
shutil.rmtree(home, ignore_errors=True)

home, renv = reap_home()
live = subprocess.Popen([sys.executable, ENGINE, "run", "--name", "stillgoing", "--eta", "1h",
                         "--", "sleep", "45"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=dict(renv, CLAUDE_CODE_SESSION_ID="reap2"))
time.sleep(3)
rc, said = hook(renv, "Stop", "reap2")
job = [j for j in records(home) if j.get("id") == "stillgoing"]
ck("a running job is not mistaken for a dead one",
   bool(job) and job[0].get("state") == "running", str(job[:1]))
ck("and nothing is said about it", said == "", repr(said[:80]))
rc, said = hook(renv, "UserPromptSubmit", "reap2", prompt="hello")
ck("a prompt does not end a running job either",
   [j for j in records(home) if j.get("id") == "stillgoing"][0].get("state") == "running")
for jr in records(home):
    for pid in (jr.get("pid"), jr.get("watcher_pid")):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, TypeError, ValueError):
            pass
sandbox.kill_watchers(cc)
shutil.rmtree(home, ignore_errors=True)

home, renv = reap_home()
for a in (0, 1):
    subprocess.run([sys.executable, ENGINE, "run", "--name", "own%d" % a, "--eta", "1h", "--",
                    "sh", "-c", "sleep 1; exit 4"], capture_output=True,
                   env=dict(renv, CLAUDE_CODE_SESSION_ID="reapA%d" % a))
time.sleep(2.5)
_rc, first = hook(renv, "Stop", "reapA0")
ck("reaping tells an agent about its own dead job",
   "own0" in first and "own1" not in first, repr(first[:120]))
_rc, second = hook(renv, "Stop", "reapA1")
ck("and the other agent still hears about its own",
   "own1" in second and "own0" not in second, repr(second[:120]))
sandbox.kill_watchers(cc)
shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------- the wrapper closing out
# The wrapper closes its own record when the command ends. When the state file
# is too busy to take that, the exit file must stay: it is the only proof of
# how the command ended, and without it the watcher can only read a dead pid
# as "killed" - a false obituary for a command that succeeded.
import fcntl

home, renv = reap_home()
renv = dict(renv, CLAUDE_CODE_SESSION_ID="busy1")
subprocess.run([sys.executable, ENGINE, "ls"], capture_output=True, env=renv)
wr = subprocess.Popen([sys.executable, ENGINE, "exec", "--name", "fine", "--after", "1",
                       "--shell", "sleep 2; echo ok"], stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, env=renv)
time.sleep(1.7)
held = open(os.path.join(home, ".lock"), "a+")
fcntl.flock(held, fcntl.LOCK_EX)
out, _err = wr.communicate()
time.sleep(4)
fcntl.flock(held, fcntl.LOCK_UN)
held.close()
ck("a busy state file does not change the command's result",
   wr.returncode == 0 and out.strip() == "ok", "%r %r" % (wr.returncode, out))
kept = [n for n in os.listdir(os.path.join(home, "logs")) if n.endswith(".exit")]
ck("the exit file is kept when the record could not be closed", bool(kept), str(kept))
deadline = time.time() + 30
while time.time() < deadline:
    rec = [j for j in records(home) if j.get("id") == "fine"]
    if rec and rec[0].get("state") not in cc.ACTIVE_STATES:
        break
    time.sleep(1)
rec = [j for j in records(home) if j.get("id") == "fine"]
ck("the watcher then records the true result",
   bool(rec) and rec[0].get("state") == "done" and rec[0].get("exit_code") == 0, str(rec[:1]))
_rc, said = hook(renv, "Stop", "busy1")
ck("and no false obituary is written", "SIGKILL" not in said and "killed" not in said, repr(said[:100]))
sandbox.kill_watchers(cc)
shutil.rmtree(home, ignore_errors=True)

# An interrupt is the caller's doing: the bar stops at once, the record says so,
# and nobody is told about it afterwards.
home, renv = reap_home()
renv = dict(renv, CLAUDE_CODE_SESSION_ID="int1")
wr = subprocess.Popen([sys.executable, ENGINE, "exec", "--name", "longone", "--after", "1",
                       "--shell", "sleep 40"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, env=renv)
deadline = time.time() + 20
while time.time() < deadline:
    if os.path.exists(os.path.join(home, "state.json")) and any(
            j.get("state") == "running" for j in records(home)):
        break
    time.sleep(0.2)
os.kill(wr.pid, signal.SIGINT)
rc = wr.wait(timeout=20)
rec = [j for j in records(home) if j.get("id") == "longone"]
ck("an interrupted wrapper exits the way a shell does", rc == 130, str(rc))
ck("its record is closed at once and says why",
   bool(rec) and rec[0].get("state") == "failed" and rec[0].get("exit_code") == 130
   and "SIGINT" in (rec[0].get("note") or ""), str(rec[:1]))
ck("its run files are gone", not os.listdir(os.path.join(home, "logs")),
   str(os.listdir(os.path.join(home, "logs"))))
_rc, said = hook(renv, "Stop", "int1")
ck("the cut-short job is reported as a death", "longone" in said and "SIGINT" in said,
   repr(said[:100]))
time.sleep(12)                              # long enough for the watcher to have looked too
_rc, again = hook(renv, "Stop", "int1")
ck("and only once", "longone" not in again, repr(again[:100]))
sandbox.kill_watchers(cc)
shutil.rmtree(home, ignore_errors=True)

# When a finished record is pruned, the wrapper's own files go with it; a
# `run` job's log is the user's and stays.
home, renv = reap_home()
logs = os.path.join(home, "logs")
os.makedirs(logs, exist_ok=True)
for n in ("auto.log", "auto.log.exit", "users.log"):
    open(os.path.join(logs, n), "w").write("x\n")
old = time.time() - 10 * 86400
json.dump({"version": 1, "jobs": {
    "auto": {"id": "auto", "state": "done", "ended": old, "auto_launched": True,
             "log": os.path.join(logs, "auto.log"),
             "exit_file": os.path.join(logs, "auto.log.exit")},
    "users": {"id": "users", "state": "done", "ended": old,
              "log": os.path.join(logs, "users.log"), "exit_file": None}},
    "sessions": {}, "inbox": []}, open(os.path.join(home, "state.json"), "w"))
subprocess.run([sys.executable, ENGINE, "start", "tick", "--pid", str(os.getpid()),
                "--no-watch"], capture_output=True, env=renv)   # a write runs the pruner
left = sorted(os.listdir(logs))
ck("pruning a wrapper's record removes its files", "auto.log" not in left and
   "auto.log.exit" not in left, str(left))
ck("but leaves a run job's log alone", "users.log" in left, str(left))
shutil.rmtree(home, ignore_errors=True)


# ------------------------------------------------------- the same shell
# Claude Code runs Bash commands with bash. The wrapper has to as well, or a
# command that works unwrapped breaks wrapped - `[[ ]]`, pipefail and arrays
# all fail under dash, which is /bin/sh on most Linux machines.
home, renv = reap_home()
bashism = "set -o pipefail; a=(x y); [[ ${#a[@]} == 2 ]] && false | true; echo rc=$?"
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "60", "--shell", bashism],
                   capture_output=True, text=True, env=renv)
ck("a wrapped command runs under bash", r.stdout.strip() == "rc=1", repr((r.returncode, r.stdout, r.stderr[-80:])))
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "60", "--shell",
                    "ps -o comm= -p $$"], capture_output=True, text=True, env=renv)
ck("and says so if asked", r.stdout.strip().endswith("bash"), repr(r.stdout))
subprocess.run([sys.executable, ENGINE, "run", "--name", "bashrun", "--", "sh", "-c", "true"],
               capture_output=True, env=renv)
r = subprocess.run([sys.executable, ENGINE, "run", "--name", "bashy", "--eta", "1m", "--",
                    "bash", "-c", "true"], capture_output=True, text=True, env=renv)
ck("run launches through the same shell", cc.USER_SHELL.endswith("bash"), cc.USER_SHELL)
sandbox.kill_watchers(cc)
shutil.rmtree(home, ignore_errors=True)


# The wrapper forwards output as bytes. Decoding each read separately turned
# a multi-byte character that straddled two reads into replacement characters.
home, renv = reap_home()
split = r"printf '\xe2\x80'; sleep 0.3; printf '\xa6 '; printf '\xe2\x96'; sleep 0.3; printf '\x88\n'"
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "60", "--shell", split],
                   capture_output=True, env=renv)
ck("a character split across two reads comes through whole",
   r.stdout == "\u2026 \u2588\n".encode("utf-8"), repr(r.stdout))
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "60", "--shell",
                    r"printf '\xff\xfe raw\n'"], capture_output=True, env=renv)
ck("bytes that are not utf-8 are passed through untouched",
   r.stdout == b"\xff\xfe raw\n", repr(r.stdout))
shutil.rmtree(home, ignore_errors=True)


print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

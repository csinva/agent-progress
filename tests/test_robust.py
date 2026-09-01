#!/usr/bin/env python3
"""Robustness tests: a state file that has been damaged.

The state file is a plain file that several processes write and anyone can
edit, and the statusline reads it many times a second. A value of the wrong
shape must cost the bar some information, never raise.
"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
# a state directory of this test run's own; must precede loading the engine,
# which reads AGENT_PROGRESS_HOME once at import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402

spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
STATE = cc.STATE
FAILS = []
CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


def sl(env=None):
    return subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                          capture_output=True, text=True, env=env)


def run(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


print("=== malformed state shapes ===")
shapes = {
    "jobs is a list":        {"jobs": []},
    "jobs is a string":      {"jobs": "nope"},
    "a job is a number":     {"jobs": {"a": 5}},
    "a job is a list":       {"jobs": {"a": [1, 2]}},
    "a job is null":         {"jobs": {"a": None}},
    "monitor is a string":   {"jobs": {"a": {"id": "a", "state": "running",
                                             "started": 1, "monitor": "auto"}}},
    "samples are garbage":   {"jobs": {"a": {"id": "a", "state": "running", "started": 1,
                                             "total": 10, "units": 2,
                                             "samples": ["x", None]}}},
    "started is a string":   {"jobs": {"a": {"id": "a", "state": "running",
                                             "started": "yesterday"}}},
    "inbox is a dict":       {"jobs": {}, "inbox": {"a": 1}},
    "top level is a list":   [],
}
for label, doc in shapes.items():
    open(STATE, "w").write(json.dumps(doc))
    r = sl()
    r2 = run("ls")
    ck("survives: %s" % label,
       r.returncode == 0 and "Traceback" not in r.stderr
       and r2.returncode == 0 and "Traceback" not in r2.stderr,
       (r.stderr + r2.stderr).strip().splitlines()[-1][:70] if (r.stderr or r2.stderr) else "")

print()
print("=== state directory that is actually a file ===")
open(STATE, "w").write('{"jobs":{}}')
# inside the sandbox, not a fixed name in /tmp: two runs at once raced over
# that name, and whichever lost left a directory behind, which then failed
# every later run on the machine until somebody deleted it by hand
tmpfile = os.path.join(sandbox.HOME, "not-a-dir")
open(tmpfile, "w").write("x")
env = dict(os.environ, AGENT_PROGRESS_HOME=tmpfile)
for cmd in (["ls"], ["run", "--name", "x", "--", "true"], ["statusline"]):
    r = subprocess.run([sys.executable, ENGINE] + cmd, input="{}",
                       capture_output=True, text=True, env=env)
    ck("%s explains a bad state directory" % cmd[0], "Traceback" not in r.stderr,
       r.stderr.strip()[-70:])
os.remove(tmpfile)

print()
print("=== many jobs ===")
doc = {"jobs": {}, "inbox": []}
now = time.time()
for i in range(500):
    doc["jobs"]["job%03d" % i] = {"id": "job%03d" % i, "state": "running",
                                  "started": now - 3600, "total": 100, "units": i % 100,
                                  "unit": "it", "samples": [[now-60, 1], [now, 2]],
                                  "eta_end": now + 600}
open(STATE, "w").write(json.dumps(doc))
t = time.time(); r = sl(); dt = time.time() - t
ck("500 jobs still render fast", dt < 1.0 and r.returncode == 0, "%.0f ms" % (dt * 1000))
ck("and the row budget still holds",
   len(r.stdout.strip().splitlines()) <= 4, "%d rows" % len(r.stdout.strip().splitlines()))
ck("ls --json copes with 500 jobs", run("ls", "--json").returncode == 0)
open(STATE, "w").write('{"jobs":{},"inbox":[]}')

print()
print("=== settings that change behaviour ===")
run("rm", "--all")
run("config", "--reset")
run("start", "vis", "--eta", "30s", "--monitor", "time", "--no-watch")
ck("a short job is hidden", sl().stdout.count("vis") == 0, sl().stdout[:60])
run("update", "vis", "--force-show", "--quiet")
ck("--force-show pins it", "vis" in sl().stdout)
run("rm", "--all")
run("config", "--set", "show_context_line=false")
ck("show_context_line=false leaves the line empty", sl().stdout.strip() == "",
   repr(sl().stdout[:60]))
run("config", "--reset")

run("start", "keep", "--eta", "2h", "--monitor", "time", "--no-watch")
with cc.state_rw() as st:
    st["jobs"]["keep"]["started"] = time.time() - 600   # it really did run a while
run("done", "keep")
ck("a job that ran a while lingers after finishing", "keep" in sl().stdout, sl().stdout[:60])
run("config", "--set", "keep_done_seconds=0")
ck("keep_done_seconds=0 drops it at once", "keep" not in sl().stdout, sl().stdout[:60])
run("config", "--reset")
run("rm", "--all")

env = dict(os.environ, AGENT_PROGRESS_BAR_WIDTH="8", AGENT_PROGRESS_STYLE="ascii")
run("start", "envjob", "--eta", "2h", "--monitor", "time", "--no-watch")
out = sl(env).stdout
ck("environment overrides beat the config file", "[" in out and "#" not in out.split("[")[0],
   repr(out[:70]))
run("config", "--set", "bar_width=40")
out2 = sl(env).stdout
ck("and still beat an explicit setting", cc.visible_len(out2.splitlines()[0]) <
   cc.visible_len(sl().stdout.splitlines()[0]), "env=%d file=%d" % (
       cc.visible_len(out2.splitlines()[0]), cc.visible_len(sl().stdout.splitlines()[0])))
run("config", "--reset")
run("rm", "--all")

print()
print("=== the context throttle lets go after its interval ===")
run("config", "--set", "context_min_interval_seconds=2")
run("start", "thr", "--eta", "2h", "--monitor", "time", "--no-watch")
STATUS = os.path.join(ROOT, "hooks", "inject_status.py")


def ask():
    return subprocess.run([sys.executable, STATUS, "UserPromptSubmit"],
                          input=json.dumps({"session_id": "thr"}),
                          capture_output=True, text=True).stdout.strip()


first, second = ask(), ask()
time.sleep(2.5)
third = ask()
ck("unchanged status is not resent immediately", first and not second,
   "first=%d second=%d" % (len(first), len(second)))
ck("but is resent once the interval passes", bool(third), "third=%d" % len(third))
run("config", "--reset")
run("rm", "--all")

print()
print("=== hostile input on stdin ===")
AUTO = os.path.join(ROOT, "hooks", "auto_track.py")
INJECT = os.path.join(ROOT, "hooks", "inject_status.py")
for label, payload in [("not json", "{{{"), ("a list", "[1,2]"), ("empty", ""),
                       ("null", "null"), ("a huge string", json.dumps({"x": "y" * 100000}))]:
    rs = [subprocess.run([sys.executable, AUTO], input=payload,
                         capture_output=True, text=True),
          subprocess.run([sys.executable, INJECT, "UserPromptSubmit"], input=payload,
                         capture_output=True, text=True),
          subprocess.run([sys.executable, ENGINE, "statusline"], input=payload,
                         capture_output=True, text=True)]
    ck("stdin is %s" % label,
       all(r.returncode == 0 and "Traceback" not in r.stderr for r in rs),
       " | ".join((r.stderr.strip().splitlines() or [""])[-1] for r in rs)[:70])

print()
print("=== the installer will not touch a settings.json it cannot read ===")
import shutil
import tempfile
fake = tempfile.mkdtemp()
os.makedirs(os.path.join(fake, ".claude"))
sp = os.path.join(fake, ".claude", "settings.json")
open(sp, "w").write("{ broken")
r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "install-statusline.sh")],
                   capture_output=True, text=True, env=dict(os.environ, HOME=fake))
ck("it explains itself instead of raising", "Traceback" not in r.stderr and r.returncode != 0,
   r.stderr.strip()[:70])
ck("and leaves the file alone", open(sp).read() == "{ broken")
shutil.rmtree(fake, ignore_errors=True)

print()
print("=== invariants ===")
with cc.state_rw() as st:
    st["inbox"] = [{"job": "j%d" % i, "ts": time.time(), "delivered": None} for i in range(80)]
    cc.enqueue_crash(st, {"id": "one-more", "exit_code": 1, "started": 1, "ended": 2}, time.time())
# Undelivered reports are kept up to a higher ceiling: several sessions share
# this queue, and trimming to the comfortable size on every append let a noisy
# session evict the one report a quiet session had not collected yet.
inbox = json.loads(open(STATE).read())["inbox"]
ck("the crash inbox stays bounded", len(inbox) <= cc.CRASH_CEILING, str(len(inbox)))
ck("and undelivered reports are the last to go",
   sum(1 for e in inbox if not e.get("delivered")) >= 50,
   "%d undelivered" % sum(1 for e in inbox if not e.get("delivered")))
with cc.state_rw() as st:
    st["inbox"] = []
r = run("exec", "--shell", "printf 'a\\000b\\377c\\n'")
ck("binary output does not crash exec", r.returncode == 0 and "Traceback" not in r.stderr)
r = run("exec", "--shell", "echo 'h\u00e9llo \U0001f680'")
ck("unicode survives exec", "\U0001f680" in r.stdout, repr(r.stdout[:30]))
import threading
with cc.state_rw() as st:
    st["inbox"] = [{"job": "q%d" % i, "ts": time.time(), "exit_code": 1,
                    "reason": "exited with status 1", "duration": 1, "delivered": None}
                   for i in range(5)]
claimed = []
lock = threading.Lock()


def claim(i):
    for _ in range(4):
        ev = cc.take_crash("sess%d" % i)
        if not ev:
            break
        with lock:
            claimed.append(ev["job"])


th = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
[t.start() for t in th]; [t.join() for t in th]
ck("every queued crash is claimed exactly once, under contention",
   sorted(claimed) == ["q0", "q1", "q2", "q3", "q4"], str(sorted(claimed)))
ck("nothing is left unclaimed",
   not [e for e in json.loads(open(STATE).read())["inbox"] if not e.get("delivered")])
with cc.state_rw() as st:
    st["inbox"] = []

before = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                        capture_output=True, text=True).stdout
run("start", "dirty", "--eta", "1h", "--monitor", "time", "--no-watch")
run("exec", "--shell", "echo hi")
sl()
run("rm", "--all")
after = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                       capture_output=True, text=True).stdout
ck("running jobs never write inside the repo", before == after, repr(after[:80]))

print()
print("=== an interrupted command is interrupted, not orphaned ===")
# The wrapped command runs in a session of its own so that it can be detached
# once it outlives the threshold. Until then a signal to the wrapper has to
# reach it, or killing the wrapper - a harness timeout, ctrl-c - leaves the
# real work running with nothing watching it, and the session relaunches it.
import signal as _signal
marker = os.path.join(sandbox.HOME, "orphan-marker")
for name, sig in (("SIGTERM", _signal.SIGTERM), ("SIGINT", _signal.SIGINT)):
    try:
        os.remove(marker)
    except OSError:
        pass
    p = subprocess.Popen(
        [sys.executable, ENGINE, "exec", "--after", "300s",
         "--shell", "sleep 6; touch %s" % marker],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1.5)
    p.send_signal(sig)
    try:
        rc = p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill(); rc = None
    ck("%s: the wrapper exits" % name, rc is not None, "still running")
    time.sleep(6)
    ck("%s: the command dies with it" % name, not os.path.exists(marker),
       "the command outlived the wrapper")
run("rm", "--all")

print()
print("=== a job id is only read out of a command that submits one ===")
ck("a bare number is not a queued pbs job",
   cc.detect_submission("10\n", "X=5; echo $((X*2))") == (None, None),
   str(cc.detect_submission("10\n", "X=5; echo $((X*2))")))
ck("nor is a count",
   cc.detect_submission("4172\n", "wc -l < data.txt") == (None, None))
# the fully-qualified id, suffix and all, because that is the form qstat wants
ck("qsub's reply still is",
   cc.detect_submission("77431.head\n", "qsub run.pbs") == ("pbs", "77431.head"),
   str(cc.detect_submission("77431.head\n", "qsub run.pbs")))
ck("so does sbatch's",
   cc.detect_submission("Submitted batch job 4242", "sbatch run.sh") == ("slurm", "4242"))
# no command but sbatch produces that sentence, so it is taken at its word
# wherever it appears - which is what keeps a wrapper around sbatch working
ck("and sbatch's sentence is trusted whatever produced it",
   cc.detect_submission("Submitted batch job 4242", "./submit.sh") == ("slurm", "4242"))
r = run("exec", "--shell", "X=5; echo $((X*2))")
ck("a command printing a number creates no job",
   r.stdout.strip() == "10" and json.loads(run("ls", "--json").stdout or "[]") == [],
   repr(r.stdout))
run("rm", "--all")

print()
print("=== the session id never recurses ===")
env = dict(os.environ)
env.pop("CLAUDE_CODE_SESSION_ID", None)
env.pop("CLAUDE_SESSION_ID", None)
r = subprocess.run([sys.executable, ENGINE, "start", "sessionless", "--eta", "1h",
                    "--monitor", "time", "--no-watch"],
                   capture_output=True, text=True, env=env)
ck("a job starts with no session in the environment",
   r.returncode == 0 and "RecursionError" not in r.stderr, repr(r.stderr[-200:]))
subprocess.run([sys.executable, ENGINE, "rm", "--all"],
               capture_output=True, env=env)

print()
print("=== the state lock is not waited on forever ===")
lf = open(os.path.join(sandbox.HOME, ".lock"), "a+")
fcntl.flock(lf, fcntl.LOCK_EX)
t = time.time()
r = subprocess.run([sys.executable, ENGINE, "start", "blocked", "--eta", "1h",
                    "--monitor", "time", "--no-watch"], capture_output=True, text=True,
                   env=dict(os.environ, AGENT_PROGRESS_LOCK_TIMEOUT="2"))
dt = time.time() - t
fcntl.flock(lf, fcntl.LOCK_UN); lf.close()
ck("a held lock gives up instead of hanging", dt < 12 and r.returncode != 0,
   "%.1fs rc=%s" % (dt, r.returncode))
ck("and says so in a sentence, not a traceback",
   "Traceback" not in r.stderr and "locked" in r.stderr, repr(r.stderr[-160:]))

lf = open(os.path.join(sandbox.HOME, ".lock"), "a+")
fcntl.flock(lf, fcntl.LOCK_EX)
t = time.time()
h = subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "auto_track.py")],
                   input=json.dumps({"tool_name": "Bash", "session_id": "s",
                                     "tool_input": {"command": "python train.py"}}),
                   capture_output=True, text=True,
                   env=dict(os.environ, AGENT_PROGRESS_AUTO_TRACK="instruct"))
dt = time.time() - t
fcntl.flock(lf, fcntl.LOCK_UN); lf.close()
ck("the pre-command hook returns well inside its own timeout", dt < 8,
   "%.1fs" % dt)
ck("and never fails the command it sits in front of",
   h.returncode == 0 and "Traceback" not in h.stderr, repr(h.stderr[-160:]))

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

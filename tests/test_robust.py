#!/usr/bin/env python3
"""Robustness tests: a state file that has been damaged.

The state file is a plain file that several processes write and anyone can
edit, and the statusline reads it many times a second. A value of the wrong
shape must cost the bar some information, never raise.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_tqdm.py")
spec = importlib.util.spec_from_file_location("agent_tqdm", ENGINE)
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
tmpfile = "/tmp/agent-tqdm-not-a-dir"
open(tmpfile, "w").write("x")
env = dict(os.environ, AGENT_TQDM_HOME=tmpfile)
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

env = dict(os.environ, AGENT_TQDM_BAR_WIDTH="8", AGENT_TQDM_STYLE="ascii")
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
ck("the crash inbox stays capped",
   len(json.loads(open(STATE).read())["inbox"]) <= 50,
   str(len(json.loads(open(STATE).read())["inbox"])))
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
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

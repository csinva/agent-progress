#!/usr/bin/env python3
"""Tests for agent-progress.

Exercises the parts that are easy to get quietly wrong: whether a wrapped
command still behaves exactly like the unwrapped one, whether the deferral
threshold is honoured, and whether the hooks stay out of the way. Runs the real
CLI and the real hooks against real processes - nothing is mocked.

    python3 tests/run_tests.py

Takes about a minute; it has to wait on actual commands. It writes to the real
state directory, so it clears its own jobs as it goes.
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import sys
import threading
import time

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "agent_progress.py")
HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
HOOK = os.path.join(HOOKS, "auto_track.py")
# a state directory of this test run's own; must precede loading the engine,
# which reads AGENT_PROGRESS_HOME once at import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402

_spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

FAILS = []


def cli(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


def ex(shell, after=None, extra=()):
    args = ["exec"] + (["--after", after] if after else []) + list(extra) + ["--shell", shell]
    return cli(*args)


def reset():
    cli("rm", "--all", "--force")
    cli("config", "--reset")


reset()
print("=== output fidelity ===")
r = ex("printf 'a\\nb\\nc\\n'")
ck("stdout exact", r.stdout == "a\nb\nc\n", repr(r.stdout))
r = ex("printf 'out\\n'; printf 'err\\n' >&2")
ck("stderr captured", "err" in r.stdout, repr(r.stdout))
r = ex("printf 'no trailing newline'")
ck("no trailing newline preserved", r.stdout == "no trailing newline", repr(r.stdout))
# octal, not \xNN: hex escapes are a bash extension, and /bin/sh is dash on
# Debian and Ubuntu, where printf would emit the backslashes literally
r = ex("printf 'caf\\303\\251 \\342\\234\\223\\n'")
ck("utf-8 passthrough", "café" in r.stdout and "✓" in r.stdout, repr(r.stdout))
raw = subprocess.run([sys.executable, ENGINE, "exec", "--shell", "printf 'a\\rb\\n'"],
                     capture_output=True)          # bytes: no newline translation
ck("carriage returns preserved", b"\r" in raw.stdout, repr(raw.stdout))
big = ex("i=1; while [ $i -le 5000 ]; do echo line-$i; i=$((i+1)); done")  # seq is not POSIX
ck("5000 lines intact", big.stdout.count("\n") == 5000 and "line-5000" in big.stdout,
   "lines=%d" % big.stdout.count("\n"))

print()
print("=== exit codes ===")
for code in (0, 1, 42, 127):
    r = ex("exit %d" % code)
    ck("exit %d" % code, r.returncode == code, "got %d" % r.returncode)
r = ex("kill -TERM $$")
ck("killed by signal -> 143", r.returncode == 143, "got %d" % r.returncode)

print()
print("=== shell semantics survive wrapping ===")
cases = [
    ("pipes",        "echo hello | tr a-z A-Z",            "HELLO"),
    ("and-chain",    "echo one && echo two",               "one\ntwo"),
    ("or-chain",     "false || echo fallback",             "fallback"),
    ("semicolons",   "echo a; echo b",                     "a\nb"),
    ("subshell",     "(cd /tmp && pwd)",                   "/tmp"),
    ("quotes",       "echo 'single' \"double\"",           "single double"),
    ("dollar",       "X=5; echo $((X*2))",                 "10"),
    ("redirect",     "echo hi > /dev/null; echo after",    "after"),
    ("glob",         "ls /nonexistent-xyz 2>/dev/null || echo noglob", "noglob"),
    ("backslash",    "printf 'a\\\\tb\\n'",                "a\\tb"),
]
for name, sh, want in cases:
    r = ex(sh)
    ck(name, r.stdout.strip() == want, "got %r want %r" % (r.stdout.strip(), want))

print()
print("=== nothing is left behind by a fast command ===")
logs = sandbox.LOGS
before = set(os.listdir(logs)) if os.path.isdir(logs) else set()
ex("echo transient")
after_files = set(os.listdir(logs)) if os.path.isdir(logs) else set()
ck("no log files leaked", before == after_files, str(after_files - before))
jobs = json.loads(cli("ls", "--json").stdout or "[]")
ck("no job created", len(jobs) == 0, "%d jobs" % len(jobs))

print()
print("=== the threshold ===")
t = time.time(); r = ex("sleep 1", after="10s"); dt = time.time() - t
ck("under threshold: waits for the command", 0.8 < dt < 4 and r.returncode == 0, "%.1fs" % dt)
# Past the threshold the command gets a bar and nothing else: it is not taken
# away, its output is not rerouted, and this call still ends when it ends.
t = time.time(); r = ex("sleep 4; echo THE-END", after="2s"); dt = time.time() - t
ck("over threshold: still waits for the command", 3.5 < dt < 12, "%.1fs" % dt)
ck("and its output still comes back", "THE-END" in r.stdout, repr(r.stdout[-60:]))
ck("and nothing is said about tracking", "agent-progress" not in r.stdout,
   repr(r.stdout[:90]))
jobs = json.loads(cli("ls", "--json").stdout or "[]")
ck("but it did get a job, so a bar could show", len(jobs) == 1, "%d jobs" % len(jobs))
ck("and the job is closed with the command's own result",
   jobs and jobs[0]["state"] == "done", str([j["state"] for j in jobs]))
cli("rm", "--all", "--force")

print()
print("=== --after 0 means track immediately, not never ===")
t = time.time(); r = ex("sleep 3", after="0"); dt = time.time() - t
jobs = json.loads(cli("ls", "--json").stdout or "[]")
ck("--after 0 gives it a bar at once", len(jobs) == 1, "%d jobs" % len(jobs))
ck("and still waits for the command", 2.5 < dt < 10, "%.1fs" % dt)
cli("rm", "--all", "--force")

print()
print("=== a job that crashes in front of the caller ===")
subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--name", "crasher",
                "--shell", "sleep 2; echo 'boom: out of memory' >&2; exit 137"],
               capture_output=True, text=True)
deadline = time.time() + 90
while time.time() < deadline:
    jobs = json.loads(cli("ls", "--json").stdout or "[]")
    if any(j["state"] != "running" for j in jobs):
        break
    time.sleep(1)
ck("crashed job recorded", any(j["state"] == "failed" for j in jobs), str([j["state"] for j in jobs]))
# Nothing is queued about it: the caller watched it fail and has its output and
# its exit code. A report is for work that outlived the call, not for this.
inbox = json.loads(cli("inbox", "--json").stdout or "[]")
pending = [e for e in inbox if not e.get("delivered")]
ck("nothing is queued about a job the caller watched fail",
   not pending, "%d pending" % len(pending))
cli("rm", "--all", "--force")

print()
print("=== hook behaviour ===")
def hook(cmd, session="s", **ti):
    p = {"tool_name": "Bash", "session_id": session, "tool_input": dict(ti, command=cmd)}
    out = subprocess.run([sys.executable, HOOK], input=json.dumps(p),
                         capture_output=True, text=True).stdout.strip()
    return json.loads(out) if out else None

cli("config", "--reset")
ck("defer wraps every time", all(hook("python train.py", "d1") for _ in range(3)))
ck("ordinary command untouched", hook("git status", "d1") is None)
ck("already-wrapped command untouched",
   hook("agent-progress exec --shell 'python train.py'", "d1") is None)
ck("non-Bash tool ignored",
   subprocess.run([sys.executable, HOOK],
                  input=json.dumps({"tool_name": "Edit", "tool_input": {}}),
                  capture_output=True, text=True).stdout.strip() == "")
w = hook("python train.py && echo done", "d2")
wrapped = w["hookSpecificOutput"]["updatedInput"]["command"]
ck("compound command passed as one string", wrapped.count("--shell") == 1
   and wrapped.rstrip().endswith("'"), wrapped)
r = subprocess.run(["/bin/sh", "-c", wrapped.replace("python train.py", "echo ran")],
                   capture_output=True, text=True)
ck("compound command runs whole", r.stdout.strip() == "ran\ndone", repr(r.stdout))

cli("config", "--set", "auto_track=instruct")
seen = [hook("python run_training.py", "i1") for _ in range(3)]
ck("instruct asks once, then relents",
   seen[0] and seen[0]["hookSpecificOutput"]["permissionDecision"] == "deny"
   and seen[1] is None and seen[2] is None,
   str([bool(x) for x in seen]))
cli("config", "--reset")


print("=== concurrent handoffs ===")
def launch(i):
    subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--name", "pytest",
                    "--shell", "sleep 8"], capture_output=True)
ts = [threading.Thread(target=launch, args=(i,)) for i in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
jobs = json.loads(cli("ls","--json").stdout or "[]")
ids = [j["id"] for j in jobs]
ck("4 concurrent handoffs -> 4 jobs", len(jobs) == 4, str(ids))
ck("ids are unique", len(set(ids)) == len(ids), str(ids))
ck("state file still valid JSON", isinstance(jobs, list))
logs = [j["log"] for j in jobs]
ck("each job has its own log", len(set(logs)) == len(logs), str(logs))

print()
print("=== statusline renders while jobs run ===")
sl = subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                    capture_output=True, text=True)
ck("statusline does not crash", sl.returncode == 0, sl.stderr[:80])

print()
print("=== removing a running job stops its watcher ===")
before = [j["watcher_pid"] for j in jobs if j.get("watcher_pid")]
cli("rm", "--all", "--force")
time.sleep(3)
alive = []
for pid in before:
    try:
        os.kill(pid, 0); alive.append(pid)
    except OSError:
        pass
ck("watchers exited after rm", not alive, "still alive: %s" % alive)

print()
print("=== backgrounded command takes the immediate path ===")
p = {"tool_name":"Bash","session_id":"bg","tool_input":{
     "command":"python train.py --epochs 5 && echo done","run_in_background":True}}
out = subprocess.run([sys.executable, HOOK], input=json.dumps(p),
                     capture_output=True, text=True).stdout
h = json.loads(out)["hookSpecificOutput"]["updatedInput"]
# One wrapper shape for everything. Detaching a command the caller had already
# backgrounded made this call return at once, so their background job looked
# finished the moment it started.
ck("a backgrounded command is wrapped the same way as any other",
   " exec " in h["command"], h["command"][:70])
ck("the caller's backgrounding is left as it was",
   h["run_in_background"] is True, str(h))
ck("compound command kept whole", h["command"].rstrip().endswith("'"), h["command"][-40:])
r = subprocess.run(["/bin/sh","-c", h["command"].replace("python train.py --epochs 5","echo ran")],
                   capture_output=True, text=True)
ck("both halves of the command ran, and came back to the caller",
   "ran" in r.stdout and "done" in r.stdout, repr(r.stdout[:60]))
ck("a command this quick still gets no job",
   not json.loads(cli("ls", "--json").stdout or "[]"),
   cli("ls", "--json").stdout[:60])
cli("rm", "--all", "--force")

print()
print("=== duration units on --after ===")
# The threshold decides when a bar appears, not when the call returns: the
# command is waited for either way.
for spec, lo, hi in (("1s", 0.5, 3.5), ("2s", 1.5, 4.5)):
    cli("rm", "--all", "--force")
    proc = subprocess.Popen([sys.executable, ENGINE, "exec", "--after", spec,
                             "--shell", "sleep 12"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t = time.time()
    while time.time() - t < hi + 4:
        if json.loads(cli("ls", "--json").stdout or "[]"):
            break
        time.sleep(0.2)
    dt = time.time() - t
    ck("--after %s gives it a bar at about the right moment" % spec, lo < dt < hi + 4,
       "%.1fs" % dt)
    ck("--after %s: and the call is still running the command" % spec,
       proc.poll() is None, "already returned")
    proc.kill()
    proc.wait()
    cli("rm", "--all", "--force")

print()
print("=== a corrupt state file does not break anything ===")
sp = sandbox.STATE
open(sp, "w").write("{not json at all")
ck("ls survives", cli("ls").returncode == 0)
ck("statusline survives", subprocess.run([sys.executable, ENGINE, "statusline"],
   input="{}", capture_output=True, text=True).returncode == 0)
ck("hook survives", subprocess.run([sys.executable, HOOK],
   input=json.dumps({"tool_name":"Bash","tool_input":{"command":"python train.py"}}),
   capture_output=True, text=True).returncode == 0)
r = cli("exec", "--shell", "echo recovered")
ck("exec still works", r.stdout.strip() == "recovered", repr(r.stdout))


print()
print("=== a broken install must not break the command ===")
import shutil
# A directory under a regular file can never be created, by anyone. chmod
# would be the obvious way to make the state directory unusable, but root
# ignores it, and tests get run as root in containers often enough that the
# check would quietly stop checking anything.
blocker = os.path.join(tempfile.mkdtemp(prefix="agent-progress-block-"), "a-file")
open(blocker, "w").write("not a directory")
ro = os.path.join(blocker, "state")
env = dict(os.environ, AGENT_PROGRESS_HOME=ro)
try:
    for label, sh, want in [("fast", "echo hello", 0),
                            ("failing", "echo oops >&2; exit 5", 5),
                            ("slow", "echo a; sleep 3; echo b", 0)]:
        r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--shell", sh],
                           capture_output=True, text=True, env=env)
        ck("unwritable state dir: %s command still runs" % label,
           r.returncode == want and "Traceback" not in r.stderr
           and (r.stdout + r.stderr).strip(),
           "exit=%s out=%r" % (r.returncode, (r.stdout + r.stderr)[:60]))
finally:
    shutil.rmtree(os.path.dirname(blocker), ignore_errors=True)

print()
print("=== a crash reaches exactly one session ===")
reset()
with cc.state_rw() as st:
    st["inbox"] = []                     # start from a known-empty queue
# `run`, not `exec`: a report is for work that outlived the call. A command the
# caller waited for has already handed them its output and its exit code.
subprocess.run([sys.executable, ENGINE, "run", "--name", "boom", "--eta", "1h",
                "--", "sh", "-c", "sleep 2; exit 9"], capture_output=True)
# Wait for the thing being delivered, not for the job to end. They happen in the
# same write, but on a busy machine the watcher can take its time getting there,
# and racing six sessions for a report that does not exist yet proves nothing.
deadline = time.time() + 120
while time.time() < deadline:
    if [e for e in json.loads(open(cc.STATE).read()).get("inbox", [])
            if e.get("job") == "boom" and not e.get("delivered")]:
        break
    time.sleep(1)
ck("the crash was queued before anyone raced for it",
   any(e.get("job") == "boom" for e in json.loads(open(cc.STATE).read()).get("inbox", [])),
   str([e.get("job") for e in json.loads(open(cc.STATE).read()).get("inbox", [])]))
# Count who was handed *this* crash, not who was handed any crash. On a busy
# machine another job can crash while these six are racing, and a second
# session claiming that one says nothing about whether this one was claimed
# twice - which is the property under test.
got = []


def grab(i):
    # a job's end goes beside the conversation now, so that is where delivery
    # is counted; the property is unchanged - one session, exactly once
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "inject_status.py"), "Stop"],
                       input=json.dumps({"session_id": "c%d" % i,
                                         "stop_hook_active": False}),
                       capture_output=True, text=True).stdout
    got.append("boom" in r)


th = [threading.Thread(target=grab, args=(i,)) for i in range(6)]
[t.start() for t in th]; [t.join() for t in th]
ck("a crash is delivered to exactly one session", sum(got) == 1,
   "delivered %d times" % sum(got))

reset()
print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

#!/usr/bin/env python3
"""Tests for agent-tqdm.

Exercises the parts that are easy to get quietly wrong: whether a wrapped
command still behaves exactly like the unwrapped one, whether the deferral
threshold is honoured, and whether the hooks stay out of the way. Runs the real
CLI and the real hooks against real processes - nothing is mocked.

    python3 tests/run_tests.py

Takes about a minute; it has to wait on actual commands. It writes to the real
state directory, so it clears its own jobs as it goes.
"""
import json
import os
import subprocess
import sys
import threading
import time

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "agent_tqdm.py")
HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
HOOK = os.path.join(HOOKS, "auto_track.py")
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
    cli("rm", "--all")
    cli("config", "--reset")


reset()
print("=== output fidelity ===")
r = ex("printf 'a\\nb\\nc\\n'")
ck("stdout exact", r.stdout == "a\nb\nc\n", repr(r.stdout))
r = ex("printf 'out\\n'; printf 'err\\n' >&2")
ck("stderr captured", "err" in r.stdout, repr(r.stdout))
r = ex("printf 'no trailing newline'")
ck("no trailing newline preserved", r.stdout == "no trailing newline", repr(r.stdout))
r = ex("printf 'caf\\xc3\\xa9 \\xe2\\x9c\\x93\\n'")
ck("utf-8 passthrough", "café" in r.stdout and "✓" in r.stdout, repr(r.stdout))
raw = subprocess.run([sys.executable, ENGINE, "exec", "--shell", "printf 'a\\rb\\n'"],
                     capture_output=True)          # bytes: no newline translation
ck("carriage returns preserved", b"\r" in raw.stdout, repr(raw.stdout))
big = ex("for i in $(seq 1 5000); do echo line-$i; done")
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
logs = os.path.expanduser("~/.claude/agent-tqdm/logs")
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
t = time.time(); r = ex("sleep 30", after="2s"); dt = time.time() - t
ck("over threshold: hands off promptly", 1.5 < dt < 5, "%.1fs" % dt)
ck("handoff message present", "agent-tqdm" in r.stdout and "tracked" in r.stdout, repr(r.stdout[:80]))
jobs = json.loads(cli("ls", "--json").stdout or "[]")
ck("handoff created exactly one job", len(jobs) == 1, "%d jobs" % len(jobs))
cli("rm", "--all")

print()
print("=== --after 0 means track immediately, not never ===")
t = time.time(); r = ex("sleep 20", after="0"); dt = time.time() - t
jobs = json.loads(cli("ls", "--json").stdout or "[]")
ck("--after 0 hands off at once", dt < 3 and len(jobs) == 1, "%.1fs, %d jobs" % (dt, len(jobs)))
cli("rm", "--all")

print()
print("=== a job that crashes after handoff is reported ===")
subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--name", "crasher",
                "--shell", "sleep 2; echo 'boom: out of memory' >&2; exit 137"],
               capture_output=True, text=True)
deadline = time.time() + 25
while time.time() < deadline:
    jobs = json.loads(cli("ls", "--json").stdout or "[]")
    if any(j["state"] != "running" for j in jobs):
        break
    time.sleep(1)
ck("crashed job recorded", any(j["state"] == "failed" for j in jobs), str([j["state"] for j in jobs]))
inbox = json.loads(cli("inbox", "--json").stdout or "[]")
pending = [e for e in inbox if not e.get("delivered")]
ck("crash queued for Claude", len(pending) >= 1, "%d pending" % len(pending))
ck("crash names the signal", any("SIGKILL" in (e.get("reason") or "") for e in pending),
   str([e.get("reason") for e in pending]))
cli("rm", "--all")

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
   hook("agent-tqdm exec --shell 'python train.py'", "d1") is None)
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
cli("rm", "--all")
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
ck("background uses run, not exec", " run " in h["command"], h["command"][:70])
ck("background flag cleared", h["run_in_background"] is False, str(h))
ck("compound command kept whole", h["command"].rstrip().endswith("'"), h["command"][-40:])
r = subprocess.run(["/bin/sh","-c", h["command"].replace("python train.py --epochs 5","echo ran")],
                   capture_output=True, text=True)
time.sleep(2)
log = json.loads(cli("ls","--json").stdout or "[]")
ck("backgrounded job was created", len(log) == 1, str(log))
out = cli("log", log[0]["id"], "-n", "5").stdout if log else ""
ck("both halves of the command ran", "ran" in out and "done" in out, repr(out))
cli("rm", "--all")

print()
print("=== duration units on --after ===")
for spec, lo, hi in (("1s", 0.5, 3.5), ("2s", 1.5, 4.5)):
    t = time.time()
    subprocess.run([sys.executable, ENGINE, "exec", "--after", spec, "--shell", "sleep 20"],
                   capture_output=True)
    dt = time.time() - t
    ck("--after %s" % spec, lo < dt < hi, "%.1fs" % dt)
    cli("rm", "--all")

print()
print("=== a corrupt state file does not break anything ===")
sp = os.path.expanduser("~/.claude/agent-tqdm/state.json")
open(sp, "w").write("{not json at all")
ck("ls survives", cli("ls").returncode == 0)
ck("statusline survives", subprocess.run([sys.executable, ENGINE, "statusline"],
   input="{}", capture_output=True, text=True).returncode == 0)
ck("hook survives", subprocess.run([sys.executable, HOOK],
   input=json.dumps({"tool_name":"Bash","tool_input":{"command":"python train.py"}}),
   capture_output=True, text=True).returncode == 0)
r = cli("exec", "--shell", "echo recovered")
ck("exec still works", r.stdout.strip() == "recovered", repr(r.stdout))


reset()
print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

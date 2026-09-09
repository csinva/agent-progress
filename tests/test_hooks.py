#!/usr/bin/env python3
"""Hook and statusline contract tests.

Claude Code reads these outputs, so their shape matters as much as their
content: a statusline that emits an extra line, or a Stop hook that blocks
twice, misbehaves in ways the CLI never would.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
HOOKS = os.path.join(ROOT, "hooks")
STATUS = os.path.join(HOOKS, "inject_status.py")
AUTO = os.path.join(HOOKS, "auto_track.py")
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


def hook(script, event, payload):
    return subprocess.run([sys.executable, script, event], input=json.dumps(payload),
                          capture_output=True, text=True).stdout.strip()


def statusline(payload):
    return subprocess.run([sys.executable, ENGINE, "statusline"], input=json.dumps(payload),
                          capture_output=True, text=True).stdout


cli("rm", "--all", "--force")
cli("config", "--reset")
with cc.state_rw() as st:
    st["inbox"] = []

print("=== the statusline stays inside its own lines ===")
cli("start", "one", "--eta", "2h", "--monitor", "time", "--no-watch",
    "--note", "first line\nsecond line\rthird")
out = statusline({})
ck("a note containing newlines does not add rows", out.count("\n") <= 1,
   "%d rows: %r" % (out.count("\n"), out[:120]))
cli("update", "one", "--desc", "a\nb", "--quiet")
ck("a description with newlines is contained",
   statusline({}).count("\n") <= 1, repr(statusline({})[:120]))
cli("rm", "--all", "--force")

for n in range(5):
    cli("start", "job%d" % n, "--eta", "2h", "--monitor", "time", "--no-watch")
out = statusline({})
ck("never more rows than max_jobs plus the overflow note",
   len([l for l in out.splitlines() if l.strip()]) <= 4,
   "%d rows" % len(out.splitlines()))
narrow = statusline({"terminal_width": 40})
ck("honours the terminal width",
   all(cc.visible_len(l) <= 40 for l in narrow.splitlines() if l.strip()),
   str([cc.visible_len(l) for l in narrow.splitlines()]))
ck("statusline emits no ansi when colour is off",
   "\033[" not in subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                                 capture_output=True, text=True,
                                 env=dict(os.environ, NO_COLOR="1")).stdout)
cli("rm", "--all", "--force")

print()
print("=== the Stop hook ===")
cli("config", "--reset")
# `run`, so the job genuinely outlives the call. A command the caller waited
# for needs no report - they watched it fail and have its exit code.
subprocess.run([sys.executable, ENGINE, "run", "--name", "dies", "--eta", "1h",
                "--", "sh", "-c", "sleep 2; exit 4"], capture_output=True)
deadline = time.time() + 120
while time.time() < deadline:
    jobs = json.loads(cli("ls", "--json").stdout or "[]")
    if jobs and jobs[0]["state"] != "running":
        break
    time.sleep(1)
# A turn ending is the user's turn to speak. News about a job goes beside the
# transcript, where it interrupts nobody, and never holds the turn open.
first = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": False})
_first = json.loads(first or "{}")
ck("a crash is put in front of the user when the turn ends",
   "systemMessage" in _first, first[:90])
ck("and it says which job and why",
   "dies" in _first.get("systemMessage", ""), _first.get("systemMessage", "")[:70])
ck("without holding the turn open", _first.get("decision") != "block", first[:90])
second = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": False})
ck("and only once", second == "", second[:100])
with cc.state_rw() as st:
    for e in st.get("inbox", []):
        e["delivered"] = None
guarded = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": True})
ck("never blocks when already inside a stop hook", guarded == "", guarded[:100])
cli("config", "--set", "crash_alert=false")
off = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": False})
ck("crash_alert=false silences the interruption", off == "", off[:100])
cli("config", "--reset")
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []

print()
print("=== SessionStart brings back a lost watcher ===")
cli("start", "orphan", "--eta", "2h", "--monitor", "time", "--no-watch")
with cc.state_rw() as st:
    st["jobs"]["orphan"]["watcher_pid"] = 999999      # a pid that cannot exist
    st["jobs"]["orphan"]["no_watch"] = False          # it had one; the flag only skipped the spawn above
hook(STATUS, "SessionStart", {"session_id": "s"})
time.sleep(2)
pid = json.loads(open(cc.STATE).read())["jobs"]["orphan"].get("watcher_pid")
ck("a dead watcher is replaced", pid != 999999 and cc.alive(pid), "watcher_pid=%s" % pid)
cli("rm", "--all", "--force")
time.sleep(2)

print()
print("=== a state directory whose path contains spaces ===")
spacey = os.path.join(tempfile.mkdtemp(prefix="agent progress "), "state dir")
env = dict(os.environ, AGENT_PROGRESS_HOME=spacey)
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--shell",
                    "echo hello from a spacey path"], capture_output=True, text=True, env=env)
ck("a fast command works from a path with spaces",
   r.returncode == 0 and "hello from a spacey path" in r.stdout, repr(r.stdout[:80]))
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--name", "spacey",
                    "--keep-log", "--shell", "echo a; sleep 3; echo b"],
                   capture_output=True, text=True, env=env)
ck("a tracked command works from a path with spaces too",
   r.returncode == 0 and "a" in r.stdout and "b" in r.stdout, repr(r.stdout[:80]))
jobs = json.loads(subprocess.run([sys.executable, ENGINE, "ls", "--json"],
                                 capture_output=True, text=True, env=env).stdout or "[]")
ck("and it got a job there, with a readable log",
   jobs and os.path.exists(jobs[0]["log"] or ""), str(jobs)[:90])
time.sleep(3)
shutil.rmtree(os.path.dirname(spacey), ignore_errors=True)

print()
print("=== reporting commands ===")
cli("rm", "--all", "--force")
cli("start", "running-one", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("start", "finished-one", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("done", "finished-one")
ck("ls --running shows only what is running",
   len(json.loads(cli("ls", "--json", "--running").stdout or "[]")) == 1)
d = json.loads(cli("show", "running-one", "--json").stdout or "{}")
ck("show --json carries the estimate", "estimate" in d and d.get("id") == "running-one",
   str(list(d)[:6]))
ck("inbox --limit is accepted", cli("inbox", "--limit", "2").returncode == 0)
cli("rm", "--all", "--force")

print()
print("=== a working directory that is not there ===")
for label, args in [("exec", ["exec", "--cwd", "/no/such/dir", "--shell", "echo hi"]),
                    ("run", ["run", "--name", "cw", "--cwd", "/no/such/dir", "--", "echo hi"])]:
    r = cli(*args)
    ck("%s reports a missing --cwd cleanly" % label,
       r.returncode != 0 and "Traceback" not in r.stderr and "no such directory" in r.stderr,
       r.stderr.strip()[-90:])
cli("rm", "--all", "--force")

print()
print("=== loading into a session that was already running ===")
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["sessions"] = {}

env_old = dict(os.environ, CLAUDE_CODE_SESSION_ID="was-already-open")
r = subprocess.run([sys.executable, ENGINE, "start", "s1", "--eta", "3h",
                    "--monitor", "time", "--no-watch"],
                   capture_output=True, text=True, env=env_old)
warned = "started before agent-progress was loaded" in r.stdout
ck("a job from an older session explains the missing bar",
   warned or not cc.statusline_wired(), r.stdout[-120:])
ck("doctor says which kind of session this is",
   "started BEFORE" in subprocess.run([sys.executable, ENGINE, "doctor"],
                                      capture_output=True, text=True,
                                      env=env_old).stdout or not cc.statusline_wired())
hook(STATUS, "SessionStart", {"session_id": "was-already-open"})
r2 = subprocess.run([sys.executable, ENGINE, "start", "s2", "--eta", "3h",
                     "--monitor", "time", "--no-watch"],
                    capture_output=True, text=True, env=env_old)
ck("and stops saying it once the session is known",
   "started before agent-progress was loaded" not in r2.stdout, r2.stdout[-120:])
ck("session_is_new is false for a recorded session",
   not cc.session_is_new("was-already-open"))
ck("session_is_new is true for one never seen", cc.session_is_new("never-seen-before"))
ck("no session id means no claim either way", not cc.session_is_new(None))
cli("rm", "--all", "--force")

print()
print("=== the wrapped command does not depend on PATH ===")
launcher = cc.launcher_prefix()
ck("the launcher is an absolute path", launcher.strip("'").startswith("/"), launcher)
wrapped = cc.wrap_command("echo ran", "t", after=20)
for label, path in [("without ~/.local/bin",
                     ":".join(d for d in os.environ["PATH"].split(":")
                              if not d.endswith("/.local/bin"))),
                    ("with no PATH at all", "")]:
    r = subprocess.run(["/bin/sh", "-c", wrapped], capture_output=True, text=True,
                       env=dict(os.environ, PATH=path))
    ck("a rewritten command still runs %s" % label,
       r.returncode == 0 and "ran" in r.stdout, "exit=%d %s" % (r.returncode, r.stderr[:50]))

print()
print("=== a long job asked for in words never blocks ===")
cli("rm", "--all", "--force")
cli("config", "--reset")
scratch = tempfile.mkdtemp(prefix="agent-progress-words-")
open(os.path.join(scratch, "train.py"), "w").write(
    "import time\nfor i in range(1, 40):\n    print('Epoch %d/40' % i, flush=True)\n"
    "    time.sleep(1)\n")

# the shapes a model actually picks when told "run training"
for label, cmd in [
        ("a script", "python3 train.py --epochs 40"),
        ("a module", "python3 -m src.train --config base.yaml"),
        ("a shell script", "bash scripts/train.sh"),
        ("a flag", "python3 main.py --mode train")]:
    out = hook(AUTO, "PreToolUse", {"tool_name": "Bash", "session_id": "words",
                                    "tool_input": {"command": cmd}})
    ck("caught when Claude runs %s" % label, bool(out), cmd)

# and the job really does come back long before it finishes
real = "python3 %s" % os.path.join(scratch, "train.py")
out = hook(AUTO, "PreToolUse", {"tool_name": "Bash", "session_id": "words",
                                "tool_input": {"command": real}})
wrapped = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
wrapped = wrapped.replace("--after 20", "--after 2")
# The threshold gives it a bar; it does not take the command away. The call
# runs the command to the end, as it would have without any of this.
proc = subprocess.Popen(["/bin/sh", "-c", wrapped],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
t = time.time()
appeared = None
while time.time() - t < 20:
    if json.loads(cli("ls", "--json").stdout or "[]"):
        appeared = time.time() - t
        break
    time.sleep(0.2)
ck("a bar appears at about the threshold", appeared is not None and appeared < 12,
   str(appeared))
ck("and the command is still the caller's, still running", proc.poll() is None,
   "the call had already returned")
proc.kill()
_out, _ = proc.communicate()
ck("and nothing was said to the caller about tracking",
   "agent-progress" not in _out, repr(_out[:90]))
cli("rm", "--all", "--force")

# the explicit route a model should prefer when it knows the job is long
t = time.time()
r = cli("run", "--name", "explicit", "--eta", "3h", "--", "sleep", "60")
ck("an explicit run returns at once", time.time() - t < 5, "%.1fs" % (time.time() - t))
d = json.loads(cli("ls", "--json").stdout or "[]")
ck("and its bar has an eta from the first frame",
   d and d[0]["remaining_s"] and d[0]["remaining_s"] > 3000, str(d and d[0].get("remaining_human")))
cli("cancel", "explicit")
cli("rm", "--all", "--force")
shutil.rmtree(scratch, ignore_errors=True)

print()
print("=== the wrapper never wraps itself ===")


def wrapped_form(cmd):
    out = hook(AUTO, "PreToolUse", {"tool_name": "Bash", "session_id": "recur",
                                    "tool_input": {"command": cmd}})
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"] if out else None


once = wrapped_form("python3 train.py")
ck("a real command is wrapped", bool(once), str(once))
ck("its wrapped form is not wrapped again", wrapped_form(once) is None, str(wrapped_form(once)))
for form in ["agent-progress ls",
             os.path.expanduser("~/.local/bin/agent-progress") + " exec --shell 'python3 train.py'",
             "sudo /usr/local/bin/agent-progress run -- python3 train.py",
             "python3 /somewhere/agent_progress.py exec --shell 'python3 train.py'"]:
    ck("left alone: %s" % form[:44], wrapped_form(form) is None)

print()
print("=== the session a job belongs to is recorded ===")
cli("rm", "--all", "--force")
for name, sid in [("other", "sessB"), ("mine", "sessA")]:
    subprocess.run([sys.executable, ENGINE, "start", name, "--eta", "3h",
                    "--monitor", "time", "--no-watch"], capture_output=True,
                   env=dict(os.environ, CLAUDE_CODE_SESSION_ID=sid))
    time.sleep(0.1)
raw = json.loads(open(cc.STATE).read())["jobs"]
ck("a job records the session that started it",
   raw["mine"]["session_id"] == "sessA" and raw["other"]["session_id"] == "sessB",
   str({k: v.get("session_id") for k, v in raw.items()}))
out = statusline({"session_id": "sessA"})
names = [re.sub(r"\x1b\[[0-9;]*m", "", l).split()[1] for l in out.splitlines() if l.strip()]
ck("this session's jobs come first", names and names[0] == "mine", str(names))
cli("rm", "--all", "--force")

print()
print("=== a clock that jumped ===")
future = {"id": "future", "state": "running", "started": time.time() + 600,
          "unit": "it", "samples": [], "eta_end": time.time() + 1200}
cfg = cc.load_config()
ck("a job that appears to start in the future still renders",
   isinstance(cc.render_line(future, cfg, width=100), str))
ck("and its elapsed time is not negative",
   "-" not in cc.fmt_dur(cc.estimate(future, None, cfg)["elapsed"]))

print()
print()
print("=== a watcher that dies mid-job comes back ===")
# Watchers were revived only when a session started. Inside a session left open
# for hours, nothing ever brought one back: the job's record stopped being
# updated and its bar sat at whatever it last said, still calling a job that
# had finished long ago "running".
cli("rm", "--all", "--force")
orphan = "orphaned-" + sandbox.TAG
cli("run", "--name", orphan, "--eta", "1h", "--", "sleep", "30")
time.sleep(2)
wpid = json.loads(open(cc.STATE).read())["jobs"][orphan].get("watcher_pid")
ck("the job started with a watcher", bool(wpid))
try:
    os.kill(int(wpid), 9)
except OSError:
    pass
time.sleep(1)
gone = subprocess.run(["pgrep", "-f", "_watch " + orphan], capture_output=True, text=True).stdout.split()
ck("and the watcher is gone", not gone, str(gone))
hook(STATUS, "UserPromptSubmit", {"session_id": "s"})
time.sleep(1.5)
back = subprocess.run(["pgrep", "-f", "_watch " + orphan], capture_output=True, text=True).stdout.split()
ck("a prompt brings it back", len(back) == 1, str(back))
hook(STATUS, "UserPromptSubmit", {"session_id": "s"})
time.sleep(1)
again = subprocess.run(["pgrep", "-f", "_watch " + orphan], capture_output=True, text=True).stdout.split()
ck("and does not stack up a second one", len(again) == 1, str(again))
sandbox.kill_watchers(cc)
subprocess.run(["pkill", "-f", "_watch " + orphan], capture_output=True)
cli("rm", "--all", "--force")

print()
print("=== a finished bar retires after a couple of messages ===")
# A completed bar is there to be noticed, not to be lived with, and time alone
# measured that badly: five minutes is many messages if you are working and none
# at all if you stepped away.
cli("rm", "--all", "--force")


# the jobs below are made by cli(), which runs with whatever session this test
# process is in, so the bar has to be drawn for that same session
_ME = cc.current_session()


def _bar():
    out = subprocess.run([sys.executable, ENGINE, "statusline"],
                         input=json.dumps({"session_id": _ME}),
                         capture_output=True, text=True, env=os.environ).stdout
    return re.sub(r"\033\[[0-9;]*m", "", out)


def _prompt(sid=None):
    env = dict(os.environ)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    subprocess.run([sys.executable, STATUS, "UserPromptSubmit"],
                   input=json.dumps({"session_id": sid or _ME}),
                   capture_output=True, text=True, env=env)


cli("start", "finished", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("done", "finished")
ck("it is shown the moment it finishes", "finished" in _bar())
_prompt()
ck("and still after one message", "finished" in _bar())
_prompt()
ck("but not after two", "finished" not in _bar(), _bar()[:60])

cli("rm", "--all", "--force")
cli("start", "crashed", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("fail", "crashed", "--note", "exploded")
for _ in range(4):
    _prompt()
ck("a crash is not retired that quickly", "crashed" in _bar(), _bar()[:60])

cli("rm", "--all", "--force")
cli("start", "running-still", "--eta", "2h", "--monitor", "time", "--no-watch")
for _ in range(4):
    _prompt()
ck("and a running job is untouched", "running-still" in _bar(), _bar()[:60])

cli("rm", "--all", "--force")
# a name of its own: an earlier test leaves a job called "mine" behind, owned by
# another session, so reusing the name here got this one called "mine-2" and the
# `done` refused - correctly - as reaching into somebody else's session
cli("start", "my-own-job", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("done", "my-own-job")
for _ in range(5):
    _prompt("a-different-session")
ck("another session's messages do not retire yours", "my-own-job" in _bar(), _bar()[:60])
_prompt()
_prompt()
ck("your own two do", "my-own-job" not in _bar(), _bar()[:60])
cli("rm", "--all", "--force")

print()
print("=== a job that finishes hands its result back ===")
# A handed-off command writes its output to a log the session never reads. If
# nothing says it finished, and nothing gives back what it produced, asking for
# a model to be trained means never being told the answer.
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []
_prog = os.path.join(sandbox.HOME, "produces.py")
open(_prog, "w").write(
    "import time\n"
    "for i in range(3):\n"
    "    print('step %d/3' % (i + 1), flush=True); time.sleep(1)\n"
    "print('FINAL RESULT: accuracy 0.93')\n")
cli("run", "--name", "produces", "--eta", "1h", "--", sys.executable, _prog)
_final = None
for _ in range(40):
    time.sleep(1)
    _final = json.loads(open(cc.STATE).read())["jobs"].get("produces", {}).get("state")
    if _final not in ("running", None):
        break
ck("it finished", _final == "done", str(_final))
# The result comes back beside the conversation, not inside it: the user sees
# what their job produced, and nothing is added to what they said to Claude.
_side = json.loads(hook(STATUS, "Stop",
                        {"session_id": cc.current_session(),
                         "stop_hook_active": False}) or "{}").get("systemMessage", "")
ck("the user is shown that it finished", "finished" in _side.lower(), _side[:80])
ck("and the result it produced", "FINAL RESULT: accuracy 0.93" in _side, _side[-120:])
ck("and where to read more of it", "agent-progress log" in _side, _side[-60:])
_ctx = hook(STATUS, "UserPromptSubmit", {"session_id": cc.current_session()})
_text = json.loads(_ctx or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")
ck("nothing rides along with the user's next message", not _text.strip(), _text[:60])
_again = json.loads(hook(STATUS, "Stop", {"session_id": cc.current_session(),
                                          "stop_hook_active": False}) or "{}")
ck("and it is not shown twice", "finished" not in _again.get("systemMessage", "").lower(),
   str(_again)[:60])

cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []
cli("config", "--set", "announce_done=false")
cli("run", "--name", "quiet-one", "--eta", "1h", "--", "sh", "-c", "echo hi")
for _ in range(30):
    time.sleep(1)
    if json.loads(open(cc.STATE).read())["jobs"].get("quiet-one", {}).get("state") != "running":
        break
_text = json.loads(hook(STATUS, "UserPromptSubmit", {"session_id": cc.current_session()}) or "{}")
_text = _text.get("hookSpecificOutput", {}).get("additionalContext", "")
ck("announce_done=false keeps it quiet", "FINISHED" not in _text, _text[:60])
cli("config", "--reset")
sandbox.kill_watchers(cc)
cli("rm", "--all", "--force")

print()
print("=== a fresh install is told which launcher to use ===")
_fresh = tempfile.mkdtemp(prefix="agent-progress-fresh-")
_bin = os.path.join(_fresh, ".local", "bin")
os.makedirs(_bin)
with open(os.path.join(_bin, "agent-progress"), "w") as f:
    f.write("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, ENGINE))
os.chmod(os.path.join(_bin, "agent-progress"), 0o755)
_off = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if ".local/bin" not in p)
_env_off = dict(os.environ, HOME=_fresh, PATH=_off, AGENT_PROGRESS_HOME=os.path.join(_fresh, "state"))
r = subprocess.run([sys.executable, STATUS, "SessionStart"], input=json.dumps({"session_id": "fresh"}),
                   capture_output=True, text=True, env=_env_off)
ctx = ""
if r.stdout.strip():
    ctx = json.loads(r.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
ck("with the shim off PATH, session start names the full path to use",
   "not on PATH" in ctx and "~/.local/bin/agent-progress" in ctx, repr((r.stdout[:120], r.stderr[-80:])))
_env_on = dict(_env_off, PATH=_bin + os.pathsep + _off)
r = subprocess.run([sys.executable, STATUS, "SessionStart"], input=json.dumps({"session_id": "fresh2"}),
                   capture_output=True, text=True, env=_env_on)
_d = json.loads(r.stdout) if r.stdout.strip() else {}
ck("with it on PATH, no launcher advice is given",
   "not on PATH" not in _d.get("hookSpecificOutput", {}).get("additionalContext", ""), r.stdout[:160])
r = subprocess.run([sys.executable, ENGINE, "doctor"], capture_output=True, text=True, env=_env_off)
ck("doctor says the launcher is off PATH and what to use", "NOT on PATH" in r.stdout and "~/.local/bin/agent-progress" in r.stdout,
   r.stdout[-300:])
r = subprocess.run([sys.executable, ENGINE, "doctor"], capture_output=True, text=True, env=_env_on)
ck("and that it is on PATH when it is", "agent-progress (on PATH)" in r.stdout, r.stdout[-300:])
shutil.rmtree(_fresh, ignore_errors=True)

print()
print("=== a session with no bar to show is told why, once ===")
_h = tempfile.mkdtemp(prefix="agent-progress-unwired-")
os.makedirs(os.path.join(_h, ".claude"))
_off = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if ".local/bin" not in p)
_env = dict(os.environ, HOME=_h, PATH=_off, AGENT_PROGRESS_HOME=os.path.join(_h, "state"))
def _start(payload, env=_env):
    r = subprocess.run([sys.executable, STATUS, "SessionStart"], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    objs = [l for l in r.stdout.splitlines() if l.strip()]
    return objs, (json.loads(objs[0]) if objs else {})
objs, d = _start({"session_id": "u1", "source": "startup"})
ck("an unwired statusline is explained to the person", "not wired" in d.get("systemMessage", "")
   and "install-statusline.sh" in d.get("systemMessage", ""), repr(d)[:160])
ck("and the launcher advice to Claude rides in the same single object",
   len(objs) == 1 and "not on PATH" in d.get("hookSpecificOutput", {}).get("additionalContext", ""), str(len(objs)))
objs, d = _start({"session_id": "u1", "source": "resume"})
ck("a resume does not repeat the statusline note", "systemMessage" not in d, repr(d)[:120])
with open(os.path.join(_h, ".claude", "settings.json"), "w") as f:
    json.dump({"statusLine": {"type": "command", "command": 'python3 "/nowhere/agent_progress.py" statusline'}}, f)
objs, d = _start({"session_id": "u2"})
ck("a statusline wired to a missing copy is explained", "no longer exists" in d.get("systemMessage", ""), repr(d)[:160])
r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "install-statusline.sh")], capture_output=True, text=True, env=_env)
wired = json.load(open(os.path.join(_h, ".claude", "settings.json")))["statusLine"]["command"]
ck("the installer wires the interpreter by full path", wired.startswith('"/') and "agent_progress.py" in wired, wired[:80])
objs, d = _start({"session_id": "u3"}, env=dict(_env, PATH=os.path.join(_h, ".local", "bin") + os.pathsep + _off))
ck("after installing, session start has nothing to tell the person", "systemMessage" not in d, str(objs)[:120])
ck("but still asks Claude for estimates", "AGENT_PROGRESS_ETA" in d.get("hookSpecificOutput", {}).get("additionalContext", ""), str(d)[:160])
shutil.rmtree(_h, ignore_errors=True)

print()
print("=== the wrapper keeps to the user's permission rules ===")
# Claude Code checks a command against the allow rules after hooks have
# rewritten it. A command the user allowed must not become one they did not.
_rh = tempfile.mkdtemp(prefix="agent-progress-rules-")
os.makedirs(os.path.join(_rh, ".claude"))
_rp = tempfile.mkdtemp(prefix="agent-progress-proj-")
os.makedirs(os.path.join(_rp, ".claude"))
def _wrap(cmd, rules, project_rules=None):
    json.dump({"permissions": {"allow": rules}}, open(os.path.join(_rh, ".claude", "settings.json"), "w"))
    lp = os.path.join(_rp, ".claude", "settings.local.json")
    if project_rules is not None:
        json.dump({"permissions": {"allow": project_rules}}, open(lp, "w"))
    elif os.path.exists(lp):
        os.remove(lp)
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "auto_track.py")],
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "r", "cwd": _rp}),
                       capture_output=True, text=True,
                       env=dict(os.environ, HOME=_rh, AGENT_PROGRESS_HOME=os.path.join(_rh, "st"), AGENT_PROGRESS_NOTIFY="false"))
    return json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["command"] if r.stdout.strip() else None
w = _wrap("python3 train.py", ["Bash(python3:*)"])
ck("a python3 rule gets a wrapper that starts with python3", w is not None and w.startswith("python3 ") and "agent_progress.py" in w, str(w)[:100])
ck("and the original command still runs inside it", w is not None and "--shell 'python3 train.py'" in w, str(w)[-60:])
w = _wrap("bash train.sh", ["Bash(bash:*)"])
ck("a bash rule gets bash -c around the wrapper", w is not None and w.startswith("bash -c "), str(w)[:80])
ck("a command the rules allow but no wrapper form could is left alone", _wrap("make train", ["Bash(make:*)"]) is None)
w = _wrap("python3 train.py", [])
ck("with no rules known, the python3-first form is still preferred (CLI rules are invisible here)",
   w is not None and w.startswith("python3 ") and "exec --name" in w, str(w)[:80])
w = _wrap("make train", [])
ck("and a command that cannot host the engine gets the plain wrapper", w is not None and "exec --name" in w and not w.startswith("make"), str(w)[:80])
ck("a bare Bash rule allows everything, so any form is fine", _wrap("python3 train.py", ["Bash"]) is not None)
ck("an unrelated rule changes nothing", _wrap("python3 train.py", ["Bash(git:*)"]) is not None)
w = _wrap("python3 train.py", [], project_rules=["Bash(python3:*)"])
ck("a project's own settings.local.json counts too", w is not None and w.startswith("python3 "), str(w)[:80])
ck("rule matching is exact about the prefix", not cc.rule_allows("python3x train.py", ["Bash(python3:*)"])
   and cc.rule_allows("python3", ["Bash(python3:*)"]) and cc.rule_allows("make", ["Bash(make)"]) and not cc.rule_allows("make x", ["Bash(make)"]))
shutil.rmtree(_rh, ignore_errors=True); shutil.rmtree(_rp, ignore_errors=True)

print()
print("=== set a variable, then do the work: wrapped, and the variable survives ===")
_w = tempfile.mkdtemp(prefix="agent-progress-assign-")
_env = dict(os.environ, AGENT_PROGRESS_HOME=os.path.join(_w, "st"), AGENT_PROGRESS_NOTIFY="false")
subprocess.run([sys.executable, ENGINE, "config", "--set", "auto_track_after_seconds=60"], capture_output=True, env=_env)
_cmd = ("cd %s && J=%s/tmp && mkdir -p $J && cat > $J/a.py <<'EOF'\nimport os, sys\nprint('ran', sys.argv[1:])\nEOF\n"
        "python3 $J/a.py one two" % (_w, _w))
r = subprocess.run([sys.executable, os.path.join(HOOKS, "auto_track.py")],
                   input=json.dumps({"tool_name": "Bash", "tool_input": {"command": _cmd, "timeout": 600000}, "session_id": "as", "cwd": _w}),
                   capture_output=True, text=True, env=_env)
_wrapped = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["command"] if r.stdout.strip() else None
ck("the command is wrapped", _wrapped is not None and "exec --name" in _wrapped, str(_wrapped)[:100])
def _run(c):
    return subprocess.run(["/bin/bash", "-c", c + '\necho "__after J=$J rc=$?"'], cwd="/", capture_output=True, text=True, env=_env)
_a, _b = _run(_cmd), _run(_wrapped or _cmd)
ck("output, exit code and the variable afterwards are identical to the raw command",
   _a.stdout == _b.stdout and _a.stderr == _b.stderr and "J=%s/tmp" % _w in _b.stdout, "%r vs %r" % (_a.stdout[-80:], _b.stdout[-80:]))
shutil.rmtree(_w, ignore_errors=True)

print()
print("=== the shapes a real session used: setup, work, then more shell ===")
_w = tempfile.mkdtemp(prefix="agent-progress-shapes-")
os.makedirs(os.path.join(_w, "sub"))
open(os.path.join(_w, "train.py"), "w").write("import os,sys\nprint('ran', sys.argv[1:], os.path.basename(os.getcwd()))\n"
                                              "sys.exit(int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else 0)\n")
_env = dict(os.environ, AGENT_PROGRESS_HOME=os.path.join(_w, "st"), AGENT_PROGRESS_NOTIFY="false")
subprocess.run([sys.executable, ENGINE, "config", "--set", "auto_track_after_seconds=60"], capture_output=True, env=_env)
def _hook(cmd):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "auto_track.py")],
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd, "timeout": 600000}, "session_id": "sh", "cwd": _w}),
                       capture_output=True, text=True, env=_env)
    return json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["command"] if r.stdout.strip() else None
def _run(c):
    return subprocess.run(["/bin/bash", "-c", c + '\n__rc=$?; echo "__END pwd=$(basename "$(pwd)") S=$S rc=$__rc"'],
                          cwd=_w, capture_output=True, text=True, env=_env)
_T = os.path.join(_w, "train.py")
for label, cmd, wrapped_expected in (
        ("commit, then S=..., then the work", "git init -q . 2>/dev/null; S=%s && python3 $S 3" % _T, True),
        ("edit with a heredoc, then S=..., then the work",
         "cat > %s/e.py <<'EOF'\nprint('edited; ok && fine')\nEOF\npython3 %s/e.py && S=%s && python3 $S 2" % (_w, _w, _T), True),
        ("the work, then cd", "python3 %s 4; cd sub" % _T, True),
        ("python -c with assignment lines", "python3 -c \"\nrng=1\nd=dict(a=1)\nprint('inline', rng, d)\n\"", True),
        ("cd in the middle is left alone", "python3 %s 0 && cd sub && python3 %s 0" % (_T, _T), False)):
    w = _hook(cmd)
    ck("%s: %s" % (label, "wrapped" if wrapped_expected else "untouched"), (w is not None) == wrapped_expected, str(w)[:120])
    a, b = _run(cmd), _run(w or cmd)
    ck("  and runs identically, shell state included", (a.stdout, a.stderr, a.returncode) == (b.stdout, b.stderr, b.returncode),
       "%r vs %r" % (a.stdout[-90:], b.stdout[-90:]))
shutil.rmtree(_w, ignore_errors=True)

print()
print("=== a statusline that is wired but never refreshed is explained ===")
_h = tempfile.mkdtemp(prefix="agent-progress-static-")
os.makedirs(os.path.join(_h, ".claude"))
_env = dict(os.environ, HOME=_h, AGENT_PROGRESS_HOME=os.path.join(_h, "state"))
with open(os.path.join(_h, ".claude", "settings.json"), "w") as f:
    json.dump({"statusLine": {"type": "command", "command": 'python3 "%s" statusline' % ENGINE}}, f)
r = subprocess.run([sys.executable, STATUS, "SessionStart"], input=json.dumps({"session_id": "st1", "source": "startup"}),
                   capture_output=True, text=True, env=_env)
_d = json.loads(r.stdout) if r.stdout.strip() else {}
ck("an install without refreshInterval is told the bar cannot move", "refreshInterval" in _d.get("systemMessage", ""), repr(_d)[:160])
r = subprocess.run([sys.executable, ENGINE, "doctor"], capture_output=True, text=True, env=_env)
ck("doctor says the same", "without refreshInterval" in r.stdout, r.stdout[-300:])
r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "install-statusline.sh")], capture_output=True, text=True, env=_env)
_sl = json.load(open(os.path.join(_h, ".claude", "settings.json")))["statusLine"]
ck("the installer sets refreshInterval", _sl.get("refreshInterval") == 1, str(_sl))
r = subprocess.run([sys.executable, ENGINE, "doctor"], capture_output=True, text=True, env=_env)
ck("and doctor is satisfied", "refreshed every second" in r.stdout, r.stdout[-200:])
shutil.rmtree(_h, ignore_errors=True)

print()
print("=== the estimate travels with the command ===")
_w = tempfile.mkdtemp(prefix="agent-progress-eta-")
_env = dict(os.environ, AGENT_PROGRESS_HOME=os.path.join(_w, "st"), AGENT_PROGRESS_NOTIFY="false", CLAUDE_CODE_SESSION_ID="eta")
subprocess.run([sys.executable, ENGINE, "config", "--set", "auto_track_after_seconds=0.3"], capture_output=True, env=_env)
def _hook(cmd):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "auto_track.py")],
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "eta", "cwd": _w}),
                       capture_output=True, text=True, env=_env)
    return json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["command"] if r.stdout.strip() else None
w = _hook("AGENT_PROGRESS_ETA=10m python3 -c 'import time; time.sleep(1)'")
ck("a command with an estimate is tracked because of it", w is not None and "--eta 10m" in w, str(w)[:160])
subprocess.run(["/bin/bash", "-c", w], capture_output=True, env=_env, timeout=60)
_jobs = json.load(open(os.path.join(_w, "st", "state.json")))["jobs"]
_j = list(_jobs.values())[0]
ck("and the job records it", _j.get("eta_prior_s") == 600 and _j.get("eta_end"), str({k: _j.get(k) for k in ("id", "eta_prior_s", "eta_end")}))
w = _hook("AGENT_PROGRESS_ETA=2h AGENT_PROGRESS_NAME=evalrun uv run python eval.py")
ck("a name hint names the job", w is not None and "--name evalrun" in w and "--eta 2h" in w, str(w)[:160])
w = _hook("cd %s && AGENT_PROGRESS_ETA=5m python3 -c 'print(1)'" % _w)
ck("the hint is read after a setup prefix too", w is not None and "--eta 5m" in w, str(w)[:160])
ck("a hint that is not a duration is ignored, and the command untouched",
   _hook("AGENT_PROGRESS_ETA=soon python3 -c 'print(1)'") is None)
r = subprocess.run(["/bin/bash", "-c", "AGENT_PROGRESS_ETA=10m python3 -c 'import os; print(os.environ.get(\"AGENT_PROGRESS_ETA\"))'"],
                   capture_output=True, text=True)
ck("the variable is an ordinary one to the command", r.stdout.strip() == "10m")
shutil.rmtree(_w, ignore_errors=True)

print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

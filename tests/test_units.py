#!/usr/bin/env python3
"""Unit tests for agent-progress's internals.

run_tests.py checks that the CLI behaves; this checks the parts underneath it -
duration and size parsing, progress scraping, the estimator, line clipping, the
monitors, and config coercion. These are where a wrong answer is quiet: a bar
that is subtly wrong looks like a bar.
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import time

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "agent_progress.py")
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


def eq(name, got, want):
    ck(name, got == want, "got %r want %r" % (got, want))


print("=== parse_duration ===")
for text, want in [("45m", 2700), ("2h30m", 9000), ("90s", 90), ("1h", 3600),
                   ("2:30:00", 9000), ("120", 120), ("0", 0), ("1.5h", 5400),
                   ("1h30", 3630), ("30", 30), ("", None), (None, None)]:
    eq("parse_duration(%r)" % text, cc.parse_duration(text), want)
try:
    cc.parse_duration("banana")
    ck("parse_duration rejects nonsense", False, "no error raised")
except SystemExit:
    ck("parse_duration rejects nonsense", True)

print()
print("=== parse_size / fmt_size ===")
for text, want in [("12GB", 12 * 1024**3), ("500 MB", 500 * 1024**2), ("1.5t", int(1.5 * 1024**4)),
                   ("4096", 4096), ("8mb", 8 * 1024**2), (None, None)]:
    eq("parse_size(%r)" % text, cc.parse_size(text), want)
try:
    cc.parse_size("big")
    ck("parse_size rejects nonsense", False)
except SystemExit:
    ck("parse_size rejects nonsense", True)
eq("fmt_size(0)", cc.fmt_size(0), "0B")
eq("fmt_size(1536)", cc.fmt_size(1536), "1.5KB")
eq("fmt_size(None)", cc.fmt_size(None), "?")

print()
print("=== duration formatting ===")
for secs, want in [(0, "00:00"), (59, "00:59"), (60, "01:00"), (3599, "59:59"),
                   (3600, "1:00:00"), (-5, "00:00"), (None, "--:--")]:
    eq("fmt_dur(%r)" % secs, cc.fmt_dur(secs), want)
for secs, want in [(0, "0s"), (59, "59s"), (60, "1m"), (3600, "1h"),
                   (3660, "1h01m"), (None, "?")]:
    eq("fmt_short(%r)" % secs, cc.fmt_short(secs), want)

print()
print("=== clipping keeps within the column budget ===")
line = cc.paint("\U0001f480 job", "fail", True) + " " + "x" * 200
for w in (1, 2, 3, 5, 10, 40, 120):
    got = cc.visible_len(cc.clip(line, w))
    ck("clip to %d columns" % w, got <= w, "produced %d columns" % got)
ck("clip leaves short lines alone", cc.clip("abc", 40) == "abc")
ck("clip closes colour codes", cc.clip(line, 10).endswith("\033[0m"))

print()
print("=== progress scraping ===")
cases = [
    ("tqdm", " 45%|####      | 45/100 [00:12<00:14]", 45, 100),
    ("epoch", "Epoch 12/50 - loss 0.3", 12, 50),
    ("step", "global_step 1200/10000", 1200, 10000),
    ("keras", "   32/1875 [>....]", 32, 1875),
    ("trial", "Trial 7 of 40 finished", 7, 40),
]
for name, text, step, total in cases:
    r = cc.parse_progress(text)
    ck("parse %s" % name, r and r["step"] == step and r["total"] == total, repr(r))
ck("percent only", (cc.parse_progress("67% complete") or {}).get("pct") == 0.67)
ck("ignores paths and versions",
   cc.parse_progress("loading /a/b/c-1/2 v1.2.3 at 2026/08/31") is None,
   repr(cc.parse_progress("loading /a/b/c-1/2 v1.2.3 at 2026/08/31")))
ck("ignores a step beyond its total", cc.parse_progress("weird 500/100") is None,
   repr(cc.parse_progress("weird 500/100")))
ck("survives a huge line", cc.parse_progress("x" * 200000 + " 5/10 ") is not None)
try:
    cc.parse_progress("5/10", pattern="(unclosed")
    ck("a bad custom pattern is survivable", True)
except Exception as ex:
    ck("a bad custom pattern is survivable", False, "%s: %s" % (type(ex).__name__, ex))

print()
print("=== probe output ===")
for text, want in [("42\n", 42), ("42/100\n", 42), ("  7 / 9 \n", 7), ("", None)]:
    r = cc.parse_probe_output(text)
    got = r.get("step") if r else None
    eq("probe %r" % text, got, want)
eq("probe percent", (cc.parse_probe_output("55%") or {}).get("pct"), 0.55)

print()
print("=== the estimator ===")
now = time.time()
cfg = cc.load_config()

def job(**kw):
    base = dict(id="j", state="running", started=now - 600, unit="it", samples=[])
    base.update(kw)
    return base

j = job(total=100, units=50.0, samples=[[now - 600 + i * 12, i] for i in range(51)])
e = cc.estimate(j, now, cfg)
ck("measured rate wins once there is data", e["source"] == "measured", e["source"])
ck("remaining is sane", 500 < e["remaining"] < 700, str(e["remaining"]))
e = cc.estimate(job(total=100, units=1.0, eta_end=now + 3000,
                    samples=[[now - 30, 0], [now - 10, 1]]), now, cfg)
ck("blends early on", e["source"] == "blend", e["source"])
e = cc.estimate(job(eta_end=now + 900, started=now - 900), now, cfg)
ck("falls back to the prior", e["source"] == "claude", e["source"])
ck("prior fraction never reaches 1", e["frac"] < 1.0, str(e["frac"]))

ck("zero total does not divide by zero", cc.estimate(job(total=0, units=5), now, cfg)["frac"] is None)
ck("units past total clamps to 1",
   cc.estimate(job(total=10, units=99.0), now, cfg)["frac"] == 1.0)
ck("negative step clamps to 0",
   cc.estimate(job(total=10, units=-5.0), now, cfg)["frac"] == 0.0)
e = cc.estimate(job(state="done", ended=now, total=10, units=10.0), now, cfg)
ck("finished job is 100% and has no remaining", e["frac"] == 1.0 and e["remaining"] == 0)
e2 = cc.estimate(job(state="failed", ended=now - 100, eta_end=now + 900), now, cfg)
e3 = cc.estimate(job(state="failed", ended=now - 100, eta_end=now + 900), now + 500, cfg)
ck("a stopped job's bar is frozen", e2["frac"] == e3["frac"], "%s vs %s" % (e2["frac"], e3["frac"]))
ck("no signal at all -> indeterminate", cc.estimate(job(), now, cfg)["frac"] is None)

print()
print("=== monitors ===")
scratch = tempfile.mkdtemp(prefix="agent-progress-unit-")
log = os.path.join(scratch, "j.log")
open(log, "w").write("loading data\nnormalizing (v2)\n")
m = cc.monitor_reading({"log": log, "log_offset": 0,
                        "monitor": {"kind": "milestones",
                                    "milestones": ["loading data", "normalizing (v2)",
                                                   "writing"]}}, now)
ck("milestones count what has appeared", m and m["step"] == 2, repr(m))
open(log, "a").write("cost was $5\n")
j2 = {"log": log, "log_offset": 0,
      "monitor": {"kind": "milestones", "milestones": ["cost was $5", "done"]}}
m = cc.monitor_reading(j2, now)
ck("a stage name with regex characters still matches", m and m["step"] == 1, repr(m))
j3 = {"log": log, "log_offset": 0,
      "monitor": {"kind": "milestones", "milestones": ["(unclosed", "loading data"]}}
m = cc.monitor_reading(j3, now)
ck("an unparseable stage name does not crash", m is not None, repr(m))

os.makedirs(os.path.join(scratch, "out"))
mon = {"kind": "files", "glob": os.path.join(scratch, "out", "*.txt"), "total": 3}
ck("files monitor with no matches yet",
   (cc.monitor_reading({"monitor": mon}, now) or {}).get("step") == 0)
open(os.path.join(scratch, "out", "a.txt"), "w").write("x")
ck("files monitor counts", (cc.monitor_reading({"monitor": mon}, now) or {}).get("step") == 1)

ck("size monitor with a missing path returns nothing",
   cc.monitor_reading({"monitor": {"kind": "size", "path": scratch + "/nope",
                                   "target_bytes": 100}}, now) is None)
open(os.path.join(scratch, "big"), "w").write("x" * 50)
m = cc.monitor_reading({"monitor": {"kind": "size", "path": os.path.join(scratch, "big"),
                                    "target_bytes": 100}}, now)
ck("size monitor measures", m and abs(m["pct"] - 0.5) < 0.01, repr(m))
m = cc.monitor_reading({"monitor": {"kind": "size", "path": scratch}}, now)
ck("size monitor without a target gives no fraction", m is None, repr(m))

ck("probe that fails is survivable",
   cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "exit 7"}}, now) is None)
ck("probe that prints nothing is survivable",
   cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "true"}}, now) is None)
m = cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "echo 12", "total": 40}}, now)
ck("probe reads a count", m and m["step"] == 12, repr(m))
ck("probe that hangs is bounded",
   cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "sleep 30", "timeout": 1}}, now) is None)
ck("time monitor reads nothing", cc.monitor_reading({"monitor": {"kind": "time"}}, now) is None)
ck("a bad custom pattern does not crash the monitor",
   cc.monitor_reading({"log": log, "log_offset": 0, "pattern": "(unclosed",
                       "monitor": {"kind": "log"}}, now) is None)
shutil.rmtree(scratch, ignore_errors=True)

print()
print("=== config coercion ===")
eq("bool from 'true'", cc.coerce("color", "true"), True)
eq("bool from 'off'", cc.coerce("color", "off"), False)
eq("int from '30'", cc.coerce("bar_width", "30"), 30)
eq("float", cc.coerce("interval_fraction", "0.25"), 0.25)
eq("wrap is still accepted", cc.coerce("auto_track", "wrap"), "defer")
for key, bad in [("bar_width", "9999"), ("style", "fancy"), ("color", "maybe"),
                 ("interval_fraction", "2.0"), ("bar_width", "abc")]:
    try:
        cc.coerce(key, bad)
        ck("%s=%s rejected" % (key, bad), False, "accepted")
    except ValueError:
        ck("%s=%s rejected" % (key, bad), True)

print()
print("=== command classification ===")
eq("name from a script", cc.suggest_job_name("python3 src/train_model.py --lr 1"), "train_model")
eq("name from a tool and its subcommand", cc.suggest_job_name("cargo build --release"), "cargo-build")
ck("name is never empty", cc.suggest_job_name("!!!") == "job")
ck("timeout given as text does not crash",
   cc.classify_command("./x", {"timeout": "600000"}, cfg) is not None)
ck("a command that is only whitespace is ignored",
   not cc.classify_command("   ", {}, cfg)["track"])

print()
print("=== crash reasons ===")
eq("exit 1", cc.crash_reason(1)[0], "exit 1")
eq("SIGKILL", cc.crash_reason(137)[0], "SIGKILL")
eq("SIGSEGV", cc.crash_reason(139)[0], "SIGSEGV")
eq("unknown signal", cc.crash_reason(128 + 60)[0], "signal 60")
ck("no code at all", cc.crash_reason(None)[0] == "no exit code")

print()
print("=== rendering with awkward settings ===")
import subprocess


def render(job, **over):
    c = dict(cc.load_config())
    c.update(over)
    return cc.render_line(job, c, width=120)


j = job(total=10, units=5.0)
plain = cc.visible_len(render(j, bar_width=20))
wide = cc.visible_len(render(j, fill_char="ab", bar_width=20))
ck("a multi-character fill does not widen the bar", wide == plain,
   "%d columns vs %d" % (wide, plain))
ck("an empty spinner does not crash", isinstance(render(j, spinner=""), str))
ck("colour off produces no escapes", "\033[" not in render(j, color=False))
ck("a wide glyph is accounted for",
   cc.visible_len(cc.clip(render(job(state="failed", ended=now, exit_code=137),
                                 glyph_failed="\U0001f480"), 30)) <= 30)

print()
print("=== the CLI on awkward input ===")


def run(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


run("rm", "--all", "--force")
ck("log for an unknown job fails cleanly",
   run("log", "nope").returncode != 0 and "Traceback" not in run("log", "nope").stderr)
ck("show for an unknown job fails cleanly",
   "Traceback" not in run("show", "nope").stderr)
ck("rm for an unknown job fails cleanly",
   "Traceback" not in run("rm", "nope").stderr)
r = run("run", "--name", "edge", "--eta", "nonsense", "--", "true")
ck("a bad --eta is reported, not crashed",
   r.returncode != 0 and "Traceback" not in r.stderr, r.stderr[-120:])
r = run("run", "--name", "edge", "--pattern", "(unclosed", "--", "true")
ck("a bad --pattern is reported, not crashed",
   r.returncode != 0 and "not a valid regex" in (r.stderr + r.stdout), r.stderr[-120:])
r = run("preview", "--set", "bar_width=abc")
ck("preview rejects a bad value cleanly",
   "Traceback" not in r.stderr, r.stderr[-160:])
r = run("statusline", input="{}")
ck("statusline with no jobs is fine", r.returncode == 0)
run("start", "aaa-one", "--eta", "1h", "--monitor", "time", "--no-watch")
run("start", "aaa-two", "--eta", "1h", "--monitor", "time", "--no-watch")
r = run("show", "aaa")
ck("an ambiguous job name is reported, not crashed",
   "ambiguous" in (r.stderr + r.stdout).lower(), (r.stderr + r.stdout)[-120:])
run("done", "aaa-one")
r = run("done", "aaa-one")
ck("finishing an already finished job is harmless", r.returncode == 0, r.stderr[-120:])
run("rm", "--all", "--force")

print()
print()
print("=== a bar the threshold has already allowed is not taken back ===")
# Estimates move as a job is measured. One that starts at half an hour and is
# revised down to ninety seconds used to take its own bar away mid-run, then
# give it back once elapsed crossed the threshold on its own.
cfg = dict(cc.load_config())
cfg["min_duration_seconds"] = 120
now = time.time()
long_then_short = {"state": "running", "started": now - 20, "samples": [],
                   "est_total_s": 45, "initial_est_total_s": 1800,
                   "eta_end": now + 25, "unit": "it"}
ck("a job once thought long keeps its bar", cc.job_visible(long_then_short, cfg, now))
never_long = {"state": "running", "started": now - 20, "samples": [],
              "est_total_s": 20, "initial_est_total_s": 20,
              "eta_end": now, "unit": "it"}
ck("a job never thought long stays off the statusline",
   not cc.job_visible(never_long, cfg, now))
grown = {"state": "running", "started": now - 20, "samples": [],
         "est_total_s": 3600, "initial_est_total_s": 30, "eta_end": now + 3580, "unit": "it"}
ck("and one that turns out to be long earns a bar", cc.job_visible(grown, cfg, now))
old_enough = {"state": "running", "started": now - 300, "samples": [], "unit": "it"}
ck("elapsed alone is still enough", cc.job_visible(old_enough, cfg, now))

print()
print("=== every way of creating a job, in one breath ===")
# A search-and-replace once landed inside attach_batch_job instead of the two
# places it was meant for, and every scheduler job raised NameError on creation
# - no slurm, LSF or PBS job could be tracked at all. It shipped, because the
# paths that were checked were the ones the edit was supposed to touch. This
# costs a second and covers all of them.
import types as _types

_args = _types.SimpleNamespace(
    eta=None, note=None, desc=None, unit=None, total=None, pattern=None,
    monitor=None, log=None, cwd=None, interval=None, force_show=False,
    files=None, size=None, probe=None, milestones=None, name=None, quiet=True)
made = {}
try:
    made["_new_job"] = cc._new_job(_args, cmd="echo hi", log="/tmp/x.log",
                                   exit_file="/tmp/x.log.exit", pid=1)
except Exception as ex:
    made["_new_job"] = "RAISED %r" % (ex,)
for kind in ("slurm", "lsf", "pbs"):
    try:
        with cc.state_rw() as st:
            st["jobs"] = {}
        made[kind] = cc.attach_batch_job(kind, "4242", "/tmp", eta=None,
                                         name="probe-" + kind)
    except Exception as ex:
        made[kind] = "RAISED %r" % (ex,)
for how, got in sorted(made.items()):
    ck("a job can be made the %s way" % how, not str(got).startswith("RAISED"), str(got)[:80])
ck("a scheduler job records no exit file of its own",
   not (cc.state_ro()["jobs"].get("probe-pbs") or {}).get("exit_file"))
ck("but a locally run one does",
   isinstance(made.get("_new_job"), dict) and made["_new_job"].get("exit_file"))
with cc.state_rw() as st:
    st["jobs"] = {}

print()
print("=== numbers a bar cannot be drawn from ===")
# float() accepts "nan" and "inf", and a diverged training run prints the word
# on every line. One reaching the samples gave the bar a rate of nan, which it
# then showed to the user as "nans/it" while claiming no time remained.
import math as _math

ck("nan is not a number here", cc._float("nan") is None)
ck("nor is inf", cc._float("inf") is None and cc._float("-inf") is None)
ck("but a real one still is", cc._float("3.5") == 3.5)
_j = {"samples": []}
cc.record_sample(_j, float("nan"), time.time())
cc.record_sample(_j, float("inf"), time.time())
ck("a sample that is not a number is refused", _j["samples"] == [])
cc.record_sample(_j, 5, time.time())
ck("and a real one is kept", len(_j["samples"]) == 1)

_cfg = dict(cc.load_config())
_now = time.time()
_hostile = {
    "step beyond total": dict(step=150, total=100),
    "negative step": dict(step=-5, total=100),
    "zero total": dict(step=5, total=0),
    "percent over 100": dict(pct=180.0),
    "negative percent": dict(pct=-20.0),
    "nan percent": dict(pct=float("nan")),
    "infinite percent": dict(pct=float("inf")),
    "started in the future": dict(started=_now + 3600),
    "ended before it started": dict(state="done", ended=_now - 600, started=_now),
    "nan estimate": dict(est_total_s=float("nan")),
    "infinite estimate": dict(est_total_s=float("inf")),
    "negative estimate": dict(est_total_s=-500),
    "samples out of order": dict(samples=[[_now, 10], [_now - 30, 40]], step=10, total=100),
    "a nan among the samples": dict(samples=[[_now - 30, float("nan")], [_now, 5]],
                                    step=5, total=100),
    "malformed sample rows": dict(samples=[["x", "y"], None, [_now, 5]], step=5, total=100),
    "identical timestamps": dict(samples=[[_now, 1], [_now, 2]], step=2, total=100),
    "progress going backwards": dict(samples=[[_now - 60, 90], [_now, 10]], step=10, total=100),
}
_bad = []
for _name, _extra in sorted(_hostile.items()):
    _job = dict(state="running", started=_now - 60, samples=[], unit="it", updated=_now)
    _job.update(_extra)
    try:
        _e = cc.estimate(_job, _now, _cfg)
        _line = re.sub(r"\033\[[0-9;]*m", "", cc.render_line(_job, _cfg, width=100))
        _pct = _e.get("pct")
        _sane = _pct is None or (isinstance(_pct, (int, float)) and _math.isfinite(_pct)
                                 and -0.01 <= _pct <= 100.01)
        if not (_sane and "nan" not in _line.lower() and "inf" not in _line.lower()):
            _bad.append((_name, _pct, _line[:50]))
    except Exception as _ex:
        _bad.append((_name, "RAISED", repr(_ex)))
ck("every hostile job still draws a sane bar", not _bad, str(_bad[:2]))
for _key in ("max_jobs", "bar_width"):
    try:
        cc.coerce(_key, "nan")
        _rejected = False
    except ValueError:
        _rejected = True
    ck("a setting cannot be set to nan (%s)" % _key, _rejected)

print()
print("=== a job buried in a compound command ===")
# Claude writes the setup and the work in one call constantly - a heredoc that
# writes a script and then the line that runs it, or `mkdir -p out && python
# train.py`. Judging the whole thing by its first word called it trivial and
# left the training run untracked, which is the main case the plugin exists for.
_cases = [
    ("setup and then the job", "mkdir -p out && python3 train.py --epochs 50", True),
    ("a heredoc, then the job",
     "cat > t.py <<'X'\nimport time\nX\npython3 train.py", True),
    ("five thousand lines, then the job",
     "\n".join("mkdir -p d%d" % i for i in range(5000)) + "\npython3 train.py", True),
    ("a huge heredoc, then the job",
     "cat > f <<'X'\n" + ("data line\n" * 20000) + "X\npython3 train.py", True),
    ("trivial all the way through", "mkdir -p out && ls -la && echo done", False),
    ("just a listing", "ls -la", False),
    ("asking for help", "python train.py --help", False),
    ("a long token and a job", "python train.py " + "a" * 200000, True),
    ("thousands of echoes", " && ".join("echo %d" % i for i in range(5000)), False),
]
_wrong, _slow = [], []
for _name, _cmd, _want in _cases:
    _t0 = time.time()
    _got = cc.classify_command(_cmd)["track"]
    _took = time.time() - _t0
    if _got != _want:
        _wrong.append((_name, _got, _want))
    if _took > 1.0:
        _slow.append((_name, round(_took, 1)))
ck("a job is found wherever it sits in a compound command", not _wrong, str(_wrong))
ck("and deciding stays fast on pathological commands", not _slow, str(_slow))
# The self-reference guard is anchored, so it moved into the per-segment bucket
# when compound commands started being read part by part. It has to keep
# holding: the plugin wrapping its own invocation is how a wrapper recurses.
for _own in ("agent-progress run --name t --eta 1h -- python train.py",
             "/usr/local/bin/agent-progress exec --name train --after 20 --shell 'python train.py'",
             "agent-progress ls",
             "agent-progress update train --eta 2h",
             "python3 /somewhere/scripts/agent_progress.py run -- python train.py"):
    ck("the plugin does not wrap itself: %s" % _own[:40],
       not cc.classify_command(_own)["track"], _own[:60])

# A heredoc is how a script gets written; collapsing one onto a line put the
# script's own source into the middle of what claimed to be the command.
_with_heredoc = ("mkdir -p out && cat > train.py <<'PY'\nimport time\n"
                 "for i in range(14):\n    print(i)\nPY\npython3 train.py --epochs 13")
_shown = cc.command_for_display(_with_heredoc)
ck("a shown command does not include the script it wrote",
   "import time" not in _shown and "range(14)" not in _shown, _shown[:70])
ck("but it still shows what was actually run",
   "python3 train.py --epochs 13" in _shown and "mkdir -p out" in _shown, _shown[:70])
ck("and a command with no heredoc is untouched",
   cc.command_for_display("python3 train.py") == "python3 train.py")

ck("heredoc bodies are not read as commands",
   not cc.classify_command("cat > f <<'X'\npython3 train.py\nX\nls")["track"],
   "the text of a script is data, not a command being run")

print()
print("=== asking some other scheduler how a job is doing ===")
# The slurm path has its own suite; this is the generic one, used for anything
# with a --state-probe: LSF, PBS, a queue somebody wrote themselves. Coverage
# said none of it had ever run.
# Only "done" and "failed" are endings; everything else means "leave it alone",
# which the probe spells "running" and an unusable answer spells None. The two
# behave identically downstream - a job is never finished on a guess.
for _out, _want in (("RUNNING", "running"), ("COMPLETED", "done"), ("COMPLETE", "done"),
                    ("FAILED", "failed"), ("OUT_OF_MEMORY", "failed"),
                    ("TIMEOUT", "failed"), ("CANCELLED", "failed"),
                    ("CANCELLED_BY_12345", "failed"),
                    ("PENDING", "running"), ("", None), ("what?", "running"),
                    ("0", "done"), ("3", "failed")):
    _job = {"state_probe": "printf '%s'" % _out}
    ck("a probe saying %-14r reads as %s" % (_out, _want),
       cc.read_state_probe(_job) == _want, repr(cc.read_state_probe(_job)))
ck("a probe with nothing to run answers nothing", cc.read_state_probe({}) is None)
ck("a probe that fails answers nothing",
   cc.read_state_probe({"state_probe": "exit 7"}) is None)
ck("a probe that prints several lines uses the last",
   cc.read_state_probe({"state_probe": "printf 'noise\\nCOMPLETED'"}) == "done")
ck("and one that hangs does not hang the caller",
   cc.read_state_probe({"state_probe": "sleep 45"}) is None)

print()
print("=== folding a reading into a job ===")
_j = {"unit": "it", "samples": []}
cc.apply_reading(_j, {"step": 1, "total": 10}, time.time())
ck("a first step of 1 is taken as one done", _j.get("units") in (0.0, 1.0), str(_j.get("units")))
_z = {"unit": "it", "samples": []}
cc.apply_reading(_z, {"step": 0, "total": 10}, time.time())
ck("a job that starts counting at zero is noticed", _z.get("zero_indexed") is True,
   str(_z.get("zero_indexed")))
cc.apply_reading(_z, {"step": 4, "total": 10}, time.time())
ck("and then four means four", _z.get("units") == 4.0, str(_z.get("units")))
# a reading's pct is a fraction, not a number out of a hundred: the parser
# divides by 100 before it gets here
ck("the parser gives a percentage as a fraction",
   cc.parse_progress("Progress: 40%")["pct"] == 0.4,
   str(cc.parse_progress("Progress: 40%")["pct"]))
_p = {"unit": "it", "samples": [], "total": 50}
cc.apply_reading(_p, {"pct": 0.4}, time.time())
ck("a percentage against a known total becomes units", _p.get("units") == 20.0,
   str(_p.get("units")))
_q = {"unit": "it", "samples": []}
cc.apply_reading(_q, {"pct": 0.4}, time.time())
ck("and with no total it is kept as a percentage", _q.get("pct") == 0.4, str(_q.get("pct")))
ck("a reading with nothing in it changes nothing",
   cc.apply_reading({"unit": "it", "samples": []}, {}, time.time()) is False)

print()
print("=== the monitor a job gets from its flags ===")
import types as _t


def _mon(**kw):
    base = dict(monitor=None, pattern=None, log=None, milestone=None, milestones=None,
                glob=None, path=None, target_size=None, probe=None, state_probe=None)
    base.update(kw)
    return cc.build_monitor(_t.SimpleNamespace(**base))


ck("milestones given one at a time", _mon(milestone=["a", "b"])["kind"] == "milestones")
ck("and their order is kept", _mon(milestone=["a", "b"])["milestones"] == ["a", "b"])
ck("milestones given as one string", _mon(milestones="a;b;c")["milestones"] == ["a", "b", "c"])
ck("a glob makes a files monitor", _mon(glob="out/*.pt")["kind"] == "files")
ck("a path makes a size monitor", _mon(path="/tmp/x")["kind"] == "size")
ck("a target size is understood", _mon(path="/tmp/x", target_size="2GB")["target_bytes"]
   == 2 * 1000 ** 3 or _mon(path="/tmp/x", target_size="2GB")["target_bytes"] == 2 * 1024 ** 3,
   str(_mon(path="/tmp/x", target_size="2GB")["target_bytes"]))
ck("a probe command makes a probe monitor", _mon(probe="echo 1/2")["kind"] == "probe")
# no flags at all means no spec: the job works out how to watch itself
ck("and nothing at all leaves it to work it out", _mon() is None, str(_mon()))
ck("while asking for auto says so explicitly", _mon(monitor="auto") == {"kind": "auto"},
   str(_mon(monitor="auto")))

print()
print("=== a duration is the whole string, with real units ===")
for text, want in (("90", 90), ("90s", 90), ("5m", 300), ("2h30m", 9000), ("2d", 172800),
                   ("1w", 7 * 86400), ("1:30:00", 5400), ("2 hours", 7200), ("45 min", 2700),
                   ("1.5h", 5400), ("500ms", 0.5)):
    got = cc.parse_duration(text)
    ck("%-8r -> %s" % (text, want), abs(got - want) < 1e-6, str(got))
for text in ("-5m", "1e3", "2x", "1:x", "::", "nan:0", "inf:0", "99999999999999999999h", "5m junk", "1y"):
    try:
        cc.parse_duration(text)
        ck("%r is refused" % text, False, "accepted")
    except SystemExit:
        ck("%r is refused" % text, True)
ck("an empty duration is None, not an error", cc.parse_duration("  ") is None)

print()
print("=== sizes, percents, huge numbers ===")
for bad in ("1.2.3GB", ".", "GB", "1..5"):
    try:
        cc.parse_size(bad)
        ck("size %r is refused cleanly" % bad, False, "accepted")
    except SystemExit:
        ck("size %r is refused cleanly" % bad, True)
    except ValueError as ex:
        ck("size %r is refused cleanly" % bad, False, "traceback: %s" % ex)
got = cc.parse_progress("GPU util 1234%")
ck("'GPU util 1234%' is not 34%% done", got is None or got.get("pct") is None, str(got))
ck("'done 45%' still reads", (cc.parse_progress("done 45%") or {}).get("pct") == 0.45)
job = {"state": "running", "units": None, "total": None, "samples": []}
ck("a 320-digit step is ignored rather than raised",
   cc.apply_reading(job, {"step": int("1" * 320), "total": int("2" * 320)}, time.time()) is False
   and job.get("units") is None, str(job.get("units")))
arr = {"state": "running", "unit": "task", "total": 8, "total_locked": True, "units": 7.0, "samples": []}
ck("an array's task count is not overwritten by one task's log",
   cc.apply_reading(arr, {"step": 3, "total": 50}, time.time()) is False and arr["units"] == 7.0,
   str(arr["units"]))
st = {"jobs": {"a": {"id": "a", "state": "running", "eta_end": float("inf"), "total": 10 ** 20,
                     "pct": float("nan"), "started": time.time()}}}
cc._sanitize(st)
ck("inf, nan and a number too big to be a count are dropped on read",
   all(st["jobs"]["a"].get(k) is None for k in ("eta_end", "total", "pct")), str(st["jobs"]["a"]))
ck("a timestamp beyond the platform's clock renders as ?", cc.fmt_clock(1e20) == "?")
ck("an infinite duration renders as ?", cc.fmt_short(float("inf")) == "?")
ck("a pid of 0 or -1 is never alive", not cc.alive(0) and not cc.alive(-1))

print()
print("=== scheduler words ===")
ck("LSF EXIT is a failure", "EXIT" in cc.FAILED_STATES)
ck("PBS asks for the exit status once the job is finished",
   "Exit_status" in cc.PBS_STATE_CMD and "job_state" in cc.PBS_STATE_CMD)

print()
print("=== what stays outside the wrapper ===")
cfg = cc.load_config()
def verdict(c):
    return cc.classify_command(c, {}, cfg)
v = verdict("cd /tmp && python train.py")
ck("a leading cd stays in the caller's shell", v["track"] and v.get("prefix") == "cd /tmp && "
   and v.get("body") == "python train.py", str(v))
v = verdict("export X=1 && make check")
ck("so does a leading export", v["track"] and v.get("prefix") == "export X=1 && ", str(v))
ck("set -e is fine inside the wrapper", verdict("set -euo pipefail && python train.py")["track"])
v = verdict("python train.py && cd out")
ck("a cd after the work is a suffix, back in the caller's shell", v["track"] and v.get("suffix") == " && cd out" and v.get("body") == "python train.py", str(v))
ck("a quoted cd is not split", not verdict("cd 'a b' && python train.py")["track"])
# A command that detaches itself is not wrapped - there would be nothing to
# wait on - but followed by the pid it leaves in $!.
v = verdict("python train.py &")
ck("a self-backgrounded job is followed, not wrapped", v["track"] and v.get("detached") and "body" not in v, str(v))
v = verdict("nohup python train.py &")
ck("nohup likewise, reading nohup.out", v["track"] and (v.get("detached") or {}).get("log") == "nohup.out", str(v))
ck("AGENT_PROGRESS_NO_AUTO=1 in front of the command works",
   not verdict("AGENT_PROGRESS_NO_AUTO=1 python train.py")["track"])
ck("an inline assignment before the command is fine", verdict("FOO=1 python train.py")["track"])
ck("agent-progress after a cd is not re-wrapped",
   not verdict("cd repo && agent-progress run -- python train.py")["track"])
ck("the name comes from the work, not the cd", verdict("cd /tmp && python train.py")["name"] == "train")
ck("clip adds no escape code to a plain line", "\033" not in cc.clip("a" * 50, 20))
ck("but keeps colour balanced when there was some", cc.clip("\033[31m" + "a" * 50, 20).endswith("\033[0m"))

print()
print("=== the prefix split knows its limits ===")
cfg = load_cfg = cc.load_config()
def verdict(c):
    return cc.classify_command(c, {}, cfg)
ck("a continuation line inside a cd is not cut", not verdict("cd /tmp \\\n  && python train.py")["track"])
ck("source is never split from the work: the command is left whole",
   not verdict("source lib.sh && python train.py")["track"] and not verdict(". lib.sh; python train.py")["track"])
v = verdict("python train.py \\\n  model=resnet \\\n  data=cifar")
ck("hydra overrides on continuation lines are still tracked", v["track"], v["why"])
ck("make VAR=x on a continuation line is still tracked", verdict("make \\\n  CC=gcc")["track"])
ck("an & inside a trailing comment is not backgrounding", verdict("pytest # trailing &")["track"]
   and verdict("pytest; # &")["track"])
ck("a real trailing & still is", verdict("pytest &").get("detached") and verdict("pytest 2>&1 &").get("detached"))
ck("a && b is not backgrounding", verdict("make && python train.py")["track"])
ck("./train.sh is named train", cc.suggest_job_name("./train.sh") == "train", cc.suggest_job_name("./train.sh"))
ck("./scripts/run_eval.sh --x is named run_eval", cc.suggest_job_name("./scripts/run_eval.sh --x") == "run_eval",
   cc.suggest_job_name("./scripts/run_eval.sh --x"))
ck("bash train.sh is named train", cc.suggest_job_name("bash train.sh") == "train", cc.suggest_job_name("bash train.sh"))
for text, want in (("10 msec", 0.01), ("5 secs", 5), ("3 hrs", 10800), ("2 days", 172800), ("1 hour 5 minutes", 3900)):
    ck("%r -> %s" % (text, want), abs(cc.parse_duration(text) - want) < 1e-9, str(cc.parse_duration(text)))
for text in ("1 month", "3 hz", "2 dozen", "1 mile"):
    try:
        cc.parse_duration(text)
        ck("%r is refused" % text, False, "accepted")
    except SystemExit:
        ck("%r is refused" % text, True)

print()
print("=== a job cut short by its caller is not a crash ===")
ev = {"kind": "crash", "job": "train", "exit_code": 143, "reason_short": "SIGTERM", "reason": "SIGTERM - terminated",
      "cmd": "python train.py", "log": "/nonexistent/train.log", "log_tail": "epoch 1", "duration": 120,
      "note": "killed by SIGTERM", "auto_launched": True}
rep = cc.format_report(ev)
ck("the report says stopped, not crashed", "STOPPED" in rep and "CRASHED" not in rep, rep[:80])
ck("and tells Claude the likely cause and the way out", "timeout" in rep and "agent-progress run --name train" in rep, rep[-300:])
ck("and does not point at a log that is gone", "log:" not in rep and "agent-progress log" not in rep, rep)
side = cc.format_beside(ev)
ck("the person's version says the same in a line", "timeout" in side and "agent-progress run" in side, side)
ev2 = dict(ev, note=None, auto_launched=False, exit_code=1, reason_short="exit 1")
ck("an ordinary failure is still called a crash", "CRASHED" in cc.format_report(ev2) and "timeout" not in cc.format_report(ev2))
ev3 = dict(ev, auto_launched=False)
ck("a run job killed by a signal is a crash, not a timeout", "timeout" not in cc.format_report(ev3))
j = {"state": "running", "started": time.time() + 3600, "samples": []}
ck("a start in the future - another machine's clock - is not a negative elapsed", cc.estimate(j)["elapsed"] == 0.0,
   str(cc.estimate(j)["elapsed"]))

ev = {"kind": "crash", "job": "longjob", "exit_code": 143, "reason_short": "SIGTERM", "reason": "SIGTERM",
      "cmd": "echo starting; sleep 60; echo never", "log": "/nonexistent", "log_tail": "", "duration": 5,
      "note": "killed by SIGTERM", "auto_launched": True}
rep = cc.format_report(ev)
ck("the relaunch line Claude is given keeps a compound command whole",
   "-- 'echo starting; sleep 60; echo never'" in rep, rep[-200:])

print()
print("=== set stays with the work; shopt and trap leave the command alone ===")
cfg = cc.load_config()
v = cc.classify_command("set -e; python train.py; echo after", {}, cfg)
ck("set -e is neither a prefix nor a blocker", v["track"] and v.get("prefix") == "" and v.get("body", "").startswith("set -e"), str(v))
v = cc.classify_command("set -o pipefail && python train.py | tee log", {}, cfg)
ck("set -o pipefail likewise", v["track"] and v.get("prefix") == "", str(v))
ck("shopt leaves the command alone", not cc.classify_command("shopt -s globstar && python train.py", {}, cfg)["track"])
ck("trap leaves the command alone", not cc.classify_command("trap 'echo bye' EXIT; python train.py", {}, cfg)["track"])
ck("ulimit as a leading line is a prefix", cc.classify_command("ulimit -n 4096 && python train.py", {}, cfg).get("prefix") == "ulimit -n 4096 && ")

print()
print("=== a leading assignment stays outside and is restated inside ===")
cfg = cc.load_config()
v = cc.classify_command("cd /tmp && J=/tmp/w && cat > $J/a.py && python3 $J/a.py", {"timeout": 600000}, cfg)
ck("`J=...` after a cd, and the quick `cat`, are setup outside", v["track"] and v.get("prefix") == "cd /tmp && J=/tmp/w && cat > $J/a.py && ", str(v.get("prefix")))
ck("and the assignment is restated at the front of the work", v.get("body") == "J=/tmp/w; python3 $J/a.py", str(v.get("body"))[:60])
v = cc.classify_command("J=~/.claude/jobs/x/tmp; python3 $J/verify.py", {"timeout": 600000}, cfg)
ck("a tilde value is a plain value", v["track"] and v.get("prefix") == "J=~/.claude/jobs/x/tmp; ", str(v.get("prefix")))
# A computed or quoted value cannot be hoisted, but it need not be: everything
# that reads it is on the line, and the whole line runs in the wrapper's shell.
for c in ("J=$(mktemp -d) && python3 $J/x.py", "J='a b' && python3 x.py", 'J="a b" && python3 x.py'):
    v = cc.classify_command(c, {"timeout": 600000}, cfg)
    ck("%s is wrapped whole, the assignment inside" % c[:24], v["track"] and v.get("prefix") == "" and v.get("body") == c, str(v)[:120])
v = cc.classify_command("python3 x.py; RESULT=1", {"timeout": 600000}, cfg)
ck("a trailing literal assignment is a suffix", v["track"] and v.get("suffix") == "; RESULT=1", str(v))
ck("an inline assignment before the command is still fine", cc.classify_command("LOKY_MAX_CPU_COUNT=2 python3 train.py", {"timeout": 600000}, cfg)["track"])

print()
print("=== the scanner honours quotes and heredocs ===")
segs = [st.strip() for _a, _b, _sep, st in cc.scan_shell("git commit -m 'a; b && c' && S=/x && python3 $S")]
ck("separators inside single quotes do not split", segs == ["git commit -m 'a; b && c'", "S=/x", "python3 $S"], str(segs))
segs = [st.strip() for _a, _b, _sep, st in cc.scan_shell('python3 -c "\nrng=1\nprint(rng)\n" && echo done')]
ck("nor inside double quotes across lines", len(segs) == 2 and segs[1] == "echo done", str(segs))
segs = cc.scan_shell("cat > f <<'EOF'\nx=1; y=2\nEOF\npython3 f")
ck("a heredoc body is blanked, not split", [st.strip() for *_r, st in segs][-1] == "python3 f" and "x=1" not in segs[0][3], str([st.strip() for *_r, st in segs]))
segs = cc.scan_shell("python - <<'EOF' && echo EDITED\nprint(1)\nEOF\nrun.sh")
ck("a heredoc announced before && still owns the lines that follow", [st.strip() for *_r, st in segs][-1] == "run.sh" and "print" not in "".join(st for *_r, st in segs), str([st.strip() for *_r, st in segs]))
ck("an escaped quote is not a quote", [st.strip() for *_r, st in cc.scan_shell("echo it\\'s; ls")] == ["echo it\\'s", "ls"])
segs = [st.strip() for *_r, st in cc.scan_shell("J=$(cd a && pwd) && python3 train.py")]
ck("a separator inside $(...) does not split", segs == ["J=$(cd a && pwd)", "python3 train.py"], str(segs))
segs = [st.strip() for *_r, st in cc.scan_shell("J=`cd a; pwd`; X=$((1+2)) && python3 t.py")]
ck("nor inside backticks, and $((...)) closes", segs == ["J=`cd a; pwd`", "X=$((1+2))", "python3 t.py"], str(segs))

print()
print("=== every bar is named for what it runs ===")
# A bar used to be named for the command's first word: an estimate in front
# made it AGENT_PROGRESS_ETA, a captured submission made it JOB, a loop `for`.
_names = [
    ("AGENT_PROGRESS_ETA=5m sbatch train.sbatch", "train"),
    ("AGENT_PROGRESS_ETA=10m make -j8", "make"),
    ("AGENT_PROGRESS_ETA=10m make test", "make-test"),
    ("AGENT_PROGRESS_ETA=10m pytest tests/", "pytest"),
    ("AGENT_PROGRESS_ETA=1h uv run python -m lm_eval --tasks mmlu", "lm_eval"),
    ("AGENT_PROGRESS_ETA=2h AGENT_PROGRESS_NAME=nightly python x.py", "nightly"),
    ("CUDA_VISIBLE_DEVICES=0,1 accelerate launch finetune.py", "finetune"),
    ("torchrun --nproc_per_node 8 pretrain.py", "pretrain"),
    ("python -m torch.distributed.run --nproc_per_node 4 train_ddp.py", "train_ddp"),
    ("JOB=$(sbatch --parsable train.sbatch) && echo $JOB", "train"),
    ("sbatch --job-name=sweep-a run.sbatch", "sweep-a"),
    ("sbatch --wrap 'python embed.py'", "embed"),
    ("for f in cfg/*.sbatch; do sbatch $f; done", "sbatch"),
    ("nohup python train.py > t.log 2>&1 &", "train"),
    ("timeout 2h python3 -u scripts/embed_corpus.py --n 8", "embed_corpus"),
    ("env OMP_NUM_THREADS=4 python predict.py", "predict"),
    ("srun --gres=gpu:1 python infer.py", "infer"),
    ("cd runs && python3 score.py", "score"),
    ("python src/main.py --config sweep_a.yaml", "main-sweep_a"),
    ("python experiments/run.py", "experiments-run"),
    ("python -c 'import time; time.sleep(100)'", "py-inline"),
    ("wget https://x.org/data/imagenet-val.tar", "dl-imagenet-val"),
    ("hf download meta-llama/Llama-3-8B", "dl-Llama-3-8B"),
    ("git clone https://github.com/csinva/imodels", "clone-imodels"),
    ("pip install torch torchvision", "pip-torch"),
    ("pip install -r requirements.txt", "pip-reqs"),
    ("npm test", "npm-test"),
    ("npm run build", "npm-build"),
    ("docker build -t myimg .", "build-myimg"),
    ("docker run --gpus all img python train.py", "train"),
    ("conda env create -f environment.yml", "conda-env"),
    ("sudo apt-get install -y ffmpeg", "apt-ffmpeg"),
    ("ffmpeg -i talk.mp4 talk.mkv", "ffmpeg-talk"),
    ("tar czf backup.tgz /data", "tar-backup"),
    ("rsync -av data/ box:/data", "rsync-data"),
    ("latexmk -pdf paper.tex", "tex-paper"),
    ("terraform apply", "tf-apply"),
    ("sleep 600", "sleep-600"),
    ("./scripts/run_eval.sh --x", "run_eval"),
]
_bad = [(c, cc.suggest_job_name(c), want) for c, want in _names if cc.suggest_job_name(c) != want]
ck("%d commands are each named for their work" % len(_names), not _bad, str(_bad))
_every = [c for c, _w in _names] + ["X=1 Y=2 Z=3 ./go", "JOB=$(qsub run.pbs)", "while true; do python poll.py; done",
                                     "for i in 1 2; do python embed_multilingual_retrieval_corpus.py; done"]
_vague = [(c, n) for c in _every for n in [cc.suggest_job_name(c)]
          if n.lower() in cc._VAGUE or re.match(r"(?i)agent[-_]progress", n) or len(n) > cc.NAME_MAX]
ck("none is vague, an env variable, or longer than %d" % cc.NAME_MAX, not _vague, str(_vague))
ck("a long name is shortened the usual way, at a word",
   cc.suggest_job_name("python embed_multilingual_retrieval_corpus.py") == "embed_multi_retr",
   cc.suggest_job_name("python embed_multilingual_retrieval_corpus.py"))
ck("a name that fits is left whole", cc.abbreviate("predict") == "predict")
ck("the classifier names detached and assignment-led commands the same way",
   cc.classify_command("AGENT_PROGRESS_ETA=3h nohup make -j8 > b.log 2>&1 &", {}, cc.load_config())["name"] == "make"
   and cc.classify_command("JOB=$(sbatch --parsable eval.sbatch)", {}, cc.load_config())["name"] == "eval")

print()
print("=== an assignment is only an assignment ===")
for text, want in (("J=$(sbatch --parsable a.sbatch)", True), ("J=$(a $(b) c)", True), ("J='a b'", True),
                   ('J="$(x)"', True), ("J=`x y`", True), ("J=plain", True),
                   ("FOO=1 python train.py", False), ("J=$(x) python t.py", False), ("python t.py", False),
                   ("=x", False)):
    ck("%r -> %s" % (text, want), cc._is_assignment_only(text) == want)

print()
print("=== submitting to slurm the way the skill says to is tracked ===")
cfg = cc.load_config()
for c in ("JOB=$(sbatch --parsable train.sbatch)",
          "JOB=$(sbatch --parsable train.sbatch) && echo $JOB",
          "JOB=$(sbatch --parsable a.sbatch) && sbatch --dependency=afterok:$JOB b.sbatch",
          "cd runs && JOB=$(sbatch --parsable a.sbatch) && echo $JOB"):
    v = cc.classify_command(c, {}, cfg)
    ck("tracked: %s" % c[:56], v["track"] and v.get("body") and "sbatch" in v["body"], str(v)[:140])
v = cc.classify_command("cd runs && JOB=$(sbatch --parsable a.sbatch) && echo $JOB", {}, cfg)
ck("the cd still stays in the caller's shell", v.get("prefix") == "cd runs && ", str(v.get("prefix")))
ck("a cd in the middle still leaves the line alone",
   not cc.classify_command("JOB=$(sbatch --parsable a.sbatch) && cd out && echo $JOB", {}, cfg)["track"])

print()
print("=== broader coverage: the long commands a session actually types ===")
_tracked = [
    "srun --gres=gpu:1 python run.py", "python predict.py --split test", "python embed_corpus.py",
    "uv run preprocess.py", "bash run_all.sh", "./scripts/generate_data.sh",
    "python run.py --epochs 50", "python main.py --max_steps=10000", "python app.py --multirun lr=1,2",
    "wget https://x.org/big.tar.gz", "curl -L --output m.bin https://x", "hf download meta-llama/x",
    "huggingface-cli upload me/x .", "aria2c -x8 https://x", "gdown 1abc", "rclone sync s3:b ./l",
    "scp -r box:/data .", "git lfs pull", "docker pull nvcr.io/x:1", "ollama pull llama3",
    "pip install torch", "uv sync", "uv pip install -r req.txt", "poetry install",
    "conda env create -f env.yml", "mamba install pytorch", "npm ci", "pnpm install",
    "apt-get install -y ffmpeg", "brew install llvm", "cargo install ripgrep",
    "ffmpeg -i in.mp4 out.mkv", "tar czf b.tgz /data", "tar -xf data.tar", "unzip data.zip",
    "zstd -19 big", "ls *.txt | xargs -P 8 -n 1 gzip", "parallel python run.py ::: 1 2 3",
    "mypy src/", "pyright", "tsc -p .", "pre-commit run --all-files", "jest", "npx vitest run",
    "playwright test", "ninja -C build", "ctest --output-on-failure", "dotnet test",
    "latexmk -pdf paper.tex", "pdflatex paper.tex", "sphinx-build docs out", "quarto render",
    "mkdocs build", "psql -d db -f big.sql", "mysql db < dump.sql", "mongorestore dump/",
    "bq load ds.t gs://x", "helm upgrade --install x ./chart", "kubectl wait --for=condition=complete job/x",
    "packer build x.pkr.hcl", "Rscript analysis.R", "julia sim.jl", "matlab -batch run",
    "snakemake -j 8", "nextflow run main.nf", "papermill in.ipynb out.ipynb",
    "jupyter nbconvert --execute nb.ipynb", "lm_eval --model hf --tasks mmlu", "tune run lora",
    "sky launch task.yaml", "modal run app.py", "timeout 2h python x.py", "timeout -s KILL 3600 python x.py",
]
_missed = [c for c in _tracked if not cc.classify_command(c, {}, cfg)["track"]]
ck("%d long commands are all tracked" % len(_tracked), not _missed, str(_missed))
_left = [
    # servers and watchers: a bar for one could never finish
    "uvicorn app:main --reload", "python -m http.server 8000", "jupyter lab", "tensorboard --logdir runs",
    "streamlit run app.py", "vllm serve meta-llama/x", "npm run dev", "yarn start", "tail -f train.log",
    "watch -n 5 nvidia-smi", "tsc --watch", "jest --watchAll", "mkdocs serve", "flask run",
    # and the ordinary quick things
    "ls -la", "pip list", "npm ls", "echo running parallel jobs", "timeout 5 python x.py",
    "git status", "cat train.log",
]
_caught = [c for c in _left if cc.classify_command(c, {}, cfg)["track"]]
ck("%d servers, watchers and quick commands are left alone" % len(_left), not _caught, str(_caught))
v = cc.classify_command("timeout 3h python x.py", {}, cfg)
ck("a timeout prefix is read as the caller's own bound", v["signal"] == "timeout" and "3h" in v["why"], str(v))

print()
print("=== a script run by path is judged by what is in it ===")
_sd = tempfile.mkdtemp(prefix="agent-progress-scripts-")
open(os.path.join(_sd, "submit_all.sh"), "w").write("#!/bin/bash\nfor f in cfg/*.sbatch; do\n  sbatch $f\ndone\n")
open(os.path.join(_sd, "serve.sh"), "w").write("#!/bin/bash\nsbatch warm.sbatch\nuvicorn app:main\n")
open(os.path.join(_sd, "notes.sh"), "w").write("#!/bin/bash\n# remember to sbatch this later\necho hi\n")
os.makedirs(os.path.join(_sd, "scripts"))
open(os.path.join(_sd, "scripts", "go"), "w").write("#!/bin/sh\npython3 -m torch.distributed.run x.py\n")
open(os.path.join(_sd, "blob.sh"), "wb").write(b"\x7fELF\0\0sbatch")
v = cc.classify_command("bash submit_all.sh", {}, cfg, cwd=_sd)
ck("a script that submits is tracked, and says why", v["track"] and v["signal"] == "script"
   and "submit_all.sh" in v["why"] and "batch submission" in v["why"], str(v)[:160])
ck("./scripts/go by its contents too", cc.classify_command("./scripts/go", {}, cfg, cwd=_sd)["track"])
ck("an absolute path too", cc.classify_command("sh %s/submit_all.sh" % _sd, {}, cfg)["track"])
ck("a script that starts a server is not", not cc.classify_command("bash serve.sh", {}, cfg, cwd=_sd)["track"])
ck("a command in a comment is not a command", not cc.classify_command("bash notes.sh", {}, cfg, cwd=_sd)["track"])
ck("a binary is not read as a script", not cc.classify_command("bash blob.sh", {}, cfg, cwd=_sd)["track"])
ck("a script that is not there is simply not read", not cc.classify_command("bash nope.sh", {}, cfg, cwd=_sd)["track"])
ck("bash -c '...' is not a script path", not cc.classify_command("bash -c 'echo hi'", {}, cfg, cwd=_sd)["track"])
shutil.rmtree(_sd, ignore_errors=True)

print()
print("=== a command that detaches itself ===")
line, fg, log = cc.split_detached("nohup python train.py > train.log 2>&1 &  # overnight")
ck("the line runs to its &, the comment dropped", line == "nohup python train.py > train.log 2>&1 &", repr(line))
ck("the log is where stdout goes, not stderr", log == "train.log" and fg == "nohup python train.py > train.log 2>&1", repr((fg, log)))
ck("appending counts", cc.split_detached("python x.py >> out.txt 2>/dev/null &")[2] == "out.txt")
ck("tee counts", cc.split_detached("python train.py | tee -a run.log &")[2] == "run.log")
ck("no redirect and no nohup: no log", cc.split_detached("python train.py &")[2] is None)
ck("a cd inside the background list makes a relative log unknowable",
   cc.split_detached("cd runs && python train.py > t.log &")[2] is None)
ck("two jobs in the background are not one", cc.split_detached("python a.py & python b.py &") is None)
ck("an & in quotes is not a job", cc.split_detached("echo 'a & b'") is None)
ck("&& is not &", cc.split_detached("make && python train.py") is None)
v = cc.classify_command("nohup python train.py > train.log 2>&1 &", {}, cfg)
ck("a detached training run is tracked and named", v["track"] and v["name"] == "train" and v["detached"]["log"] == "train.log", str(v)[:160])
v = cc.classify_command("AGENT_PROGRESS_ETA=3h nohup ./go > g.log 2>&1 &", {}, cfg)
ck("an estimate on a detached command is kept", v["track"] and v.get("eta") == "3h", str(v)[:160])
for c in ("nohup python -m http.server 8000 &", "sleep 5 &", "tensorboard --logdir runs > tb.log 2>&1 &"):
    ck("left alone: %s" % c, not cc.classify_command(c, {}, cfg)["track"])
w = cc.detached_command("nohup python train.py > t.log 2>&1 &", "train", log="t.log", eta="2h",
                        foreground="nohup python train.py > t.log 2>&1")
ck("the rewrite keeps the line and adds a start by pid", w.startswith("nohup python train.py > t.log 2>&1 & ")
   and "start train --pid $! --auto-launched --log t.log --eta 2h" in w, w)

print()
print("=== setup stays outside, the work inside, a trailing cd after ===")
cfg = cc.load_config()
def split(c):
    return cc.split_shell_prefix(c)
pre, body, suf = split("git add a.py && git commit -q -m 'fix; all' && S=/tmp/x && python3 $S/run.py")
ck("trivial setup and a literal assignment go outside", pre == "git add a.py && git commit -q -m 'fix; all' && S=/tmp/x && ", repr(pre))
ck("the assignment is restated at the front of the work", body.startswith("S=/tmp/x; python3 $S/run.py"), repr(body))
pre, body, suf = split("python3 x.py 4; cd sub")
ck("a trailing cd is a suffix", body == "python3 x.py 4" and suf == "; cd sub", repr((body, suf)))
pre, body, suf = split("python3 a.py && S=/tmp/x && python3 $S/b.py")
ck("a literal assignment mid-line is hoisted before the wrapper", pre == "S=/tmp/x; " and body == "python3 a.py && S=/tmp/x && python3 $S/b.py", repr((pre, body)))
pre, body, suf = split("python3 a.py && export X=1 && python3 b.py")
ck("an export mid-line is not hoisted; the line is left alone", body is None)
pre, body, suf = split("sleep 60; cd proj && python3 a.py")
ck("a cd in the middle of the work leaves the line alone", body is None)
v = cc.classify_command("python3 -c \"\nrng=1\nd=dict(a=1)\nprint(rng)\n\"", {"timeout": 600000}, cfg)
ck("python assignments inside -c are not shell state", v["track"], v["why"])

print()
print("=== every bar has an estimate: Claude's, then history, then a bound, then typical ===")
_now = time.time()
def _done(jid, cmd, dur, auto=True):
    return {"id": jid, "state": "done", "cmd": cmd, "started": _now - dur - 100, "ended": _now - 100, "auto_launched": auto}
st = {"jobs": {}, "history": {}}
ck("with nothing to go on there is no prior", cc.choose_prior(st, cmd="python x.py", name="x") == (None, None, 0))
ck("Claude's figure wins outright", cc.choose_prior(st, cmd="python x.py", name="x", eta=600, bound=1200) == (600, "claude", 0))
ck("with no history, the tool's timeout is the bound", cc.choose_prior(st, cmd="python x.py", name="x", bound=1200) == (1200, "bound", 0))
st["jobs"]["x"] = _done("x", "python x.py", 300)
cc._remember_finished(st)
ck("a finished job leaves its duration in the history by name", st["history"].get("x") == [300.0], str(st.get("history")))
ck("and is not counted twice", (cc._remember_finished(st), st["history"]["x"])[1] == [300.0])
secs, src, n = cc.choose_prior(st, cmd="python x.py", name="x", bound=1200)
ck("history beats the bound", (secs, src, n) == (300.0, "history", 1), str((secs, src, n)))
st["history"]["x"] = [300.0, 340.0, 280.0]
ck("several runs by name give their median", cc.choose_prior(st, cmd="python other.py", name="x-3")[0] == 300.0)
ck("the estimate strips the hint from the command text when matching",
   cc._strip_hints("AGENT_PROGRESS_ETA=5m python x.py") == "python x.py")
st = {"jobs": {k: _done(k, "cmd %s" % k, d) for k, d in (("a", 40), ("b", 60), ("c", 50))}}
cc._remember_finished(st)
ck("three finished tracked jobs give a typical figure for a stranger", cc.choose_prior(st, cmd="new", name="new") == (50, "typical", 3), str(cc.choose_prior(st, cmd="new", name="new")))
st["jobs"]["z"] = {"id": "z", "state": "running", "started": _now - 30, "eta_end": _now + 90, "eta_prior_s": 120, "eta_prior_source": "bound", "samples": []}
e = cc.estimate(st["jobs"]["z"], _now)
ck("the estimator reports the prior's source", e["source"] == "bound" and abs(e["remaining"] - 90) < 1, str(e["source"]))
line = cc.render_line(st["jobs"]["z"], dict(cc.load_config(), color=False), width=100)
ck("and a bound is drawn as an upper bound", re.search(r"<\u226401:(29|30)", line) is not None, line)
st["jobs"]["z"]["eta_prior_source"] = "history"
ck("a history figure as a guess", re.search(r"<~01:(29|30)", cc.render_line(st["jobs"]["z"], dict(cc.load_config(), color=False), width=100)) is not None)
hist = {"history": {"n%d" % i: [1.0] for i in range(cc.HISTORY_NAMES + 5)}, "jobs": {}}
cc.remember_duration(hist, "late", {"state": "done", "started": 10, "ended": 70})
ck("the history is bounded by name", len(hist["history"]) <= cc.HISTORY_NAMES, str(len(hist["history"])))

print()
print("=== past its estimate, a job is re-estimated, and the old figure stays visible ===")
ck("under the estimate nothing changes", cc.revised_total(34, 20) == (34.0, 0))
ck("at the estimate it grows by half", cc.revised_total(34, 34) == (51.0, 1))
ck("and again each time the clock catches it", cc.revised_total(34, 51)[1] == 2 and abs(cc.revised_total(34, 51)[0] - 76.5) < 1e-9)
ck("a missing prior gives nothing", cc.revised_total(None, 50) == (None, 0))
ck("a runaway job is bounded", cc.revised_total(1, 10 ** 12)[1] <= 40)
_now = time.time(); _cfg = dict(cc.load_config(), color=False)
def _job(elapsed, **extra):
    j = {"id": "j", "state": "running", "started": _now - elapsed, "eta_end": _now - elapsed + 34, "eta_prior_s": 34,
         "initial_est_total_s": 34, "eta_prior_source": "claude", "samples": []}
    j.update(extra); return j
e = cc.estimate(_job(20), _now, _cfg)
ck("before the estimate: Claude's figure, no revision", e["source"] == "claude" and e["revisions"] == 0)
e = cc.estimate(_job(40), _now, _cfg)
ck("past it: revised, with time remaining rather than none", e["source"] == "revised" and e["revisions"] == 1 and abs(e["remaining"] - 11) < 0.5, str((e["source"], e["remaining"])))
ck("and the bar falls back from its ceiling", cc.estimate(_job(33), _now, _cfg)["frac"] > 0.95 > cc.estimate(_job(34), _now, _cfg)["frac"])
ck("then climbs again", cc.estimate(_job(50), _now, _cfg)["frac"] > cc.estimate(_job(40), _now, _cfg)["frac"])
line = re.sub(r"\s+", " ", cc.render_line(_job(40), _cfg, width=120))
ck("the bar shows the new figure beside the old one", "est 51s (was 34s)" in line and "<~00:1" in line, line)
line = re.sub(r"\s+", " ", cc.render_line(_job(60), _cfg, width=120))
ck("to the second, not rounded to a minute", "est 1m16s (was 34s)" in line, line)
ck("and no longer says merely 'past estimate'", "past estimate" not in line)
m = _job(60, total=100, units=50.0, step=50, samples=[[_now - 60, 0.0], [_now - 30, 25.0], [_now, 50.0]])
e = cc.estimate(m, _now, _cfg)
ck("measured progress past the estimate wins outright, unrevised", e["source"] == "measured" and e["revisions"] == 0 and abs(e["remaining"] - 60) < 5, str((e["source"], e["revisions"], e["remaining"])))
line = re.sub(r"\s+", " ", cc.render_line(m, _cfg, width=120))
ck("and its bar shows the measured figure beside the old one, in the same form", "(was 34s)" in line and "(+" not in line, line)
# a measured job just past its estimate, within the drift threshold: this used
# to fall through every branch and print a bare "(past estimate)" next to a
# remaining time that had in fact been re-estimated from the log
m = {"id": "j", "state": "running", "started": _now - 101, "eta_end": _now - 1, "eta_prior_s": 100, "initial_est_total_s": 100,
     "eta_prior_source": "claude", "total": 100, "units": 90.0, "step": 90, "samples": [[_now - 101, 0.0], [_now - 50, 45.0], [_now, 90.0]]}
e = cc.estimate(m, _now, _cfg)
line = re.sub(r"\s+", " ", cc.render_line(m, _cfg, width=120))
ck("a measured job just past its estimate is re-estimated from its own rate", e["source"] == "measured" and e["overdue"] and abs(e["remaining"] - 11) < 2, str((e["source"], e["overdue"], e["remaining"])))
ck("and says so, rather than 'past estimate'", "est 1m5" in line and "(was 1m40s)" in line and "past estimate" not in line, line)
m.pop("initial_est_total_s")
line = re.sub(r"\s+", " ", cc.render_line(m, _cfg, width=120))
ck("a record with no initial figure still shows the re-estimate", "(was 1m40s)" in line and "past estimate" not in line, line)
# under the estimate, a material drift keeps its own form: the change, not a 'was'
u = _job(60, eta_end=_now - 60 + 1000, eta_prior_s=1000, initial_est_total_s=1000, total=100, units=50.0, step=50,
         samples=[[_now - 60, 0.0], [_now - 30, 25.0], [_now, 50.0]])
line = re.sub(r"\s+", " ", cc.render_line(u, _cfg, width=120))
ck("under the estimate, drift is still shown as a change", re.search(r"est \S+ \(-", line) is not None and "(was" not in line, line)
d = _job(60, state="done", ended=_now - 1)
ck("a finished job is not revised", cc.estimate(d, _now, _cfg)["revisions"] == 0)

print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)

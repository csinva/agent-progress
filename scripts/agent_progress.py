#!/usr/bin/env python3
"""agent-progress - tqdm-style progress bars for long-running jobs, driven by Claude Code.

Design in one paragraph: a job's ETA starts as Claude's *prior* (a guess made from
reading the training script), and is progressively replaced by a *measured* rate
scraped out of the job's log by a small background watcher. The statusline renderer
blends the two, so the bar is useful from second one and accurate by the end.

No third-party dependencies. Python 3.8+.
"""


import argparse
import contextlib
import errno
import difflib
import fcntl
import glob as globmod
import hashlib
import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import unicodedata

# --------------------------------------------------------------------------- paths

HOME = os.path.expanduser("~")
ROOT = os.environ.get("AGENT_PROGRESS_HOME") or os.path.join(HOME, ".claude", "agent-progress")
STATE = os.path.join(ROOT, "state.json")
LOCK = os.path.join(ROOT, ".lock")
LOGS = os.path.join(ROOT, "logs")
CONFIG = os.path.join(ROOT, "config.json")

STATE_VERSION = 1

# A job that is alive: either doing the work, or waiting in a queue for its turn
# to. "queued" exists because a scheduler job spends real time - sometimes most
# of its life - having been submitted and not yet started, and calling that
# "running" makes every number wrong: the elapsed clock counts queue time as run
# time, the throughput is measured against work that has not begun, and the bar
# claims progress on a job that has produced nothing.
ACTIVE_STATES = ("running", "queued")

# The user's command runs under the same shell the tool it came from would
# have used. Claude Code runs Bash commands with bash; on Linux /bin/sh is
# often dash, where `[[ ]]`, `set -o pipefail`, arrays and `<( )` all fail -
# and a command that works unwrapped and breaks wrapped is the plugin changing
# its output. The probes a user writes for the watcher are documented as sh
# and stay on it.
USER_SHELL = shutil.which("bash") or "/bin/sh"

# --------------------------------------------------------------------- config
#
# Every tunable lives in one table: default, type, valid range, and a one-line
# explanation. `agent-progress config` renders this, validates writes against it,
# and no default is hard-coded anywhere else in the file.


def _spec(group, default, help, type="int", choices=None, lo=None, hi=None):
    return {"group": group, "default": default, "help": help,
            "type": type, "choices": choices, "lo": lo, "hi": hi}


CONFIG_SPEC = {
    # what shows up at all
    "min_duration_seconds": _spec(
        "visibility", 120,
        "hide jobs expected to take less than this many seconds (0 = show all)", lo=0),
    "max_jobs": _spec("visibility", 3, "bars shown in the statusline at once", lo=1),
    "keep_done_seconds": _spec("visibility", 300, "how long a finished job lingers", lo=0),
    "keep_done_prompts": _spec(
        "visibility", 2,
        "how many of your messages a finished bar survives (0 to go by time alone)",
        lo=0),
    "scope": _spec("visibility", "session",
                   "whose jobs a statusline shows: this session's, or every session's",
                   "str", choices=["session", "all"]),
    "keep_failed_seconds": _spec("visibility", 1800,
                                 "how long a crashed job stays on the statusline", lo=0),
    "prune_after_hours": _spec("visibility", 48, "forget finished jobs after this", lo=0),
    "show_context_line": _spec("visibility", True,
                               "show model/dir/branch when no bar is visible", "bool"),

    # how often a job is re-observed
    "min_interval_seconds": _spec("cadence", 120,
                                  "never re-observe a job more often than this", lo=1),
    "interval_fraction": _spec("cadence", 0.05,
                               "nor more often than this fraction of the estimated total",
                               "float", lo=0.0, hi=1.0),

    # bar shape
    "style": _spec("bar", "blocks", "preset bar characters", "str",
                   choices=["blocks", "tqdm", "ascii", "dots", "bars"]),
    "bar_width": _spec("bar", 22, "width of the bar, in cells", lo=4, hi=200),
    "name_width": _spec("bar", 18, "truncate job names to this many characters", lo=4),
    "fill_char": _spec("bar", "", "override the filled character (empty = use style)", "str"),
    "track_char": _spec("bar", "", "override the unfilled character", "str"),
    "left_cap": _spec("bar", "", "override the left bracket", "str"),
    "right_cap": _spec("bar", "", "override the right bracket", "str"),
    "spinner": _spec("bar", "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f",
                     "spinner frames for a running job", "str"),
    "spinner_fps": _spec("bar", 8, "spinner frames per second", "float", lo=0.0),
    "glyph_done": _spec("bar", "\u2713", "marker for a job that finished cleanly", "str"),
    "glyph_failed": _spec("bar", "\U0001f480", "marker for a job that crashed", "str"),
    "glyph_cancelled": _spec("bar", "\u25a0", "marker for a job you stopped", "str"),
    "glyph_stalled": _spec("bar", "\u23f8", "marker for a job that stopped making progress", "str"),
    "glyph_queued": _spec("bar", "\u23f3", "marker for a job waiting in a scheduler queue", "str"),

    # which fields appear on the line
    "show_spinner": _spec("fields", True, "leading spinner / status glyph", "bool"),
    "show_name": _spec("fields", True, "the job name", "bool"),
    "show_percent": _spec("fields", True, "the percentage", "bool"),
    "show_counts": _spec("fields", True, "the step/total counter", "bool"),
    "show_clock": _spec("fields", True, "the elapsed<remaining pair", "bool"),
    "show_rate": _spec("fields", True, "throughput, e.g. 31.2s/ep", "bool"),
    "show_eta_clock": _spec("fields", True, "wall-clock finish time, e.g. -> 16:18", "bool"),
    "show_drift": _spec("fields", True, "flag when the estimate has moved a lot", "bool"),
    "show_note": _spec("fields", True, "the job's note", "bool"),
    "note_width": _spec("fields", 40, "truncate notes to this many characters", lo=4),
    "clock_format": _spec("fields", "%H:%M", "strftime format for the finish time", "str"),

    # color (256-color codes; see `agent-progress colors`)
    "color": _spec("color", True, "use color at all", "bool"),
    "color_running": _spec("color", 44, "bar and spinner while running", lo=0, hi=255),
    "color_done": _spec("color", 42, "a finished job", lo=0, hi=255),
    "color_failed": _spec("color", 203, "a failed or cancelled job", lo=0, hi=255),
    "color_warn": _spec("color", 179, "unmeasured estimates, drift, warnings", lo=0, hi=255),
    "color_dim": _spec("color", 244, "secondary text", lo=0, hi=255),
    "color_track": _spec("color", 238, "the unfilled part of the bar", lo=0, hi=255),
    "color_text": _spec("color", 252, "primary text", lo=0, hi=255),

    # estimation behavior
    "blend_full_at": _spec("estimation", 6,
                           "observations after which the measured rate fully replaces the prior", lo=1),
    "rate_window": _spec("estimation", 12, "samples used for the throughput estimate", lo=2),
    "rate_min_span": _spec("estimation", 3.0,
                           "seconds a sample window must cover before it is trusted", "float", lo=0.0),
    "drift_threshold": _spec("estimation", 0.2,
                             "relative change in the total estimate before it is flagged",
                             "float", lo=0.0, hi=10.0),

    # behavior
    "notify": _spec("behavior", True, "desktop notification on completion (macOS)", "bool"),
    "crash_handover_seconds": _spec(
        "behavior", 3600,
        "how long a crash waits for its own session before any session may take it",
        lo=0),
    "report_style": _spec(
        "behavior", "side",
        "how a job's end reaches you: 'side' beside the conversation, 'context' "
        "with your next message so Claude can act on it, 'off' not at all",
        "str", choices=["side", "context", "off"]),
    "announce_done": _spec(
        "behavior", True,
        "tell the session when a tracked job finishes, with the tail of its output",
        "bool"),
    "crash_alert": _spec("behavior", True,
                         "show a job's ending beside the conversation when Claude finishes a turn",
                         "bool"),
    "context_min_interval_seconds": _spec(
        "behavior", 300,
        "least time between unchanged job summaries sent to Claude", lo=0),

    # Automatic tracking: catch long jobs as they are launched, with no
    # /agent-progress:track needed. See `agent-progress autotrack`.
    "auto_track": _spec("auto", "defer",
                        "what to do when a long-running command is launched", "str",
                        choices=["defer", "instruct", "off"]),
    "auto_track_after_seconds": _spec(
        "auto", 20,
        "only start tracking once a command has run this long (0 = immediately)", lo=0),
    "auto_track_timeout_seconds": _spec(
        "auto", 120,
        "treat a command given at least this long a timeout as long-running", lo=0),
    "auto_track_background": _spec(
        "auto", True, "also catch commands launched in the background", "bool"),
    "auto_track_patterns": _spec(
        "auto", "", "extra ';'-separated regexes that mean 'this is a long job'", "str"),
    "auto_track_ignore": _spec(
        "auto", "", "';'-separated regexes to never auto-track", "str"),
    "notify_sound_ok": _spec("behavior", "Glass", "sound for a successful finish", "str"),
    "notify_sound_fail": _spec("behavior", "Basso", "sound for a failure", "str"),
}

DEFAULT_CONFIG = dict((k, s["default"]) for k, s in CONFIG_SPEC.items())

CONFIG_GROUPS = [
    ("visibility", "What appears"),
    ("cadence", "Update cadence"),
    ("bar", "Bar shape"),
    ("fields", "Fields on the line"),
    ("color", "Color (256-color codes)"),
    ("estimation", "Estimation"),
    ("behavior", "Behavior"),
    ("auto", "Automatic tracking"),
]

CONFIG_PRESETS = {
    "minimal": {"show_counts": False, "show_rate": False, "show_eta_clock": False,
                "show_note": False, "show_drift": False, "bar_width": 14, "name_width": 12},
    "rich": {"show_counts": True, "show_rate": True, "show_eta_clock": True,
             "show_note": True, "show_drift": True, "bar_width": 30, "max_jobs": 5},
    "tqdm": {"style": "tqdm", "show_eta_clock": False, "show_drift": False, "bar_width": 24},
    "plain": {"style": "ascii", "color": False, "show_spinner": False},
    "quiet": {"min_duration_seconds": 600, "max_jobs": 1, "notify": False},
    "manual": {"auto_track": "off"},
    "guided": {"auto_track": "instruct"},
    "eager": {"auto_track_after_seconds": 0},
}


def coerce(key, value):
    """Validate and convert one setting. Raises ValueError with a usable message."""
    spec = CONFIG_SPEC[key]
    t = spec["type"]
    if isinstance(value, str):
        s = value.strip()
        if t == "bool":
            if s.lower() in ("1", "true", "yes", "on"):
                value = True
            elif s.lower() in ("0", "false", "no", "off"):
                value = False
            else:
                raise ValueError("expected true or false")
        elif t in ("int", "float"):
            value = _number(s, key, t)
        else:
            value = s
    elif t == "bool":
        value = bool(value)
    elif t in ("int", "float"):
        value = _number(value, key, t)
    else:
        value = str(value)
    if key == "auto_track" and value == "wrap":
        value = "defer"          # the old name, kept working
    if spec["choices"] and value not in spec["choices"]:
        raise ValueError("must be one of: %s" % ", ".join(str(c) for c in spec["choices"]))
    if spec["lo"] is not None and value < spec["lo"]:
        raise ValueError("must be >= %s" % spec["lo"])
    if spec["hi"] is not None and value > spec["hi"]:
        raise ValueError("must be <= %s" % spec["hi"])
    return value


def ensure_dirs():
    for d in (ROOT, LOGS):
        try:
            os.makedirs(d)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise SystemExit("cannot use the agent-progress directory %s: %s" % (d, e))
        if not os.path.isdir(d):
            raise SystemExit("not a directory: %s" % d)


_CFG_CACHE = {"mtime": None, "cfg": None}


def load_config(force=False):
    """Defaults, overlaid with the config file, overlaid with the environment.

    Any setting can be overridden for one invocation as AGENT_PROGRESS_<KEY>, e.g.
    AGENT_PROGRESS_BAR_WIDTH=40. Cached on the config file's mtime because the
    statusline renders many times a second."""
    try:
        mtime = os.path.getmtime(CONFIG)
    except OSError:
        mtime = 0
    if not force and _CFG_CACHE["cfg"] is not None and _CFG_CACHE["mtime"] == mtime:
        return _CFG_CACHE["cfg"]

    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG) as f:
            for k, v in json.load(f).items():
                if k in CONFIG_SPEC:
                    try:
                        cfg[k] = coerce(k, v)
                    except ValueError:
                        pass          # a bad value in the file must not break rendering
    except Exception:
        pass
    for k in CONFIG_SPEC:
        env = os.environ.get("AGENT_PROGRESS_" + k.upper())
        if env is not None:
            try:
                cfg[k] = coerce(k, env)
            except ValueError:
                pass
    if os.environ.get("NO_COLOR"):
        cfg["color"] = False
    _CFG_CACHE["mtime"], _CFG_CACHE["cfg"] = mtime, cfg
    return cfg


# --------------------------------------------------------------------------- state

_NUMERIC_FIELDS = ("started", "updated", "ended", "eta_end", "eta_prior_s",
                   "est_total_s", "initial_est_total_s", "next_probe", "interval_s",
                   "interval_override", "units", "total", "pct", "step", "exit_code",
                   "pid", "watcher_pid", "log_offset", "size_bytes",
                   "submitted", "queued_seconds", "prompts_since_done", "waiter_pid")


def _sanitize(st):
    """Make a state document safe to walk.

    Several processes write this file, and it is a plain file on disk that
    anyone can edit or truncate. Everything below reads it many times a second
    to draw the statusline, so a value of the wrong shape must not become an
    exception - it becomes a missing value, and the bar simply knows less."""
    if not isinstance(st, dict):
        st = {}
    jobs = st.get("jobs")
    clean = {}
    if isinstance(jobs, dict):
        for jid, job in jobs.items():
            if not isinstance(job, dict):
                continue                    # not a job record; forget it
            for key in _NUMERIC_FIELDS:
                if key in job and not isinstance(job[key], (int, float)):
                    job[key] = None
                if isinstance(job.get(key), bool):
                    job[key] = None
                # inf and nan are floats too, and every renderer chokes on them;
                # so does an integer too big to become a float
                v = job.get(key)
                if isinstance(v, float) and not math.isfinite(v):
                    job[key] = None
                elif isinstance(v, int) and abs(v) > 10 ** 15:
                    job[key] = None
            for key in ("id", "state", "unit", "note", "desc", "cmd", "log", "pattern",
                        "state_probe", "queue_reason", "nodes", "partition",
                        "scheduler_state", "session_id", "bridge_id", "exit_file",
                        "host", "watcher_host"):
                if key in job and job[key] is not None and not isinstance(job[key], str):
                    job[key] = None
            job["id"] = job.get("id") or str(jid)
            job["state"] = job.get("state") or "running"
            if not isinstance(job.get("monitor"), dict):
                job["monitor"] = None
            samples = job.get("samples")
            job["samples"] = [
                list(pair) for pair in samples
                if isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                        for x in pair)
            ] if isinstance(samples, list) else []
            hit = job.get("milestones_hit")
            job["milestones_hit"] = [x for x in hit if isinstance(x, str)] \
                if isinstance(hit, list) else []
            clean[str(jid)] = job
    st["jobs"] = clean
    if not isinstance(st.get("inbox"), list):
        st["inbox"] = []
    # Events are shaped by us, but the file is on disk and every consumer -
    # the listing, the handover grace, the report itself - does arithmetic on
    # `ts`. A string there crashed `inbox` outright. Anything unusable becomes
    # now, which keeps the report deliverable and gives it a full grace window
    # rather than making it look ancient and up for grabs.
    _now = time.time()
    clean_inbox = []
    for e in st["inbox"]:
        if not isinstance(e, dict):
            continue
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
            e["ts"] = _now
        if e.get("delivered") is not None and not isinstance(e.get("delivered"), dict):
            e["delivered"] = None
        clean_inbox.append(e)
    st["inbox"] = clean_inbox
    # Every one of these maps is cleaned by a loop that reads inside its values.
    # A single value of the wrong shape makes that loop raise, and every caller
    # of these swallows exceptions, so the effect is not a crash but something
    # quieter and worse: cleaning up stops for good and the map grows without
    # limit for the life of the installation. Values are checked here, once, so
    # nothing malformed ever reaches a prune loop.
    def _timestamp(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for key in ("sessions", "auto_track_seen"):
        seen = st.get(key)
        st[key] = ({k: v for k, v in seen.items() if _timestamp(v)}
                   if isinstance(seen, dict) else {})
    sent = st.get("context_sent")
    st["context_sent"] = (
        {k: v for k, v in sent.items() if isinstance(v, dict) and _timestamp(v.get("ts"))}
        if isinstance(sent, dict) else {})
    st.setdefault("version", STATE_VERSION)
    return st


def _read_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    return _sanitize(st)


TEMP_GRACE = 300        # a live writer's temp file is seconds old, never minutes


def _sweep_temp_files():
    """Clear temp files left by writers that were killed before the rename.

    The write is a create-then-rename, so the state file itself is never half
    written - but a process killed in between leaves its temp file behind, and
    nothing ever came back for it. One is nothing; they accumulate for the life
    of the installation, each a full copy of the state, and a machine that
    interrupts commands often (a harness timeout, ctrl-c, a reboot) keeps
    making them. Anything older than the grace belongs to a process that is
    gone."""
    cutoff = time.time() - TEMP_GRACE
    try:
        names = os.listdir(ROOT)
    except OSError:
        return
    # ROOT, emphatically not HOME: HOME here is the user's home directory, and
    # sweeping that for anything with ".tmp." in the name would delete their
    # files. Only the plugin's own state directory, and only names this module
    # writes.
    for name in names:
        if not name.startswith(("state.json.tmp.", "config.json.tmp.")):
            continue
        path = os.path.join(ROOT, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass            # someone else got there first, or it is not ours to remove


def _write_state(st):
    tmp = STATE + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


class StateBusy(RuntimeError):
    """The state lock could not be taken in time."""


# One process holding this lock blocks every other one: the statusline reads
# without it, but every watcher, every finishing job and the hook in front of
# every Bash command take it. A blocking flock with no timeout turns any single
# wedged process - stopped, on a hung network filesystem, waiting on a probe -
# into a permanent stall of the whole plugin. Waiting a bounded time and then
# giving up is always better: every caller here can carry on knowing less.
LOCK_TIMEOUT = 20.0


@contextlib.contextmanager
def hold_lock(timeout=None):
    """Hold the plugin's one exclusive lock, or raise StateBusy trying."""
    ensure_dirs()
    if timeout is None:
        try:
            timeout = float(os.environ.get("AGENT_PROGRESS_LOCK_TIMEOUT") or LOCK_TIMEOUT)
        except (TypeError, ValueError):
            timeout = LOCK_TIMEOUT
    lf = open(LOCK, "a+")
    held = False
    try:
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except (IOError, OSError):
                if time.time() >= deadline:
                    raise StateBusy(
                        "the agent-progress state file stayed locked for %gs" % timeout)
                time.sleep(0.05)
        yield
    finally:
        try:
            if held:
                fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


@contextlib.contextmanager
def state_rw(timeout=None):
    """Read-modify-write the state file under an exclusive flock.

    Raises StateBusy rather than waiting forever. Callers on a path that must
    not fail - the hooks, the deferral wrapper - already treat any exception
    here as "skip the bookkeeping and get on with it", which is the right
    answer: the bar is worth less than the command."""
    with hold_lock(timeout):
        st = _read_state()
        yield st
        _prune(st)
        _write_state(st)


def state_ro():
    """Lock-free read. Rendering runs many times a second; a torn read just
    means one stale frame, which is cheaper than contending on the lock."""
    return _read_state()


SESSION_TTL = 30 * 86400
SESSION_CAP = 500       # more than anyone has open; the rest are history
FINISHED_CAP = 300      # finished jobs kept, however recent they are


def _prune_finished(st):
    """Keep the history of finished jobs from growing without limit.

    Age alone was the only bound, and a machine running many agents makes jobs
    far faster than two days retires them - a minute of eight agents made four
    hundred. The whole file is rewritten on every job update, every watcher tick
    and every hook, so six thousand old records cost ninety-five milliseconds a
    write and two and a half megabytes of disk, for history nobody reads.

    Only finished ones, and only the oldest: a job that is still running is
    never forgotten, whatever else is going on."""
    jobs = st.get("jobs")
    if not isinstance(jobs, dict) or len(jobs) <= FINISHED_CAP:
        return
    done = [(k, v) for k, v in jobs.items()
            if isinstance(v, dict) and v.get("state") not in ACTIVE_STATES]
    if len(done) <= FINISHED_CAP:
        return
    done.sort(key=lambda kv: kv[1].get("ended") or kv[1].get("updated") or 0, reverse=True)
    for key, job in done[FINISHED_CAP:]:
        del jobs[key]
        _discard_auto_files(job)


def _prune_sessions(st):
    """Keep the session map small and its cleanup unstoppable.

    It is only ever asked "have I seen this session before", so old entries are
    dead weight - and the whole state file is rewritten on every job update,
    every watcher tick and every hook, so a map that grows without limit makes
    every one of those writes bigger. Age alone is not enough: somebody running
    many agents can open thousands well inside the time limit."""
    seen = st.get("sessions")
    if not isinstance(seen, dict) or not seen:
        return
    now = time.time()
    fresh = {k: v for k, v in seen.items()
             if isinstance(v, (int, float)) and now - v <= SESSION_TTL}
    if len(fresh) > SESSION_CAP:
        newest = sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)[:SESSION_CAP]
        fresh = dict(newest)
    if len(fresh) != len(seen):
        st["sessions"] = fresh


def _prune(st):
    _sweep_temp_files()
    _prune_sessions(st)
    _prune_finished(st)
    cfg = load_config()
    cutoff = time.time() - cfg["prune_after_hours"] * 3600
    for jid in list(st["jobs"]):
        j = st["jobs"][jid]
        # ACTIVE_STATES, not just "running": a queued job has no `ended`, so
        # testing on that alone would delete every scheduler job the moment it
        # was created - and take its watcher with it
        if j.get("state") not in ACTIVE_STATES and (j.get("ended") or 0) < cutoff:
            del st["jobs"][jid]
            _discard_auto_files(j)


def _discard_auto_files(job):
    """The wrapper's own log and exit file go with the record.

    A wrapped command normally removes them itself on the way out. It leaves
    them when it cannot close its record - the state file busy, or an
    interrupt - so the watcher can still read the true exit status. Once the
    record is gone nothing can reach them, so they go too. Only files the
    wrapper made: a `run` job's log is the user's, named by them, and stays."""
    if not job.get("auto_launched"):
        return
    for f in (job.get("log"), job.get("exit_file")):
        if f and os.path.dirname(os.path.abspath(f)) == os.path.abspath(LOGS):
            try:
                os.remove(f)
            except OSError:
                pass


def current_session():
    """The session this process belongs to.

    Claude Code exports CLAUDE_CODE_SESSION_ID; the shorter name was a guess,
    and reading it meant every job recorded a session of None, which quietly
    disabled putting the current session's jobs first on the statusline. Both
    are read now - the fallback used to be a call to this function, which
    recursed until it raised whenever neither variable was set, i.e. every time
    the tool was run from an ordinary shell rather than from inside a session.

    None is a fine answer. A job started outside any session belongs to none."""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or None)


def current_bridge():
    """The conversation this session belongs to.

    Recorded on every job for diagnosis - `ls --json` reports it - but never
    used to decide ownership: several agents can share one conversation, and
    matching on it made every agent claim every job."""
    return os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")


def know_who_we_are(session_id=None):
    """Can this process tell which session it is drawing for?

    Filtering by session is only meaningful if the answer is yes. Neither the
    statusline payload nor the environment is guaranteed to carry the id - which
    session a statusline is drawn for is not something this can insist on - and
    filtering on an unknown identity matches nothing, so every bar disappears
    while the jobs are still running. A bar shown to somebody who did not start
    the job is a small untidiness. A running job with no bar is the failure the
    whole plugin exists to prevent."""
    return bool(session_id or current_session())


def job_belongs_here(job, session_id=None):
    """Is this job one that the session being drawn for should see?

    Several agents share one state file, so without this every agent's
    statusline shows every other agent's work and a crash is reported to
    whichever session happens to ask first. A job with no owner - started from
    an ordinary shell, or by cron - belongs to nobody and so is shown to
    everyone, since otherwise it would be shown to no one."""
    owner = job.get("session_id")
    if not owner:
        return True
    if session_id and owner == session_id:
        return True
    return owner == current_session()


def session_is_new(session_id, st=None):
    """True when this session started before the plugin's hooks were loaded.

    SessionStart records every session that begins with the plugin already
    active. A session that never got recorded was already running when the
    plugin arrived - which matters, because the statusline is read from
    settings.json when a session starts, so no bar will appear in it."""
    if not session_id:
        return False
    st = st if st is not None else state_ro()
    return session_id not in (st.get("sessions") or {})


def statusline_wired():
    try:
        with open(os.path.join(HOME, ".claude", "settings.json")) as f:
            return "agent_progress" in json.dumps(json.load(f).get("statusLine", {}))
    except Exception:
        return False


def slug(text):
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "job").strip()).strip("-.")
    return (s or "job")[:32]


def new_id(st, name):
    base = slug(name)
    if base not in st["jobs"]:
        return base
    # Reuse the slot if the previous job of this name is finished - one of our
    # own, and one nobody is still watching. Another session's finished record
    # is its history and, for a while, its bar; taking the id replaced both
    # with a job that session never started. And a watcher still running for
    # the old job - marked done by hand while its process ran on - would go on
    # writing its readings into the new job's record.
    prev = st["jobs"][base]
    if (prev.get("state") not in ACTIVE_STATES and job_belongs_here(prev)
            and not watcher_alive(prev)):
        return base
    if prev.get("state") not in ACTIVE_STATES and job_belongs_here(prev):
        # ours, finished, but its watcher has not noticed yet: the new run
        # takes a suffixed id, and the old bar - a failure, usually, since
        # this is what re-running after one looks like - retires now rather
        # than sitting beside the new one for half an hour
        prev["superseded"] = True
    n = 2
    while "%s-%d" % (base, n) in st["jobs"]:
        n += 1
    return "%s-%d" % (base, n)


def resolve(st, ref, mutating=False, any_session=False):
    """Find a job by exact id, then unique prefix, then substring.

    This session's jobs come first. Ids are global but names are not chosen -
    they come from the command, so two agents each training something both have
    a job called `train`, and without this `cancel train` in one of them kills
    the other's run. That is not a bookkeeping mistake: cancel signals the
    process group.

    Anything that changes a job refuses to reach into another session unless
    asked to in as many words. Reading one - `show`, `log` - is allowed, since
    looking is harmless and sometimes the point."""
    jobs = st["jobs"]
    here = current_session()

    def own(keys):
        mine = [k for k in keys if job_belongs_here(jobs[k], here)]
        return mine or keys

    def guard(jid):
        # outside a session there is nothing to reach out of: a plain shell owns
        # the lot, and refusing there would just make the CLI unusable
        if (mutating and here and not any_session
                and not job_belongs_here(jobs[jid], here)):
            # Ids are unique but names are not chosen, so when agent A has
            # `train`, agent B's is `train-2` - and B saying `cancel train`
            # almost always means its own. The refusal used to answer only
            # "pass --any-session", which pointed B straight at killing A's
            # run; it now names B's own job first.
            own = sorted(k for k in jobs if k != jid and k.startswith(jid)
                         and job_belongs_here(jobs[k], here)
                         and jobs[k].get("state") in ACTIVE_STATES)
            hint = ("\nYour own job by that name is %s - did you mean that one?"
                    % ", ".join(own)) if own else ""
            raise SystemExit(
                "%s belongs to another session (%s), so this would reach outside "
                "this one.%s\nPass --any-session only if that session's job is "
                "really the one you want."
                % (jid, (jobs[jid].get("session_id") or "unknown")[:12], hint))
        return jid

    if ref in jobs:
        return guard(ref)
    for match in (
        [k for k in jobs if k.startswith(ref)],
        [k for k in jobs if ref.lower() in k.lower()],
    ):
        match = own(match)
        running = [k for k in match if jobs[k].get("state") in ACTIVE_STATES]
        pool = running or match
        if len(pool) == 1:
            return guard(pool[0])
        if len(pool) > 1:
            raise SystemExit("ambiguous job ref %r: matches %s" % (ref, ", ".join(sorted(pool))))
    raise SystemExit("no such job: %r (try: agent-progress ls)" % ref)


# A pid means something only on the machine it belongs to. A home directory
# shared between the login nodes of a cluster shares this state file too, and
# a session on one node asking "is pid 4242 alive" about a job on another gets
# an answer about some unrelated process - or about nothing, which read as the
# job having died. Every record says where it runs; a pid elsewhere is presumed
# alive, since nothing here can tell.
HOST = socket.gethostname()


def pid_here(job):
    """Can this machine answer questions about the job's pid?"""
    host = job.get("host")
    return not host or host == HOST


def watcher_here(job):
    host = job.get("watcher_host") or job.get("host")
    return not host or host == HOST


def job_pid_alive(job):
    """The job's process, as far as this machine can tell; presumed alive
    when it lives on another one."""
    if not pid_here(job):
        return True
    return alive(job.get("pid"))


def watcher_alive(job):
    if not watcher_here(job):
        return True
    return alive(job.get("watcher_pid"))


def alive(pid):
    """Is there a process with this pid? Never true for 0 or a negative number:
    kill(0) and kill(-1) address process groups, and answering "alive" for
    them let `start --pid -1` in, whose cancel would have signalled every
    process the user owns."""
    if not pid:
        return False
    try:
        if int(pid) <= 1:
            return False
        os.kill(int(pid), 0)
    except OSError as e:
        return e.errno == errno.EPERM
    except (TypeError, ValueError):
        return False
    return True


# ----------------------------------------------------------------- log parsing

# Ordered most-specific first. Each yields some of: step, total, pct.
BUILTIN_PATTERNS = [
    # PyTorch Lightning: "Epoch 3: 45%|███ | 450/1000 [..]"  -> outer epoch + inner bar
    ("lightning", re.compile(
        r"(?i)\bepoch\s+(?P<step>\d+)\s*:\s*(?P<sub>\d{1,3})%\|")),
    # "Epoch 12/50", "epoch: 12 / 50", "Epoch [12/50]"
    ("epoch", re.compile(
        r"(?i)\bepochs?\b[:\s\[]*(?P<step>\d+)\s*/\s*(?P<total>\d+)")),
    # "step 1200/10000", "iteration 5/20", "batch 3/40"
    ("step", re.compile(
        r"(?i)\b(?:global[_ ]?step|steps?|iters?|iterations?|batch(?:es)?)\b[:\s\[]*"
        r"(?P<step>\d+)\s*/\s*(?P<total>\d+)")),
    # bare tqdm: " 45%|█████     | 45/100 [00:12<00:14,  3.9it/s]"
    ("tqdm", re.compile(
        r"(?P<pct>\d{1,3})%\|[^|\n]*\|\s*(?P<step>\d+)/(?P<total>\d+)")),
    # keras: "  32/1875 [>.............]"
    ("keras", re.compile(r"^\s*(?P<step>\d+)/(?P<total>\d+)\s*\[")),
    # "Progress: 45/100", "trial 3 of 20"
    ("progress", re.compile(
        r"(?i)\b(?:progress|trial|fold|shard|chunk|file)\b[:\s]*"
        r"(?P<step>\d+)\s*(?:/|of)\s*(?P<total>\d+)")),
    # "45% complete", "done: 45.0%"
    # a left boundary, or "GPU util 1234%" reads as 234% and the bar hits 100
    ("percent", re.compile(r"(?<![\d.])(?P<pct>\d{1,3}(?:\.\d+)?)\s*%")),
    # last resort: a bare "45/100" not part of a path, date or version
    ("bare", re.compile(r"(?<![\w./-])(?P<step>\d+)\s*/\s*(?P<total>\d+)(?![\w./-])")),
]


def parse_progress(text, pattern=None, known_total=None):
    """Scan a chunk of log text and return the most recent progress reading.

    Returns {"step", "total", "pct", "sub", "src"} with missing keys as None,
    or None if nothing matched. Scans patterns in priority order and, within a
    pattern, takes the *last* match in the chunk (the freshest line).
    """
    pats = BUILTIN_PATTERNS
    if pattern:
        try:
            pats = [("custom", re.compile(pattern))]
        except re.error:
            pass          # unusable pattern: fall back rather than raise

    for name, rx in pats:
        last = None
        for m in rx.finditer(text):
            last = m
        if last is None:
            continue
        g = last.groupdict()
        step = _int(g.get("step"))
        total = _int(g.get("total"))
        pct = _float(g.get("pct"))
        sub = _float(g.get("sub"))

        if name == "bare":
            # only trust a bare a/b if it looks like a real counter
            if not total or total <= 1 or step is None or step > total:
                continue
        if total is not None and step is not None and step > total:
            # e.g. matched something unrelated; ignore the total
            total = None
        if name == "lightning":
            total = known_total  # epoch count comes from the caller
            if sub is not None:
                sub = sub / 100.0
        if pct is not None and name in ("percent", "tqdm"):
            pct = max(0.0, min(100.0, pct)) / 100.0

        return {
            "step": step,
            "total": total if total is not None else known_total,
            "pct": pct if name in ("percent", "tqdm") else None,
            "sub": sub,
            "src": name,
        }
    return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _number(value, key, kind):
    """A finite int or float, or a complaint naming the setting and what it wants."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError("expected %s, got %r" % ("a whole number" if kind == "int"
                                                  else "a number", value))
    if not math.isfinite(out):
        raise ValueError("%s must be a real number, not %s" % (key, out))
    return int(out) if kind == "int" else out


def _float(v):
    """A finite number, or None.

    float() accepts "nan" and "inf", and training logs are full of the word -
    `loss nan` is what a diverged run prints. Reading one as progress put a rate
    of nan into the bar, which then displayed the word to the user and claimed
    the job had no time remaining. Neither is a number a bar can be drawn from,
    so neither is a number."""
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def read_tail(path, offset, max_bytes=262144):
    """Read new bytes from `offset`. Returns (text, new_offset). Handles the log
    being truncated or rotated out from under us."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", offset
    if size < offset:      # truncated / rotated
        offset = 0
    start = max(offset, size - max_bytes)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read()
    except OSError:
        return "", offset
    text = raw.decode("utf-8", "replace")
    # tqdm redraws with \r; treat those as line breaks so we see each redraw
    return text.replace("\r", "\n"), size


# ------------------------------------------------------------------- estimating

def _measured_rate(job, now, cfg):
    """Units per second from recent samples, or None."""
    # A sample that is not two finite numbers cannot be measured from, and
    # not-a-number compares False against everything, so it would slip past the
    # tests below and put a rate of nan into the bar. Records written by an
    # older version, or by hand, can still hold one.
    samples = [x for x in (job.get("samples") or [])
               if isinstance(x, (list, tuple)) and len(x) == 2
               and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) for v in x)]
    if len(samples) < 2:
        return None, 0
    win = samples[-int(cfg["rate_window"]):]
    t0, s0 = win[0]
    t1, s1 = win[-1]
    if (t1 - t0) < cfg["rate_min_span"] or s1 <= s0:
        return None, len(win) - 1
    return (s1 - s0) / (t1 - t0), len(win) - 1


def estimate(job, now=None, cfg=None):
    """Fuse Claude's prior ETA with the rate measured from the log.

    Returns a dict the renderers consume. `source` explains which signal won:
      measured - purely from observed throughput
      claude   - purely from Claude's up-front guess
      blend    - weighted mix, weight shifting to `measured` as samples arrive
    """
    now = now or time.time()
    cfg = cfg or load_config()
    started = job.get("started") or now
    ended = job.get("ended")
    elapsed = (ended or now) - started

    total = job.get("total")
    units = job.get("units")            # step + sub-step fraction, a float
    if units is None:
        units = job.get("step")

    # --- fraction complete -------------------------------------------------
    frac = None
    frac_from_data = False
    if job.get("pct") is not None:
        frac = job["pct"]
        frac_from_data = True
    elif total and units is not None and total > 0:
        frac = units / float(total)
        frac_from_data = True
    eta_end = job.get("eta_end")
    if frac is None and eta_end and eta_end > started:
        # no countable progress: fall back to wall-clock against Claude's guess,
        # frozen at the end for a job that has already stopped
        frac = ((ended or now) - started) / (eta_end - started)
        frac = min(frac, 0.99)          # never claim done on a guess alone
    if frac is not None:
        frac = max(0.0, min(1.0, frac))

    # --- remaining time ----------------------------------------------------
    rate, nobs = _measured_rate(job, now, cfg)
    measured_rem = None
    if rate and total and units is not None:
        measured_rem = max(0.0, (total - units) / rate)
    if measured_rem is None and frac_from_data and frac and 0 < frac < 1 and nobs >= 2:
        # no countable total (percent-only logs): extrapolate from the fraction.
        # only valid when frac came from the log, never from the prior itself.
        measured_rem = elapsed * (1 - frac) / frac

    prior_rem = None
    overdue = False
    if eta_end:
        prior_rem = eta_end - now
        if prior_rem < 0:
            overdue = True
            prior_rem = None            # a blown guess is worse than no guess

    if measured_rem is not None and prior_rem is not None:
        w = min(1.0, nobs / float(cfg["blend_full_at"]))
        remaining = w * measured_rem + (1 - w) * prior_rem
        source = "measured" if w >= 0.999 else "blend"
    elif measured_rem is not None:
        remaining, source = measured_rem, "measured"
    elif prior_rem is not None:
        remaining, source = prior_rem, "claude"
    else:
        remaining, source = None, None

    if job.get("state") == "queued":
        # elapsed is time spent waiting, and there is no remaining to speak of:
        # the run has not begun, so nothing about its length has been observed
        elapsed = now - (job.get("submitted") or started)
        remaining, source, overdue = None, None, False
        if not job.get("total"):
            frac = None
    elif job.get("state") != "running":
        remaining, source, overdue = 0.0, None, False
        if job.get("state") == "done":
            frac = 1.0

    return {
        "frac": frac,
        "determinate": frac is not None,
        "elapsed": elapsed,
        "remaining": remaining,
        "rate": rate,
        "nobs": nobs,
        "source": source,
        "overdue": overdue,
        "eta_wall": (now + remaining) if remaining is not None else None,
        # the whole point of re-estimating: total duration as currently believed
        "total_est": (elapsed + remaining) if remaining is not None else None,
    }


# -------------------------------------------------------------------- formatting

def fmt_dur(s):
    """tqdm's clock format: MM:SS, or H:MM:SS past an hour."""
    if s is None:
        return "--:--"
    s = int(max(0, s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, sec)
    return "%02d:%02d" % (m, sec)


def fmt_short(s):
    """Compact human duration: 2h14m / 45m / 38s."""
    if s is None or not math.isfinite(s):
        return "?"
    s = int(max(0, s))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    h, m = divmod(s // 60, 60)
    return "%dh%02dm" % (h, m) if m else "%dh" % h


def fmt_rate(rate, unit):
    if not rate:
        return ""
    unit = unit or "it"
    if rate >= 1:
        return "%.2f%s/s" % (rate, unit)
    return "%.1fs/%s" % (1.0 / rate, unit)


def fmt_clock(ts, cfg=None):
    if ts is None:
        return "?"
    fmt = (cfg or load_config())["clock_format"]
    try:
        return time.strftime(fmt, time.localtime(ts))
    except (OverflowError, ValueError, OSError):
        return "?"                        # a timestamp beyond what the platform can hold


MAX_DURATION = 100 * 365 * 86400      # a century; anything longer is a typo

_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def _unit_seconds(word):
    """'h', 'hr', 'hours', 'min', 'd', 'days', 'ms', ... -> seconds, or None."""
    if not word:
        return 1.0                       # a bare number is seconds
    spellings = {
        "ms": ("ms", "msec", "msecs", "millisecond", "milliseconds", "millis"),
        "s": ("s", "sec", "secs", "second", "seconds"),
        "m": ("m", "min", "mins", "minute", "minutes"),
        "h": ("h", "hr", "hrs", "hour", "hours"),
        "d": ("d", "day", "days"),
        "w": ("w", "wk", "wks", "week", "weeks"),
    }
    for unit, words in spellings.items():
        if word in words:
            return float(_UNIT_SECONDS[unit])
    return None                          # "10 msec" is not ten minutes; "1 month" is not a minute


def parse_duration(text):
    """'90', '90s', '5m', '2h30m', '2d', '1:30:00', '2 hours' -> seconds.

    The whole string has to be a duration. Matching only the parts that looked
    like one read `2d` as 2 seconds, `-5m` as 5 minutes and `1e3` as 4 seconds,
    all silently: a two-day estimate became a two-second one, and the job was
    hidden as short and overdue before it had begun."""
    if text is None:
        return None
    text = str(text).strip().lower()
    if not text:
        return None
    total = None
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            if len(parts) > 3 or any(p < 0 for p in parts):
                raise ValueError(text)
            while len(parts) < 3:
                parts.insert(0, 0.0)
            total = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            total, pos = 0.0, 0
            for m in re.finditer(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", text):
                if m.start() != pos:
                    raise ValueError(text)
                pos = m.end()
                mult = _unit_seconds(m.group(2))
                if mult is None:
                    raise ValueError(text)
                total += float(m.group(1)) * mult
            if pos != len(text) or pos == 0:
                raise ValueError(text)
    except ValueError:
        raise SystemExit("could not parse duration: %r (try 45m, 2h30m, 90s, 2d, 1:30:00)"
                         % text)
    if not math.isfinite(total) or total > MAX_DURATION:
        raise SystemExit("%r is not a sensible length of time" % text)
    return total


# ---------------------------------------------------------------------- drawing

BLOCKS = " \u258f\u258e\u258d\u258c\u258b\u258a\u2589\u2588"   # 1/8ths through full

STYLES = {
    #          left        right       fill        partials  track
    "blocks": ("\u2595",  "\u258f",  "\u2588",  BLOCKS,   "\u00b7"),
    "tqdm":   ("|",        "|",        "\u2588",  BLOCKS,   " "),
    "ascii":  ("[",        "]",        "#",        None,     "-"),
    "dots":   ("",         "",         "\u25cf",  None,     "\u25cb"),
    "bars":   ("",         "",         "\u2501",  None,     "\u2500"),
}

# Both are replaced from the user's config by apply_theme() before rendering.
PALETTE = {"run": 44, "done": 42, "fail": 203, "warn": 179,
           "dim": 244, "track": 238, "text": 252}
SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"


def apply_theme(cfg):
    """Fold the configured colors and spinner into the render globals."""
    global PALETTE, SPINNER
    PALETTE = {"run": cfg["color_running"], "done": cfg["color_done"],
               "fail": cfg["color_failed"], "warn": cfg["color_warn"],
               "dim": cfg["color_dim"], "track": cfg["color_track"],
               "text": cfg["color_text"]}
    if cfg["spinner"]:
        SPINNER = cfg["spinner"]
    return cfg


def paint(text, key, on=True):
    if not on or not text:
        return text
    return "\033[38;5;%dm%s\033[0m" % (PALETTE.get(key, 252), text)


def bar_chars(cfg):
    """Style preset, with any per-character override applied on top."""
    left, right, fill, partials, track = STYLES.get(cfg["style"], STYLES["blocks"])
    # every override is one cell wide: a longer string would silently stretch
    # the bar past bar_width and push the rest of the line off the screen
    if cfg["fill_char"]:
        fill, partials = cfg["fill_char"][:1], None   # a custom fill has no 1/8ths
    return (cfg["left_cap"][:1] or left, cfg["right_cap"][:1] or right,
            fill, partials, cfg["track_char"][:1] or track)


def draw_bar(frac, cfg, tone="run"):
    left, right, fill, partials, track = bar_chars(cfg)
    color = cfg["color"]
    width = max(4, int(cfg["bar_width"]))

    if frac is None:                       # indeterminate: a block sliding back and forth
        span = max(3, width // 5)
        period = max(1, (width - span) * 2)
        pos = int(time.time() * 4) % period
        if pos > (width - span):
            pos = period - pos
        cells = [track] * width
        for i in range(pos, min(width, pos + span)):
            cells[i] = fill
        return (paint(left, "dim", color) + paint("".join(cells), tone, color)
                + paint(right, "dim", color))

    frac = max(0.0, min(1.0, frac))
    exact = frac * width
    full = int(exact)
    body = fill * full
    if partials and full < width:
        eighth = int((exact - full) * 8)
        if eighth:
            body += partials[eighth]
    pad = track * (width - len(body))
    return (paint(left, "dim", color) + paint(body, tone, color)
            + paint(pad, "track", color) + paint(right, "dim", color))


def tone_for(job):
    st = job.get("state")
    if st == "done":
        return "done"
    if st in ("failed", "cancelled"):
        return "fail"
    if st in ("stalled", "queued"):
        return "warn"
    return "run"


def status_glyph(job, cfg):
    color = cfg["color"]
    st = job.get("state")
    if st == "done":
        return paint(cfg["glyph_done"], "done", color)
    if st == "failed":
        return paint(cfg["glyph_failed"], "fail", color)
    if st == "cancelled":
        return paint(cfg["glyph_cancelled"], "dim", color)
    if st == "stalled":
        return paint(cfg["glyph_stalled"], "warn", color)
    if st == "queued":
        # deliberately not the spinner: nothing is turning yet
        return paint(cfg["glyph_queued"], "warn", color)
    fps = cfg["spinner_fps"] or 0
    idx = int(time.time() * fps) % len(SPINNER) if fps else 0
    return paint(SPINNER[idx], "run", color)


# ------------------------------------------------------------- line composition

def render_line(job, cfg, width=None, now=None):
    """One statusline row for one job. Every field here is individually
    switchable from config; see `agent-progress config`."""
    now = now or time.time()
    apply_theme(cfg)
    color = cfg["color"]
    e = estimate(job, now, cfg)
    tone = tone_for(job)
    unit = job.get("unit") or ""

    queued = job.get("state") == "queued"

    parts = []
    if cfg["show_spinner"]:
        parts.append(status_glyph(job, cfg))
    if cfg["show_name"]:
        parts.append(paint(job.get("id", "job")[:cfg["name_width"]], "text", color))
    # An empty bar, not the indeterminate one that slides back and forth: a job
    # in a queue is not making unmeasured progress, it is making none.
    parts.append(draw_bar(0.0 if queued and e["frac"] is None else e["frac"], cfg, tone))

    if cfg["show_percent"] and e["frac"] is not None:
        parts.append(paint("%3d%%" % int(e["frac"] * 100), "text", color))

    total, units = job.get("total"), job.get("units")
    if cfg["show_counts"] and total and units is not None:
        parts.append(paint("%g/%g%s" % (round(units, 1), total, unit), "dim", color))

    if queued:
        # There is no elapsed<remaining to show: nothing has started, so the
        # only honest numbers are how long it has been waiting and why.
        waited = now - (job.get("submitted") or job.get("started") or now)
        parts.append(paint("queued " + fmt_short(waited), "warn", color))
        why = describe_queue(job)
        if why:
            parts.append(paint("· " + why, "dim", color))
    elif job.get("state") == "running":
        if cfg["show_clock"]:
            # tqdm's signature elapsed<remaining pair
            rem = fmt_dur(e["remaining"]) if e["remaining"] is not None else "?"
            mark = "~" if e["source"] in ("claude", "blend") else ""
            parts.append(paint("%s<%s%s" % (fmt_dur(e["elapsed"]), mark, rem),
                               "warn" if e["source"] == "claude" else "text", color))
        if cfg["show_rate"] and e["rate"] and job.get("total"):
            parts.append(paint(fmt_rate(e["rate"], unit or "it"), "dim", color))
        if cfg["show_eta_clock"] and e["eta_wall"]:
            parts.append(paint("\u2192" + fmt_clock(e["eta_wall"], cfg), "dim", color))
        init, tot = job.get("initial_est_total_s"), e.get("total_est")
        if (cfg["show_drift"] and init and tot
                and abs(tot - init) / float(init) > cfg["drift_threshold"]):
            # the job is taking materially longer (or less) than first thought
            d = tot - init
            parts.append(paint("est %s (%s%s)" % (
                fmt_short(tot), "+" if d > 0 else "-", fmt_short(abs(d))), "warn", color))
        if e["overdue"]:
            parts.append(paint("(past estimate)", "warn", color))
    else:
        tail = "in " + fmt_dur(e["elapsed"])
        parts.append(paint(tail, "dim", color))
        if job.get("state") == "failed":
            parts.append(paint(job.get("scheduler_state")
                               or crash_reason(job.get("exit_code"))[0], "fail", color))

    # a queued job has already said the one thing worth the space: why it waits
    if cfg["show_note"] and job.get("note") and not queued:
        parts.append(paint("\u00b7 " + one_line(job["note"], cfg["note_width"]),
                           "dim", color))

    line = " ".join(p for p in parts if p)
    return clip(line, width) if width else line


def char_width(ch):
    """Terminal columns one character occupies. Emoji and CJK take two."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


_ESCAPES = re.compile(r"\033\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


def one_line(text, limit=None):
    """Free text, made safe to put on a rendered line.

    A note or description is whatever someone passed on the command line, and
    that has reached here with newlines in it - a $(...) substitution, a pasted
    error. One of those turns a single statusline row into three, which breaks
    the row budget the statusline is supposed to keep. Escape sequences would
    likewise leak colour into everything after them."""
    if not text:
        return text
    text = _ESCAPES.sub("", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    # a note is a line; 200 KB of one was being written into the state file
    # on every tick
    return text[:limit or 1000]


def visible_len(s):
    return sum(char_width(c) for c in re.sub(r"\033\[[0-9;]*m", "", s))


def clip(s, width):
    """Truncate to `width` visible columns, keeping ANSI codes balanced."""
    if visible_len(s) <= width:
        return s
    out, seen = [], 0
    i = 0
    while i < len(s):
        if s[i] == "\033":
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i:j + 1])
            i = j + 1
            continue
        w = char_width(s[i])
        if seen + w > width - 1:
            break
        out.append(s[i])
        seen += w
        i += 1
    # the reset only belongs where colour was used; with colour off it was the
    # one escape sequence on the line
    return "".join(out) + "…" + ("\033[0m" if "\033" in s else "")


def render_block(job, cfg, width):
    """Two-line detailed view used by the `watch` dashboard."""
    now = time.time()
    e = estimate(job, now)
    color = cfg["color"]
    head = render_line(job, cfg, width=width, now=now)
    bits = []
    if job.get("desc"):
        bits.append(one_line(job["desc"], 120))
    if job.get("cmd"):
        bits.append("$ " + command_for_display(job["cmd"])[:160])
    bits.append(one_line("watching " + describe_monitor(job), 160))
    for label, key in (("reason", "queue_reason"), ("nodes", "nodes"),
                       ("partition", "partition"), ("slurm says", "scheduler_state")):
        if job.get(key):
            bits.append("%s %s" % (label, job[key]))
    if job.get("queued_seconds"):
        bits.append("queued for " + fmt_short(job["queued_seconds"]))
    if e.get("total_est"):
        init = job.get("initial_est_total_s")
        bits.append("est total %s%s" % (
            fmt_short(e["total_est"]),
            " (first guess %s)" % fmt_short(init) if init and abs(
                e["total_est"] - init) / float(init) > 0.2 else ""))
    if job.get("interval_s"):
        bits.append("updates every " + fmt_short(job["interval_s"]))
    if e["source"]:
        bits.append("eta: " + {"measured": "measured from log",
                               "claude": "Claude's estimate",
                               "blend": "blended (%d obs)" % e["nobs"]}[e["source"]])
    if job.get("log"):
        bits.append(job["log"])
    sub = paint("   " + "  ·  ".join(bits), "dim", color)
    return head + "\n" + clip(sub, width)


def job_visible(job, cfg, now=None):
    """Short jobs are tracked but never clutter the statusline.

    A job qualifies as short only while we still believe it is short: once it
    has actually been running past the threshold it appears regardless, so a
    job that was estimated at 90s and is still going at 5 minutes shows up."""
    now = now or time.time()
    if job.get("force_show"):
        return True
    if job.get("state") in ("failed", "stalled"):
        return True          # a crash is always worth showing, however short the job
    if job.get("state") == "queued":
        # the whole reason nothing is happening; hiding it is the one thing
        # guaranteed to be unhelpful
        return True
    threshold = cfg["min_duration_seconds"]
    if threshold <= 0:
        return True
    e = estimate(job, now, cfg)
    if e["elapsed"] >= threshold:
        return True
    # The largest estimate the job has ever carried, not just the current one.
    # Estimates move as the job is measured, and one that starts at half an hour
    # and is revised down to ninety seconds used to take its own bar away while
    # the job carried on running - reappearing later, once elapsed crossed the
    # threshold by itself. The threshold decides whether a bar appears; it
    # should not take one back.
    candidates = [x for x in (e.get("total_est"), job.get("est_total_s"),
                              job.get("initial_est_total_s"), job.get("eta_prior_s"))
                  if isinstance(x, (int, float))]
    return bool(candidates) and max(candidates) >= threshold


def pick_jobs(st, cfg, session_id=None, apply_visibility=True, scoped=True):
    """Jobs worth showing: everything running, plus recently finished ones."""
    now = time.time()
    out = []
    for j in st["jobs"].values():
        if j.get("state") in ACTIVE_STATES:
            pass
        elif j.get("superseded"):
            continue                    # re-run under a new id; that one is the bar
        else:
            failed = j.get("state") in ("failed", "stalled")
            linger = cfg["keep_failed_seconds"] if failed else cfg["keep_done_seconds"]
            if now - (j.get("ended") or 0) >= linger:
                continue
            # A finished bar has one job left: to be seen. Time alone did that
            # badly - five minutes is many messages if you are working, and none
            # at all if you stepped away - so a completed bar also retires after
            # a couple of your messages, whichever comes first. A crash keeps
            # its full time, since it is asking for something.
            if not failed and cfg["keep_done_prompts"] \
                    and (j.get("prompts_since_done") or 0) >= cfg["keep_done_prompts"]:
                continue
        if apply_visibility and not job_visible(j, cfg, now):
            continue
        if (scoped and cfg["scope"] == "session" and know_who_we_are(session_id)
                and not job_belongs_here(j, session_id)):
            continue
        out.append(j)
    # current session first, then oldest-started first (stable ordering)
    out.sort(key=lambda j: (j.get("session_id") != session_id, j.get("started") or 0))
    return out


# --------------------------------------------------------------- auto-tracking
#
# Deciding, from a shell command alone, whether it is worth a progress bar.
#
# Two rules govern everything here. Missing a long job is a small loss - the
# user simply gets no bar, as before. Catching a short one is a real cost: an
# interrupted tool call for something that would have finished already. So the
# evidence has to be good, and anything ambiguous is left alone.

# Signals that cost nothing to trust, because they are the caller's own words:
# a command handed a long timeout, or explicitly sent to the background, has
# already been declared slow by whoever wrote it.

AUTO_TRACK_PATTERNS = [
    # training and other GPU work
    # No \b before the keyword: a word boundary cannot follow an underscore, so
    # \btrain misses run_training.py and my_train.py, which is most of them. A
    # stray match on something like constraints.py costs nothing now that a
    # command finishing inside the threshold is never tracked at all.
    (r"\b(?:python3?|uv\s+run|poetry\s+run|pipenv\s+run)\s+[^|;&]{0,120}"
     r"(?:train|finetune|fine_tune|pretrain|sweep|eval|benchmark|experiment)"
     r"[\w.-]*\.py\b", "a training or evaluation script"),
    # The same work, invoked the other ways people actually invoke it. A model
    # asked to "run training" picks whatever the repo uses, and that is a module,
    # a shell script or a flag at least as often as it is a .py file.
    (r"\b(?:python3?|uv\s+run|poetry\s+run|pipenv\s+run)\s+(?:python3?\s+)?-m\s+[\w.]{0,60}"
     r"(?:train|finetune|fine_tune|pretrain|sweep|eval|benchmark|experiment)",
     "a training module"),
    (r"\b(?:bash|sh|zsh)\s+[^|;&]{0,80}"
     r"(?:train|finetune|pretrain|sweep|eval|benchmark|experiment)[\w.-]{0,40}\.sh\b",
     "a training script"),
    (r"(?:^|\s)\./[\w./-]{0,60}"
     r"(?:train|finetune|pretrain|sweep|eval|benchmark|experiment)[\w.-]{0,40}",
     "a training script"),
    (r"--(?:mode|stage|task)[= ](?:train|fit|finetune|pretrain)\b", "a training run"),
    (r"\b(?:sbatch|qsub|bsub)\b", "a batch submission"),
    (r"\btorchrun\b", "torchrun"),
    (r"\baccelerate\s+launch\b", "accelerate launch"),
    (r"\bdeepspeed\b", "deepspeed"),
    (r"\bpython3?\s+-m\s+(?:torch\.distributed|accelerate|vllm)\b", "a distributed run"),
    (r"\bwandb\s+(?:agent|sweep)\b", "a wandb sweep"),
    (r"\b(?:optuna|ray)\s+\w+", "a hyperparameter sweep"),
    # builds and test suites. Being often quick used to be a reason to leave
    # these out; with deferral it is not, because a run that finishes inside
    # the threshold is never tracked at all.
    (r"\bcargo\s+(?:build|test|bench)\b", "a cargo build"),
    (r"\bmake\b(?!\s+(?:clean|help|list))", "make"),
    (r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:build|test)\b", "a JS build or test"),
    (r"\b(?:pytest|py\.test)\b", "a pytest run"),
    (r"\bgo\s+(?:test|build)\b", "a go build or test"),
    (r"\b(?:tox|nox)\b", "tox or nox"),
    (r"\bdocker(?:\s+compose)?\s+build\b", "a docker build"),
    (r"\bdocker\s+compose\s+up\b", "docker compose up"),
    (r"\bbazel\s+(?:build|test)\b", "bazel"),
    (r"\b(?:gradlew?|mvn)\b", "a JVM build"),
    (r"\b(?:cmake\s+--build|xcodebuild)\b", "a native build"),
    # data and infrastructure
    (r"\bdvc\s+repro\b", "a dvc pipeline"),
    (r"\bdbt\s+(?:run|build|test)\b", "a dbt run"),
    (r"\bspark-submit\b", "a spark job"),
    (r"\bterraform\s+(?:apply|plan|destroy)\b", "terraform"),
    (r"\bansible-playbook\b", "an ansible playbook"),
    (r"\bpulumi\s+(?:up|preview)\b", "pulumi"),
    (r"\balembic\s+upgrade\b", "a database migration"),
    (r"\b(?:pg_restore|pg_dump|mysqldump)\b", "a database dump or restore"),
    # moving data
    (r"\brsync\b", "an rsync transfer"),
    (r"\baws\s+s3\s+(?:sync|cp)\b", "an S3 transfer"),
    (r"\b(?:gsutil|gcloud\s+storage)\b", "a GCS transfer"),
    (r"\bhuggingface-cli\s+download\b", "a model download"),
    (r"\b(?:wget|curl)\b[^|;&]{0,120}\s-[a-zA-Z]*[oO]\b", "a download"),
    (r"\bgit\s+clone\b", "a git clone"),
    (r"\bsleep\s+(?:[2-9]\d\d|\d{4,})\b", "a long sleep"),
]

# Checked before anything else. Anything matching here is never auto-tracked.
AUTO_TRACK_IGNORE = [
    # never re-wrap ourselves. The path prefix matters: the wrapper emits an
    # absolute path, so a rule anchored on the bare name would not match the
    # very command it exists to recognise.
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:\S*/)?agent[-_]progress\b",
    r"\bagent_progress\.py\b",
    # the documented way to say "not this one": the hook cannot see a variable
    # set for the command, but it can see it written in front of the command
    r"(?:^|[;&|]\s*)AGENT_PROGRESS_NO_AUTO=",
    r"(?:^|\s)(?:--help|-h|--version|-V)(?:\s|$)",
    # note: \b before a dash never matches - a space and a dash are both
    # non-word characters, so there is no boundary between them
    r"(?:^|\s)--(?:dry-run|collect-only|list-tests|check|noop|version)(?:\s|=|$)",
    r"^\s*(?:ls|cat|head|tail|pwd|echo|printf|which|type|env|date|whoami|wc|"
    r"grep|rg|fd|find|stat|file|du|df|ps|kill|touch|mkdir|rm|cp|mv|chmod|export|"
    r"source|test|true|false|sed|awk|jq|diff|open|code)\b",
    r"^\s*git\s+(?:status|log|diff|show|branch|rev-parse|add|commit|config|remote)\b",
    r"^\s*(?:npm|pnpm|yarn|pip|pip3|brew|apt|apt-get)\s+(?:ls|list|info|view|show)\b",
    r"^\s*docker\s+(?:ps|images|logs)\b",
]

# A name for the bar, taken from whatever in the command looks most like the
# thing being run.
_NAME_HINTS = [
    r"([\w.-]{1,80})\.py\b",
    r"([\w.-]{1,80})\.sh\b",
    r"\b(?:npm|pnpm|yarn|cargo|go|docker|terraform|dbt|dvc|bazel)\s+(?:run\s+)?(\w{1,40})",
    r"^\s*(?:sudo\s+)?(?:\S*/)?([\w.-]{1,80})",     # the basename of `./scripts/run.sh`
]


def _split_patterns(text):
    return [p for p in re.split(r"[;\n]", text or "") if p.strip()]


def suggest_job_name(command):
    command = (command or "")[:2000]      # the head is all a name can come from
    for rx in _NAME_HINTS:
        m = re.search(rx, command)
        if m:
            name = slug(os.path.basename(m.group(1)))
            name = re.sub(r"\.(py|sh|js|ts)$", "", name)
            if name and name not in ("python", "python3", "uv", "poetry", "sudo", "env"):
                return name[:18]
    return "job"


def _safe_search(rx, text):
    """re.search that treats a bad pattern as no match rather than an error."""
    try:
        return bool(re.search(rx, text))
    except re.error:
        return False


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w{1,40})\1[\s\S]{0,8000}?^\2[ \t]*$", re.M)
SEPARATORS = re.compile(r"[\n;]|&&|\|\||\|")


def command_segments(command, cap=400, most=40):
    """The separate commands in one shell line, minus any heredoc bodies.

    Claude writes a setup and the real work in a single call all the time -
    `mkdir -p out && python train.py`, or a heredoc that writes a script
    followed by the line that runs it. Judging such a command by its first word
    called it trivial and left a training run untracked. Heredoc bodies come out
    because they are data: the text of a script is not a command being run.

    Bounded on both counts so a pathological command cannot make this
    expensive, and sampled from both ends rather than the front: a command that
    writes five thousand lines of data and then runs the job keeps the job,
    which taking only the first forty segments would have thrown away."""
    text = HEREDOC.sub(" ", command or "")
    text = re.sub(r"\\\n", " ", text)         # a continuation line is the same line
    parts = [seg.strip()[:cap] for seg in SEPARATORS.split(text) if seg.strip()]
    if len(parts) <= most:
        return parts
    half = most // 2
    return parts[:half] + parts[-half:]


_SHELL_STATE = re.compile(
    r"\s*(?:cd|pushd|popd|export|source|\.|alias|unalias|unset|nohup|disown)(?:\s|$)"
    r"|\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s*$")
# `source` and `.` are deliberately not here: what a sourced file defines -
# functions, unexported variables - has to be in the shell that runs the work,
# and the work runs in the wrapper's own shell. Such a command is left whole.
# A backslash is excluded from the argument too: `cd /tmp \` + newline + `&& x`
# is one line to the shell, and cutting it at the newline handed the launcher
# to `cd` as arguments.
_LEADING_STATE = re.compile(
    r"\A(\s*(?:cd|pushd|export)(?:\s[^;&|\n\'\"`$()\\]*)?(?:&&|;|\n)\s*)")


def split_shell_prefix(command):
    """(prefix, body): the leading `cd`s and `export`s that have to stay in the
    caller's shell, and the command left to wrap. `body` is None when a command
    that acts on the shell sits anywhere else, or backgrounds itself: those
    cannot be wrapped at all.

    A prefix segment with a quote, a variable or a substitution in it is not
    taken - splitting on separators cannot see inside those, and a `cd` cut in
    half would break the command instead of tracking it."""
    text = command or ""
    prefix = ""
    while True:
        m = _LEADING_STATE.match(text)
        if not m:
            break
        prefix += m.group(1)
        text = text[m.end():]
    body = text
    if not body.strip():
        return prefix, None
    for seg in command_segments(body) or [body]:
        if _SHELL_STATE.match(seg):
            return prefix, None
    return prefix, body


def command_for_display(command):
    """A command with any heredoc body taken out, for showing to a person.

    A heredoc is how a script gets written, and collapsing one onto a single
    line put its source into the middle of what claimed to be the command -
    `cat > train.py <<PY import time for i in range(14): print(...)`. The body is
    not a command; it is the file the command wrote."""
    return one_line(HEREDOC.sub(" <<(script)", command or ""))


def classify_command(command, tool_input=None, cfg=None):
    """Decide whether a Bash command deserves a progress bar.

    Returns {"track": bool, "why": str, "signal": str, "name": str}. `why` is
    written to be read by Claude, so it explains itself in one clause."""
    cfg = cfg or load_config()
    tool_input = tool_input or {}
    command = (command or "").strip()
    # Only the head of a command is worth scanning. Anything longer is a
    # heredoc or an inline payload, and scanning all of it is what makes a
    # pathological command expensive.
    head = command[:2000]
    result = {"track": False, "why": "", "signal": "",
              "name": suggest_job_name(command[:2000])}

    if not command or cfg["auto_track"] == "off":
        result["why"] = "auto-tracking is off" if command else "empty command"
        return result

    segments = command_segments(command) or [head]

    # An unanchored rule - the plugin talking to itself, a --help, a --dry-run -
    # is about the command as a whole. A rule anchored to the start says "this
    # command is a trivial one", and only settles it if every part of a compound
    # command is trivial: `mkdir -p out && python train.py` is not.
    trivial = []
    for rx in AUTO_TRACK_IGNORE + _split_patterns(cfg["auto_track_ignore"]):
        try:
            if rx.lstrip().startswith("^"):
                trivial.append(rx)
            elif re.search(rx, head):
                result["why"] = "matches an ignore rule"
                return result
        except re.error:
            continue
    if trivial and all(any(_safe_search(rx, seg) for rx in trivial) for seg in segments):
        result["why"] = "every part of it is a trivial command"
        return result

    # Some commands act on the shell they run in - `cd`, `export`, `source`, a
    # bare assignment - and wrapping them in a shell of their own throws that
    # away: `cd repo && pytest` left the session in the old directory. When
    # they lead, they stay outside and only the work is wrapped; anywhere
    # else, the command runs untouched. And a command that backgrounds itself
    # with `&` returns at once, before the wrapper has anything to watch,
    # while its output goes to a log the wrapper then deletes.
    prefix, body = split_shell_prefix(command)
    if body is None:
        result["why"] = "part of it changes the shell it runs in, or leaves it"
        return result
    last = re.sub(r"(?:^|\s)#[^\n]*$", "", head.rstrip().split("\n")[-1])
    if re.search(r"(?<![&|>])&\s*$", last):
        result["why"] = "it puts itself in the background"
        return result
    result["prefix"], result["body"] = prefix, body
    result["name"] = suggest_job_name(body[:2000])
    segments = command_segments(body) or [body[:2000]]

    background = bool(tool_input.get("run_in_background"))
    if background and not cfg["auto_track_background"]:
        result["why"] = "backgrounded, and auto_track_background is off"
        return result

    try:
        timeout_s = float(tool_input.get("timeout") or 0) / 1000.0
    except (TypeError, ValueError):
        timeout_s = 0.0     # whatever the caller sent, it is not a timeout
    limit = cfg["auto_track_timeout_seconds"]

    if background:
        result.update(track=True, signal="background",
                      why="it was launched in the background")
        return result
    if limit and timeout_s >= limit:
        result.update(track=True, signal="timeout",
                      why="it was given a %s timeout" % fmt_short(timeout_s))
        return result
    for rx, label in AUTO_TRACK_PATTERNS:
        try:
            if any(_safe_search(rx, seg) for seg in segments):
                result.update(track=True, signal="pattern", why="it looks like %s" % label)
                return result
        except re.error:
            continue
    for rx in _split_patterns(cfg["auto_track_patterns"]):
        try:
            if re.search(rx, head):
                result.update(track=True, signal="pattern",
                              why="it matches one of your auto_track_patterns")
                return result
        except re.error:
            continue

    result["why"] = "nothing suggests this is long-running"
    return result


def launcher_prefix():
    """How to invoke this tool from a shell, as an absolute path.

    Never the bare name. This string is handed back to Claude Code and run by
    the session's own shell, whose PATH is not this process's - a session that
    was already running when the shim was installed still has the older one.
    A name that does not resolve there would fail the whole command, which is
    the user's command, not ours."""
    shim = os.path.join(os.path.expanduser("~"), ".local", "bin", "agent-progress")
    if os.path.isfile(shim) and os.access(shim, os.X_OK):
        return shlex.quote(shim)
    return "%s %s" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))


def wrap_command(command, name, launcher=None, after=None):
    """The tracked form of a command.

    The original is passed as a single quoted string, never interpolated raw:
    a command containing `&&`, `|` or a redirect would otherwise be cut in half,
    with the tail applying to the wrapper instead of to the command."""
    launcher = launcher or launcher_prefix()
    # One shape for everything, including a command the caller has already put in
    # the background. Detaching that one a second time made this call return at
    # once, so the caller's own background job looked finished the instant it
    # started - the plugin overruling a decision that was not its to make. The
    # wrapper runs the command and waits for it either way; whether that happens
    # in the foreground is the caller's business.
    opts = "" if after is None else " --after %s" % shlex.quote(str(after))
    return "%s exec --name %s%s --shell %s" % (
        launcher, shlex.quote(name), opts, shlex.quote(command))


def auto_seen(command, session_id, remember=True, ttl=6 * 3600):
    """True when this exact command was already flagged in this session.

    Without this, a command Claude deliberately re-runs untracked would be
    interrupted every single time."""
    # hashlib, not hash(): str hashing is salted per process, and every hook
    # invocation is a new process, so builtin hash() would never match
    digest = hashlib.sha1(command.strip().encode("utf-8")).hexdigest()[:16]
    key = "%s:%s" % (session_id or "-", digest)
    now = time.time()
    # This sits in front of every command, and all it decides is whether to ask
    # about the same command twice. Waiting seconds on a busy lock to answer
    # that is a worse outcome than answering "no" - several sessions writing at
    # once was making the hook stall before the command it was asked about.
    try:
        with state_rw(timeout=0.3) as st:
            seen = st.setdefault("auto_track_seen", {})
            for k, ts in list(seen.items()):
                if now - ts > ttl:
                    del seen[k]
            hit = key in seen
            if remember and not hit:
                seen[key] = now
            return hit
    except StateBusy:
        return False


# ------------------------------------------------------------- batch schedulers
#
# A job submitted to a queue has no process here to watch. `sbatch` returns in
# under a second having handed the work to a scheduler, and the run itself
# happens on some other machine for the next several hours. Two things are
# therefore needed that a local job gets for free: somewhere to read progress
# from, which is the file the scheduler writes, and some way to learn that it
# ended, which is the scheduler's own record of it.

# What a submission command prints back, how to read the id out of it, and -
# where the output alone is not enough - what the command has to have been.
#
# "Submitted batch job 4242" is a sentence no other command produces, so it is
# believed wherever it appears; that keeps a wrapper script which calls sbatch
# internally working. `qsub` answers with nothing but the job id, so its pattern
# is "a line that is just a number" - which is also what `echo $((X*2))`,
# `wc -l`, `nproc` and any script that prints a count produce. Matching that on
# output alone turned all of them into phantom queued jobs, each with a watcher
# polling `qstat` for an id that was never a job, so it needs the command too.
SUBMIT_PATTERNS = [
    # (pattern, scheduler, whether the command itself must vouch for it)
    (re.compile(r"Submitted batch job (\d+)"), "slurm", None),      # sbatch
    (re.compile(r"Job <(\d+)> is submitted"), "lsf", None),         # bsub
    # qsub prints a bare id, which on its own is indistinguishable from any
    # command that happens to print a number. Only trust it from qsub.
    (re.compile(r"^\s*(\d+(?:\.[\w-]+)?)\s*$", re.M), "pbs",
     re.compile(r"\bqsub\b")),
]

# Scheduler states, as words. Anything not named here leaves the job alone,
# because an unrecognised word is far more likely to mean "still going" than to
# mean the run is over.
DONE_STATES = {"COMPLETED", "COMPLETE", "DONE", "SUCCESS", "SUCCEEDED", "FINISHED", "OK"}
FAILED_STATES = {"FAILED", "FAIL", "TIMEOUT", "CANCELLED", "CANCELED", "OUT_OF_MEMORY",
                 "OOM", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "ERROR",
                 "SPECIAL_EXIT", "REVOKED",
                 "EXIT"}                # LSF: exited with a non-zero status

SLURM_STATE_CMD = (
    's=$(sacct -j %(id)s -n -o State -X 2>/dev/null | head -1); '
    '[ -z "$s" ] && s=$(squeue -j %(id)s -h -o %%T 2>/dev/null | head -1); '
    'echo "$s"')
LSF_STATE_CMD = "bjobs -noheader -o stat %(id)s 2>/dev/null | head -1"
# PBS answers with a letter - R, Q, H, E, F - and F says only that the job is
# finished, not how. Its exit status is on another line, so print that instead
# once the job is finished: a bare number reads as an exit code below.
PBS_STATE_CMD = ("qstat -x -f %(id)s 2>/dev/null | awk '/job_state =/{s=$3} "
                 "/Exit_status =/{e=$3} END{if(s==\"F\") print (e==\"\" ? 0 : e); "
                 "else if (s!=\"\") print s}'")

STATE_CMDS = {"slurm": SLURM_STATE_CMD, "lsf": LSF_STATE_CMD, "pbs": PBS_STATE_CMD}
CANCEL_CMDS = {"slurm": "scancel %(id)s", "lsf": "bkill %(id)s", "pbs": "qdel %(id)s"}


def detect_submission(text, command=None):
    """(scheduler, job id) if this output is a queue accepting work.

    `command` is what produced the output. Some schedulers answer with nothing
    but a number, which any command might print, so those are only believed
    when the command was the submission tool."""
    for rx, kind, needs in SUBMIT_PATTERNS:
        if needs is not None and not needs.search(command or ""):
            continue
        m = rx.search(text or "")
        if m:
            return kind, m.group(1)
    return None, None


# ------------------------------------------------------------------ slurm
#
# Slurm gets its own path rather than the generic "run a command, read a word".
# The word is the least of what it knows. One `scontrol show job` answers, in a
# single call and for the same cost, every question the bar actually has:
#
#   is it waiting or working      JobState
#   why is it still waiting       Reason=Resources / Priority / QOSMaxJobs...
#   where did it land             NodeList, Partition, NumNodes
#   how long has it really run    RunTime - which is not "time since I submitted"
#   how long may it run           TimeLimit, the one honest prior available
#                                 before the job has printed anything
#   where is its output           StdOut, so the log is read from where slurm
#                                 will actually write it
#
# and for an array, all of that per task, which is a progress bar for free:
# tasks finished out of tasks submitted.
#
# `scontrol` forgets a job MinJobAge (5 minutes by default) after it ends, so
# `sacct` is the fallback, and the only one that can still say how it went.

PENDING_STATES = {"PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD",
                  "REQUEUE_FED", "RESV_DEL_HOLD", "SUSPENDED", "SIGNALING",
                  "STAGE_OUT", "STOPPED", "PREEMPTED_HOLD"}
RUNNING_STATES = {"RUNNING", "COMPLETING", "RESIZING"}

SCONTROL_CMD = "scontrol show job %(id)s -o 2>/dev/null"
SACCT_CMD = ("sacct -j %(id)s -n -P -X -o State,Elapsed,Start,ExitCode,NodeList "
             "2>/dev/null")

# `Key=Value Key=Value`, where a value may itself contain spaces (Command=, and
# a Reason slurm chose to phrase as a sentence). A value therefore runs until
# the next `Key=` or the end of the line.
_KV = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)")


def slurm_seconds(text):
    """Slurm's durations: [DD-]HH:MM:SS, or MM:SS, or UNLIMITED."""
    text = (text or "").strip()
    if not text or text.upper() in ("UNLIMITED", "INVALID", "NOT_SET", "N/A"):
        return None
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def count_tasks(spec):
    """How many array tasks `ArrayTaskId=1-9:2,15` stands for."""
    spec = (spec or "").strip()
    if not spec:
        return 0
    spec = spec.split("%")[0]                 # 1-100%4 caps concurrency, not count
    n = 0
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        step = 1
        if ":" in chunk:
            chunk, _, st = chunk.partition(":")
            try:
                step = max(1, int(st))
            except ValueError:
                step = 1
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                n += len(range(int(lo), int(hi) + 1, step))
            except ValueError:
                continue
        else:
            n += 1
    return n


def parse_scontrol(text):
    """One dict per line `scontrol show job -o` printed - one line per task."""
    out = []
    for line in (text or "").splitlines():
        if "JobState=" not in line:
            continue
        rec = dict((m.group(1), m.group(2).strip()) for m in _KV.finditer(line))
        rec["_tasks"] = count_tasks(rec.get("ArrayTaskId")) or 1
        out.append(rec)
    return out


def parse_sacct(text):
    """State|Elapsed|Start|ExitCode|NodeList, one line per job step.

    A line with no separators at all is read as the state on its own - what
    `sacct -o State` prints, and what a site's wrapper around sacct is most
    likely to print. Accepting it costs nothing, and the alternative is to see
    a perfectly good answer and conclude the cluster is unreachable."""
    out = []
    for line in (text or "").splitlines():
        bits = line.split("|")
        if not bits[0].strip():
            continue
        out.append({"State": bits[0].strip(),
                    "Elapsed": bits[1].strip() if len(bits) > 1 else "",
                    "Start": bits[2].strip() if len(bits) > 2 else "",
                    "ExitCode": bits[3].strip() if len(bits) > 3 else "",
                    "NodeList": bits[4].strip() if len(bits) > 4 else "",
                    "_tasks": 1})
    return out


def _run(cmd, cwd=None, timeout=20):
    try:
        return subprocess.check_output(
            ["/bin/sh", "-c", cmd], stderr=subprocess.DEVNULL, timeout=timeout,
            cwd=cwd if os.path.isdir(cwd or "") else None,
        ).decode("utf-8", "replace")
    except Exception:
        return ""


def classify_slurm(word):
    """queued / running / done / failed / None, from one slurm state word."""
    word = re.sub(r"[^A-Z_0-9]", "", (word or "").strip().upper().replace(" ", "_"))
    base = word.split("_BY_")[0]              # CANCELLED_BY_12345
    if not base:
        return None
    if base in PENDING_STATES:
        return "queued"
    if base in RUNNING_STATES:
        return "running"
    if base in DONE_STATES:
        return "done"
    if base in FAILED_STATES:
        return "failed"
    return None


def slurm_probe(job_id, cwd=None):
    """Everything slurm will say about one job, in at most two calls.

    Returns None when slurm cannot be reached or has never heard of the job -
    which always means "change nothing". A job is never declared finished
    because a command was missing."""
    recs = parse_scontrol(_run(SCONTROL_CMD % {"id": shlex.quote(str(job_id))}, cwd))
    source = "scontrol"
    if not recs:
        recs = parse_sacct(_run(SACCT_CMD % {"id": shlex.quote(str(job_id))}, cwd))
        source = "sacct"
    if not recs:
        return None

    tally = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    total = 0
    run_seconds = limit_seconds = None
    reason = nodes = partition = stdout = word = None
    for rec in recs:
        n = rec.get("_tasks") or 1
        total += n
        verdict = classify_slurm(rec.get("JobState") or rec.get("State"))
        if verdict:
            tally[verdict] += n
        if word is None:
            word = (rec.get("JobState") or rec.get("State") or "").strip()
        # take the timings from whichever task is furthest along
        secs = slurm_seconds(rec.get("RunTime") or rec.get("Elapsed"))
        if secs is not None and (run_seconds is None or secs > run_seconds):
            run_seconds = secs
        lim = slurm_seconds(rec.get("TimeLimit"))
        if lim is not None and (limit_seconds is None or lim > limit_seconds):
            limit_seconds = lim
        if verdict == "running" or reason is None:
            r = (rec.get("Reason") or "").strip()
            if r and r not in ("None", "(null)"):
                reason = r
        for key, name in (("NodeList", "nodes"), ("Partition", "partition"),
                          ("StdOut", "stdout")):
            val = (rec.get(key) or "").strip()
            if val and val not in ("(null)", "None", "n/a"):
                if name == "nodes":
                    nodes = nodes or val
                elif name == "partition":
                    partition = partition or val
                else:
                    stdout = stdout or val

    # The job as a whole. Anything still running or queued outranks the tasks
    # that have finished: a job is over only when none of it is left.
    requeued = False
    if tally["running"]:
        state = "running"
    elif tally["queued"]:
        state = "queued"
    elif tally["failed"] and tally["done"] and source == "sacct" and any(
            (rec.get("State") or "").split("_BY_")[0].split()[0] in REQUEUE_STATES
            for rec in recs if rec.get("State")) and classify_slurm(
            recs[-1].get("JobState") or recs[-1].get("State")) == "done":
        # sacct lists every attempt. A job that was preempted and requeued has
        # a PREEMPTED row and then the row that finished; the first is history,
        # and calling the whole job failed on it reported a run that completed
        # as one that died.
        state = "done"
        requeued = True
        word = (recs[-1].get("JobState") or recs[-1].get("State") or "").strip()
    elif tally["failed"]:
        state = "failed"
    elif tally["done"]:
        state = "done"
    else:
        state = None                          # a word slurm knows and we do not
    return {"state": state, "word": word, "reason": reason, "nodes": nodes,
            "partition": partition, "stdout": stdout, "source": source,
            "run_seconds": run_seconds, "limit_seconds": limit_seconds,
            "tasks_total": total, "tasks_done": tally["done"] + tally["failed"],
            "tasks_running": tally["running"], "tasks_queued": tally["queued"],
            "tasks_failed": tally["failed"], "requeued": requeued}


def apply_batch_info(job, info, now):
    """Fold one scheduler reading into a job. Returns "done"/"failed" if over.

    The interesting moment is the queued -> running transition. Everything the
    bar measures is anchored on `started`, and until now that was the moment of
    submission - so a job that sat in the queue for four hours and then ran for
    ten minutes reported four hours and ten minutes of "elapsed", an ETA drawn
    from it, and a throughput averaged over four hours of doing nothing. Slurm
    knows how long the job has actually been running, so on the transition the
    clock is re-anchored to when it started and the queue-time samples, which
    measured nothing, are dropped."""
    if not info:
        return None
    if info.get("word"):
        job["scheduler_state"] = info["word"]
    for key, field in (("reason", "queue_reason"), ("nodes", "nodes"),
                       ("partition", "partition")):
        if info.get(key):
            job[field] = info[key]

    # the output file is only known for certain once slurm has opened it
    path = info.get("stdout")
    if path and path != job.get("log"):
        job["log"] = path
        job["log_offset"] = 0

    state = info.get("state")
    if state in ("running", "queued") and job.get("state") in ACTIVE_STATES:
        if job.get("state") == "queued" and state == "running":
            job["queued_seconds"] = max(0.0, now - (job.get("submitted") or now))
            job["queue_reason"] = None
            job["samples"] = []          # measured the queue, not the work
            job["units"] = job["step"] = job["pct"] = None
        if state == "running" and info.get("run_seconds") is not None:
            job["started"] = now - info["run_seconds"]
        job["state"] = state

    # An array is its own progress bar: tasks finished out of tasks submitted.
    # Better than anything the log of one task could say, so it wins outright.
    seen = info.get("tasks_total") or 0
    known = job.get("total") or 0
    if seen > 1 or (known > 1 and job.get("unit") == "task"):
        # The total is the largest count ever seen, never the latest. `scontrol`
        # reports what is left, so once seven of eight tasks have finished it
        # prints one line - and taking that as the total would rewrite an
        # eight-task sweep as a one-task one and send the bar back to zero.
        total = max(known, seen)
        job["total"] = total
        job["total_locked"] = True
        job["unit"] = "task"
        left = (info.get("tasks_running") or 0) + (info.get("tasks_queued") or 0)
        done = max(0, total - left)
        if done != job.get("units"):
            job["units"] = float(done)
            job["step"] = done
            record_sample(job, float(done), now)
            job["updated"] = now
            job["progress_source"] = "scheduler"

    # A time limit is not an estimate of how long the job takes, but before the
    # job has printed anything it is the only number in existence, and an upper
    # bound beats no bound: the bar says `~` for as long as it is being used.
    if (info.get("limit_seconds") and not job.get("eta_end")
            and not job.get("eta_prior_s")):
        job["eta_prior_s"] = info["limit_seconds"]
        job["eta_end"] = (job.get("started") or now) + info["limit_seconds"]
        job["note"] = (job.get("note")
                       or "estimate is the slurm time limit, not a measurement")

    return state if state in ("done", "failed") else None


def describe_queue(job):
    """The one phrase a queued job has to say: why it is still waiting."""
    bits = []
    reason = job.get("queue_reason")
    if reason:
        bits.append(QUEUE_REASONS.get(reason, reason))
    if job.get("partition"):
        bits.append("on " + job["partition"])
    return ", ".join(bits)


# Slurm's Reason codes are terse to the point of being cryptic the first time.
# Only the ones worth rewording are here; anything else is shown as slurm said it.
QUEUE_REASONS = {
    "Resources": "waiting for nodes",
    "Priority": "behind higher-priority jobs",
    "Dependency": "waiting on another job",
    "JobHeldUser": "held by you",
    "JobHeldAdmin": "held by an admin",
    "BeginTime": "not due to start yet",
    "ReqNodeNotAvail": "requested nodes unavailable",
    "QOSMaxJobsPerUserLimit": "at your QOS job limit",
    "AssocMaxJobsLimit": "at your account's job limit",
    "QOSGrpCpuLimit": "at the QOS CPU limit",
    "PartitionDown": "partition is down",
    "PartitionNodeLimit": "asks for more nodes than the partition has",
    "AssocGrpGRES": "at your account's GPU limit",
    "Licenses": "waiting for a license",
}


def slurm_log_path(job_id, cwd=None, info=None):
    """Where the scheduler will write the job's output.

    Asked of slurm rather than guessed, because a script that set --output to
    anywhere but the default would otherwise have its bar watching a file that
    never appears. `info` lets a caller that has already probed reuse it."""
    if info is None:
        recs = parse_scontrol(_run(SCONTROL_CMD % {"id": shlex.quote(str(job_id))}, cwd))
        info = {"stdout": (recs[0].get("StdOut") if recs else None)}
    path = (info or {}).get("stdout")
    if path:
        # %j and friends are already expanded by the time scontrol reports it,
        # but an unstarted array task can still carry them
        path = path.replace("%j", str(job_id)).replace("%A", str(job_id))
        return path
    # slurm's default, which is what most scripts leave it as
    return os.path.join(cwd or os.getcwd(), "slurm-%s.out" % job_id)


def read_state_probe(job):
    """Ask the scheduler how the job is doing.

    Returns "running", "done", "failed", or None when the answer is unusable -
    the command is missing, the cluster is unreachable, the word is unfamiliar.
    None always means "leave it alone": a job is never finished on a guess."""
    cmd = job.get("state_probe")
    if not cmd:
        return None
    try:
        out = subprocess.check_output(
            ["/bin/sh", "-c", cmd], stderr=subprocess.DEVNULL, timeout=30,
            cwd=job.get("cwd") if os.path.isdir(job.get("cwd") or "") else None,
        ).decode("utf-8", "replace")
    except Exception:
        return None
    word = (out or "").strip().split("\n")[-1].strip().upper()
    word = word.split("=")[-1].strip()       # "job_state = F" -> "F"
    word = re.sub(r"[^A-Z_0-9]", "", word.replace(" ", "_"))
    if not word:
        return None
    job["scheduler_state"] = word
    if word.isdigit():                      # a bare exit code, for anything else
        return "done" if word == "0" else "failed"
    base = word.split("_BY_")[0]            # CANCELLED_BY_12345
    if base in DONE_STATES:
        return "done"
    if base in FAILED_STATES:
        return "failed"
    return "running"


# ------------------------------------------------------------------- crashes
#
# When a job dies, three things have to happen: the bar gets a skull, the
# desktop gets a notification, and the Claude session gets told. The session is
# the awkward one - nothing can push a message into a running session from
# outside - so the crash is queued here and the plugin's hooks deliver it at the
# first opportunity (see hooks/inject_status.py).

SIGNAL_NAMES = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 6: "SIGABRT",
    8: "SIGFPE", 9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 15: "SIGTERM",
    24: "SIGXCPU", 25: "SIGXFSZ", 31: "SIGSYS",
}
SIGNAL_HINTS = {
    9: "killed outright - most often the OOM killer",
    11: "segfault",
    6: "aborted",
    15: "terminated",
    2: "interrupted",
    24: "hit the CPU time limit",
    31: "bad system call",
}


def crash_reason(code):
    """(short, explanatory) description of a non-zero exit."""
    if code is None:
        return "no exit code", "died without reporting an exit code"
    if code > 128:
        sig = code - 128
        name = SIGNAL_NAMES.get(sig, "signal %d" % sig)
        hint = SIGNAL_HINTS.get(sig)
        return name, ("%s - %s" % (name, hint)) if hint else name
    return "exit %d" % code, "exited with status %d" % code


CRASH_LINE_CHARS = 200      # what the report shows of any one line
CRASH_TAIL_CHARS = 4000     # and of all of them together


def enqueue_crash(st, job, now, kind="crash"):
    """Queue a report about a finished job for the session that started it.

    Both endings are news. A job that fails is obvious news; a job that succeeds
    is the news the user was waiting for, and until now it was thrown away - the
    command was handed off, its output went to the log, and when it finished
    nothing said so and nothing gave back the result it had produced. Asking for
    a model to be trained and never being told it finished is the plugin
    swallowing the answer."""
    tail = ""
    if job.get("log"):
        text, _ = read_tail(job["log"], 0, 65536)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # Store what the report will actually show. The renderer already cuts
        # each line at 200 characters, so keeping the whole of fifteen lines put
        # up to 64KB per crash into a file that is rewritten on every job
        # update, every watcher tick and every hook - and the queue holds
        # hundreds of them.
        tail = "\n".join(ln[:CRASH_LINE_CHARS] for ln in lines[-15:])[:CRASH_TAIL_CHARS]
    short, why = crash_reason(job.get("exit_code"))
    if job.get("scheduler_state"):
        # the scheduler's own word beats an invented exit code
        short = job["scheduler_state"]
        why = "%s, as reported by the scheduler" % job["scheduler_state"]
    st.setdefault("inbox", []).append({
        "kind": kind,
        "job": job.get("id"),
        "ts": now,
        "exit_code": job.get("exit_code"),
        "reason": why,
        "reason_short": short,
        "cmd": job.get("cmd"),
        "log": job.get("log"),
        "log_tail": tail,
        "duration": (job.get("ended") or now) - (job.get("started") or now),
        "session_id": job.get("session_id"),
        "bridge_id": job.get("bridge_id"),
        "delivered": None,
    })
    trim_inbox(st)


CRASH_KEEP = 50         # the comfortable size of the queue
CRASH_CEILING = 200     # and the point past which even undelivered ones go


def trim_inbox(st):
    """Keep the queue small without losing anything anyone still needs.

    Dropping the oldest entries is wrong when several agents share the queue: a
    session that crashes sixty times evicts the one report a quieter session had
    not yet collected, and that session then never learns its job died. Reports
    already handed over have done their work and go first.

    Since the queue also carries jobs that merely finished, the order matters
    once more: news of a death outranks news of a success, so a machine that
    completes hundreds of small jobs cannot push out the one report saying
    something died."""
    inbox = st.get("inbox") or []
    if len(inbox) <= CRASH_KEEP:
        return

    def rank(e):
        if e.get("delivered"):
            return 0                    # said its piece already
        return 1 if e.get("kind") == "done" else 2

    pending = [e for e in inbox if not e.get("delivered")]
    delivered = [e for e in inbox if e.get("delivered")]
    room = max(0, CRASH_KEEP - len(pending))
    kept = pending + (delivered[-room:] if room else [])
    kept.sort(key=lambda e: e.get("ts") or 0)
    if len(kept) > CRASH_CEILING:
        # keep the most important, then the most recent among equals
        kept.sort(key=lambda e: (rank(e), e.get("ts") or 0))
        kept = kept[-CRASH_CEILING:]
        kept.sort(key=lambda e: e.get("ts") or 0)
    st["inbox"] = kept


def orphan_grace(cfg=None):
    """How long a crash waits for the session that owns it.

    Hooks only run when a session is prompted or finishes a turn, so an agent
    that is merely idle looks exactly like one that has exited. Ten minutes
    could not tell them apart and handed live agents' reports to their
    neighbours; an hour is long enough that anything still unclaimed is very
    likely gone, and the report is visible in `agent-progress inbox` the whole
    time either way."""
    return (cfg or load_config())["crash_handover_seconds"]


def take_crash(session_id=None):
    """Claim the oldest undelivered crash that belongs here.

    A crash is news for the session that started the job, not for whichever
    session asks first - telling agent B that agent A's training died is both
    wrong and, because it is then marked delivered, the reason agent A never
    hears about it. A report nobody has claimed after crash_handover_seconds is offered to
    anyone, so a crash whose session has since exited is not lost."""
    now = time.time()
    claimed = None
    with state_rw() as st:
        pending = [e for e in st.get("inbox", []) if not e.get("delivered")]
        # scope=all means one shared view of everything, crashes included
        if load_config()["scope"] == "all":
            mine = list(pending)
        else:
            mine = [e for e in pending if job_belongs_here(e, session_id)]
        grace = orphan_grace()
        orphaned = [e for e in pending
                    if e.get("session_id") and (now - (e.get("ts") or 0)) > grace]
        for ev in (mine or orphaned):
            ev["delivered"] = {"session_id": session_id, "ts": now}
            claimed = dict(ev)
            # a report taken on behalf of a session that never came back has to
            # say so, or it is read as this session's own job having died
            # identity, not equality: two crashes of the same job at the same
            # instant compare equal, and `in` would then call one of them ours
            claimed["handover"] = not any(ev is m for m in mine)
            break
    return claimed


def pending_crashes():
    return [e for e in state_ro().get("inbox", []) if not e.get("delivered")]


def format_beside(ev, cfg=None):
    """The same news, written for the person rather than for Claude.

    Shown next to the conversation, so it is read by whoever is sitting there:
    no instructions about what to do with it, no telling them not to re-run
    their own job, and short enough to take in at a glance."""
    cfg = cfg or load_config()
    ev = ev or {}
    ended_well = ev.get("kind") == "done"
    glyph = cfg["glyph_done"] if ended_well else cfg["glyph_failed"]
    head = "%s %s %s" % (glyph, ev.get("job"),
                         "finished" if ended_well else (ev.get("reason_short") or "failed"))
    if ev.get("duration") is not None:
        head += " after %s" % fmt_dur(ev["duration"])
    lines = [head]
    if ev.get("handover"):
        lines.append("  (started by another session, which has since gone)")
    for ln in (ev.get("log_tail") or "").splitlines()[-6:]:
        lines.append("  " + ln[:120])
    lines.append("  agent-progress log %s -n 60" % (ev.get("job") or "<id>"))
    return "\n".join(lines)


def format_report(ev, cfg=None):
    """What a session is handed when a job it started ends.

    Both endings say the same things - what it was, how long it ran, the command,
    the log, and the tail of what it printed - and differ only in the word for
    what happened and what to do about it. A job that finished is reported for
    the sake of that tail: the command was handed off long ago, and this is the
    only way its result comes back to the conversation.
    """
    cfg = cfg or load_config()
    ev = ev or {}
    ended_well = ev.get("kind") == "done"
    glyph = cfg["glyph_done"] if ended_well else cfg["glyph_failed"]
    word = "FINISHED" if ended_well else "CRASHED"
    lines = []
    if ev.get("handover"):
        lines.append(
            "%s A job from ANOTHER session %s, and that session never collected the "
            "report - it has probably exited. This was not your job: say so if you "
            "mention it, and do not re-run it." % (glyph, word.lower()))
    lines.append("%s A tracked job %s while you were working: '%s'"
                 % (glyph, word, ev.get("job")))
    if ended_well:
        lines.append("  it ran for %s" % fmt_dur(ev.get("duration")))
    else:
        lines.append("  %s after %s" % (ev.get("reason"), fmt_dur(ev.get("duration"))))
    if ev.get("cmd"):
        cmd = command_for_display(ev["cmd"])
        lines.append("  command: %s" % (cmd[:200] + ("..." if len(cmd) > 200 else "")))
    if ev.get("log"):
        lines.append("  log: %s" % ev["log"])
    if ev.get("log_tail"):
        lines.append("  last output:")
        for ln in ev["log_tail"].splitlines()[-15:]:
            lines.append("    " + ln[:200])
    if ended_well:
        lines.append("Tell the user it finished and what it produced, reading the result "
                     "out of the output above. `agent-progress log %s -n 60` if you need "
                     "more of it. Do not re-run it." % (ev.get("job") or "<id>"))
    else:
        lines.append("Tell the user this job crashed, summarize why from the output above, "
                     "and suggest a fix if the cause is clear. Do not re-run it without "
                     "asking.")
    return "\n".join(lines)


# ------------------------------------------------------------------- monitors
#
# A job only has a progress bar if something observable moves. Rather than
# assuming every job prints "Epoch 3/50", the caller declares *how* to watch
# this particular job, once, at start. Everything after that is automatic.

MONITOR_KINDS = ("auto", "log", "milestones", "files", "size", "probe", "time")

MONITOR_HELP = """\
auto        (default) tail the log and look for any known progress marker;
            fall back to wall-clock against the estimate if none appear
log         same, with your own regex:  --pattern 'done (?P<step>\\d+)/(?P<total>\\d+)'
milestones  ordered stages that appear in the log, each worth an equal slice:
            --milestones 'loading data;training;evaluating;writing output'
files       count files produced so far:  --glob 'out/shard-*.parquet' --total 500
size        an output file or directory growing toward a size:
            --path out/index.bin --target-size 12GB
probe       run any shell command that prints 'k/N', 'k', or 'NN%':
            --probe 'psql -tAc "select count(*) from rows"' --total 2000000
time        no observable signal; the bar runs on the estimate alone
"""


def parse_size(text):
    """'12GB', '500 MB', '1.5t', '4096' -> bytes."""
    if text is None:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*([kmgtp]?)i?b?\s*$", str(text).strip().lower())
    if not m:
        raise SystemExit("could not parse size: %r (try 500MB, 12GB)" % text)
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3,
            "t": 1024 ** 4, "p": 1024 ** 5}[m.group(2)]
    return int(float(m.group(1)) * mult)


def fmt_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def path_size(path):
    """Bytes at `path`, whether it is a file or a directory tree."""
    if not path or not os.path.exists(path):
        return None
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return None
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def parse_probe_output(out, known_total=None):
    """A probe command may print 'k/N', a bare 'k', or 'NN%'."""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return None
    s = lines[-1]
    m = re.match(r"^(\d+(?:\.\d+)?)\s*%$", s)
    if m:
        return {"step": None, "total": None, "pct": float(m.group(1)) / 100.0,
                "sub": None, "src": "probe"}
    m = re.match(r"^(\d+)\s*/\s*(\d+)$", s)
    if m:
        return {"step": int(m.group(1)), "total": int(m.group(2)),
                "pct": None, "sub": None, "src": "probe"}
    m = re.match(r"^(\d+)$", s)
    if m:
        return {"step": int(m.group(1)), "total": known_total,
                "pct": None, "sub": None, "src": "probe"}
    return parse_progress(out, None, known_total)   # fall back to the generic parser


def tail_job_log(job, max_bytes=262144):
    """Incremental read of the job's log, remembering the offset in job state."""
    log = job.get("log")
    if not log or not os.path.exists(log):
        return ""
    text, new_off = read_tail(log, job.get("log_offset") or 0, max_bytes)
    job["log_offset"] = new_off
    return text


def _size_of(path):
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


def _milestone_hit(name, text):
    """Has this stage appeared in the log?

    Plain text first. A stage is something a person typed to describe a phase of
    a job - "normalizing (v2)", "cost was $5" - and reading those as regexes
    quietly breaks them: the parentheses become a group, the dollar an anchor.
    A regex is still honoured if the literal is not found, so a deliberate
    pattern keeps working."""
    if name.lower() in text.lower():
        return True
    try:
        return re.search(name, text, re.I) is not None
    except re.error:
        return False


def monitor_reading(job, now=None):
    """Observe the job once, however this job is configured to be observed.

    Returns a reading dict shaped like parse_progress(), or None if the job
    offers no observable signal right now (which is fine - the estimate carries
    the bar in that case)."""
    mon = job.get("monitor") or {"kind": "auto"}
    kind = mon.get("kind") or "auto"

    if kind == "time":
        return None

    if kind in ("auto", "log"):
        text = tail_job_log(job)
        if not text.strip():
            return None
        return parse_progress(text, job.get("pattern"), job.get("total"))

    if kind == "milestones":
        names = mon.get("milestones") or []
        if not names:
            return None
        hit = list(job.get("milestones_hit") or [])
        text = tail_job_log(job)
        if text:
            for n in names:
                if n in hit:
                    continue
                if _milestone_hit(n, text):
                    hit.append(n)
        job["milestones_hit"] = [n for n in names if n in hit]   # keep declared order
        return {"step": len(job["milestones_hit"]), "total": len(names),
                "pct": None, "sub": None, "src": "milestones"}

    if kind == "files":
        pat = mon.get("glob")
        if not pat:
            return None
        n = len(globmod.glob(os.path.expanduser(pat), recursive=True))
        return {"step": n, "total": mon.get("total") or job.get("total"),
                "pct": None, "sub": None, "src": "files"}

    if kind == "size":
        cur = path_size(os.path.expanduser(mon.get("path") or ""))
        if cur is None:
            return None
        job["size_bytes"] = cur
        target = mon.get("target_bytes")
        if not target:
            return None            # growing, but with no target there is no fraction
        return {"step": None, "total": None,
                "pct": max(0.0, min(1.0, cur / float(target))),
                "sub": None, "src": "size"}

    if kind == "probe":
        cmd = mon.get("cmd")
        if not cmd:
            return None
        try:
            out = subprocess.check_output(
                ["/bin/sh", "-c", cmd], stderr=subprocess.DEVNULL,
                timeout=float(mon.get("timeout", 30)),
                cwd=job.get("cwd") if os.path.isdir(job.get("cwd") or "") else None,
            ).decode("utf-8", "replace")
        except Exception:
            return None
        return parse_probe_output(out, mon.get("total") or job.get("total"))

    return None


def describe_monitor(job):
    mon = job.get("monitor") or {"kind": "auto"}
    kind = mon.get("kind", "auto")
    sched, job_id = batch_of(job)
    if sched:
        where = "the %s queue (job %s)" % (sched, job_id)
        if job.get("state") == "queued":
            return where
        return "%s, and %s" % (where, job.get("log") or "its output file")
    if kind == "milestones":
        hit = len(job.get("milestones_hit") or [])
        return "milestones %d/%d" % (hit, len(mon.get("milestones") or []))
    if kind == "files":
        return "files matching %s" % mon.get("glob")
    if kind == "size":
        return "size of %s%s" % (mon.get("path"),
                                 " -> %s" % fmt_size(mon.get("target_bytes"))
                                 if mon.get("target_bytes") else "")
    if kind == "probe":
        return "probe: %s" % mon.get("cmd")
    if kind == "time":
        return "wall clock only"
    return "log markers" + (" (custom pattern)" if job.get("pattern") else "")


def poll_interval(job, cfg, est_total_s):
    """How often to observe. Never more than once per `min_interval_seconds`,
    and never more than `interval_fraction` of the job's own estimated length -
    so a 10-hour job is not polled every 2 seconds."""
    if job.get("interval_override"):
        return max(1.0, float(job["interval_override"]))
    base = float(cfg.get("min_interval_seconds", 120))
    frac = float(cfg.get("interval_fraction", 0.05)) * float(est_total_s or 0)
    return max(base, frac)


# ------------------------------------------------------------------ the watcher

def spawn_watcher(jid):
    """Detach a tiny process that tails the log and keeps the job's state fresh."""
    script = os.path.abspath(__file__)
    err = open(os.path.join(ROOT, "watcher.log"), "a")
    p = subprocess.Popen(
        [sys.executable, script, "_watch", jid],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
        start_new_session=True,
    )
    return p.pid


def notify(title, message, ok=True):
    if not load_config()["notify"] or sys.platform != "darwin":
        return
    cfg = load_config()
    sound = cfg["notify_sound_ok"] if ok else cfg["notify_sound_fail"]
    script = 'display notification %s with title %s sound name "%s"' % (
        json.dumps(message), json.dumps(title), sound)
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def record_sample(job, units, now):
    """Append to the rate-estimation ring buffer, ignoring non-advances."""
    samples = job.setdefault("samples", [])
    # not-a-number compares False against everything, so the "did it advance"
    # test below would wave it through and every rate after it would be nan
    if not isinstance(units, (int, float)) or isinstance(units, bool) \
            or not math.isfinite(units):
        return
    if samples and units <= samples[-1][1]:
        return
    samples.append([now, units])
    del samples[:-int(load_config()["rate_window"]) * 3]


def apply_reading(job, reading, now):
    """Fold one parsed log reading into the job's counters."""
    if not reading:
        return False
    # An array's bar is its task count, from the scheduler. A reading from one
    # task's log said "epoch 3 of 50" and was written over "7 of 8 tasks" every
    # tick, so the bar flip-flopped between the two.
    if job.get("unit") == "task" and job.get("total_locked"):
        return False
    # A 300-digit "step" is not progress, and turning it into a float raises
    if any(isinstance(reading.get(k), int) and abs(reading[k]) > 10 ** 15
           for k in ("step", "total")):
        return False
    changed = False
    total = reading.get("total")
    if total and not job.get("total_locked"):
        if job.get("total") != total:
            job["total"] = total
            changed = True

    step, sub = reading.get("step"), reading.get("sub")
    units = None
    if step is not None:
        if step == 0:
            # Lightning prints "Epoch 0:" first, so this log is 0-indexed
            job["zero_indexed"] = True
        if sub is None:
            # a plain counter: "Epoch 12/50" means 12 are finished
            units = float(step)
        else:
            # outer counter + inner bar: epoch `step` is only `sub` done
            base = step if job.get("zero_indexed") else max(0, step - 1)
            units = float(base) + sub
    elif reading.get("pct") is not None and job.get("total"):
        units = reading["pct"] * job["total"]

    if units is not None and units != job.get("units"):
        job["units"] = units
        job["step"] = int(units)
        record_sample(job, units, now)
        changed = True

    if reading.get("pct") is not None and not job.get("total"):
        if job.get("pct") != reading["pct"]:
            job["pct"] = reading["pct"]
            record_sample(job, reading["pct"] * 1000.0, now)
            changed = True

    if changed:
        job["updated"] = now
        job["progress_source"] = reading.get("src")
    return changed


def finalize(job, exit_code, now, st=None):
    """Close a job out. Pass `st` when the failure was detected automatically -
    that queues a crash report for the Claude session; a failure the caller
    already knows about (agent-progress fail) should not."""
    job["state"] = "done" if exit_code in (0, None) else "failed"
    job["exit_code"] = exit_code
    job["ended"] = now
    job["updated"] = now
    if job["state"] == "done" and job.get("total") and job.get("units"):
        job["units"] = job["total"]
        job["step"] = job["total"]
    # A job whose caller waited for it needs no report: they watched it happen
    # and have its output and its exit code. Whether the wrapper or the watcher
    # gets here first should not decide whether they are told twice. But if that
    # caller was killed outright, nobody read the output, and the report becomes
    # the only way the result comes back.
    waited_for = job.get("caller_waits") and (
        not job.get("waiter_pid") or not pid_here(job) or alive(job.get("waiter_pid")))
    if st is not None and not waited_for:
        if job["state"] == "failed":
            enqueue_crash(st, job, now)
        elif job["state"] == "done" and load_config()["announce_done"]:
            enqueue_crash(st, job, now, kind="done")



def ended_already(job):
    """Has this job finished without anyone having noticed yet?

    The same two pieces of evidence the watcher trusts, and for the same
    reasons: an exit file this plugin wrote itself, which cannot be mistaken
    for anything else, and - for a job we merely attached to - a pid of ours
    that is gone. Silence is not evidence, so a job that is merely quiet is
    left alone."""
    if job.get("state") not in ACTIVE_STATES:
        return False
    own_exit = job.get("exit_file")
    if own_exit and os.path.exists(own_exit):
        return True
    return job.get("pid") is not None and not job_pid_alive(job)


def reap_ended(session_id=None, now=None):
    """Close out jobs that have already ended but whose watcher has not looked.

    A watcher notices a death on its next tick, which can be several seconds
    away. A job that dies a second after it starts is therefore still marked
    `running` when the turn ends, and the report saying it died waits for the
    next tick - which is to say, until after the person has spoken again. That
    is the one thing they asked always to hear about, so the turn ending looks
    for itself rather than waiting to be told.

    Cheap on purpose: a stat per active job, no probe, and the write lock is
    taken only when something actually looks finished."""
    now = now or time.time()
    try:
        snapshot = state_ro()
    except Exception:
        return []
    candidates = [jid for jid, job in snapshot["jobs"].items()
                  if ended_already(job) and job_belongs_here(job, session_id)]
    if not candidates:
        return []
    reaped = []
    try:
        with state_rw() as st:
            for jid in candidates:
                job = st["jobs"].get(jid)
                # Re-checked under the lock: the watcher may have won the race,
                # and whoever gets here second must not report it twice.
                if job is None or not ended_already(job):
                    continue
                code, wrote_status = job.get("exit_code"), False
                try:
                    with open((job.get("log") or "") + ".exit") as f:
                        code = int(f.read().strip())
                    wrote_status = True
                except Exception:
                    pass
                if not wrote_status and job.get("exit_file"):
                    code = code if code not in (None, 0) else 137
                    job["note"] = job.get("note") or "killed before it finished"
                finalize(job, code, now, st)
                reaped.append(dict(job))
    except Exception:
        return reaped
    return reaped

REQUEUE_STATES = {"PREEMPTED", "NODE_FAIL", "REQUEUED", "BOOT_FAIL"}


def revive_stalled(session_id=None, now=None):
    """A stalled job that has started writing again is running again.

    Stalled means only that nothing was seen for a long time; it is the
    watcher giving up, not the job ending. A job that resumes - a training
    run restarted from its checkpoint into the same log - was left frozen at
    the pause glyph with nothing watching it. Any growth in the log since it
    was declared stalled is life: the record goes back to running and, unless
    it asked for no watcher, gets one."""
    now = now or time.time()
    try:
        snapshot = state_ro()
    except Exception:
        return []
    def woke(job):
        if job.get("state") != "stalled" or not job.get("log"):
            return False
        try:
            st_ = os.stat(job["log"])
        except OSError:
            return False
        since = job.get("ended") or job.get("updated") or 0
        return st_.st_mtime > since or (job.get("size_bytes") or 0) < st_.st_size
    candidates = [jid for jid, job in snapshot["jobs"].items()
                  if isinstance(job, dict) and woke(job) and job_belongs_here(job, session_id)]
    if not candidates:
        return []
    revived = []
    try:
        with state_rw() as st:
            for jid in candidates:
                job = st["jobs"].get(jid)
                if job is None or not woke(job):
                    continue
                job["state"] = "running"
                job["ended"] = None
                job["exit_code"] = None
                job["updated"] = now
                job["note"] = (job.get("note") or "").replace("[no progress seen]", "").strip() or None
                job["resumed"] = (job.get("resumed") or 0) + 1
                if not job.get("no_watch"):
                    job["watcher_pid"] = spawn_watcher(jid)
                revived.append(jid)
    except Exception:
        return revived
    return revived


OBSERVED_FIELDS = ("milestones_hit", "size_bytes", "scheduler_state")


def batch_of(job):
    """The scheduler and id of a job that belongs to a queue, or (None, None)."""
    batch = job.get("batch")
    if not isinstance(batch, dict):
        return None, None
    return batch.get("scheduler"), batch.get("job_id")


def observe(snapshot, now, progress=False, scheduler=False):
    """Look at a job. Never call this while holding the state lock.

    Both kinds of observation can run a command - a `--probe` monitor, and the
    scheduler query for a queued job - and a command that hangs takes its full
    timeout to come back. Run under the exclusive lock, as they used to be,
    that was up to thirty seconds in which nothing else could touch the state
    file: not another watcher, not a job trying to record that it had finished,
    not the hook that sits in front of every Bash command. On a busy or flaky
    cluster `squeue` hanging is ordinary, so this was a routine way for the
    plugin to stall the session it is meant to stay out of.

    The observation therefore runs against a copy of the job, and only its
    result is carried back under the lock."""
    reading = verdict = info = None
    if progress:
        try:
            reading = monitor_reading(snapshot, now)
        except Exception:
            reading = None                # a broken probe must not kill the bar
    if scheduler:
        kind, job_id = batch_of(snapshot)
        try:
            if kind == "slurm" and job_id:
                info = slurm_probe(job_id, snapshot.get("cwd"))
                verdict = (info or {}).get("state")
                if verdict not in ("done", "failed"):
                    verdict = None        # queued and running are not endings
            else:
                verdict = read_state_probe(snapshot)
        except Exception:
            verdict, info = None, None
    updates = dict((k, snapshot[k]) for k in OBSERVED_FIELDS if k in snapshot)
    return reading, verdict, updates, info


def cmd_watch_daemon(args):
    """Keep one job's state fresh, and keep doing it if the state file is busy.

    A watcher that gave up the first time it could not take the lock would
    leave a live job with a frozen bar until some session happened to start and
    revive it. Waiting and trying again costs nothing - the loop is asleep
    almost all of the time - and the only thing lost is one probe's timing."""
    strikes = 0
    while True:
        try:
            return _watch_loop(args)
        except StateBusy:
            time.sleep(5.0)
        except Exception:
            # One bad line must not end the watching. The traceback goes to
            # watcher.log, where `doctor` can find it; the loop goes on, and
            # only gives up when the same thing keeps happening.
            strikes += 1
            traceback.print_exc()
            if strikes >= 20:
                return 1
            time.sleep(5.0)


def _watch_loop(args):
    """Background loop that keeps one job's state fresh.

    Two clocks run here. A cheap liveness check (a signal-0 kill) ticks every
    few seconds so completion is noticed promptly. The actual progress probe -
    the only part that costs anything - runs on the job's own slow cadence from
    poll_interval(): at most once every 2 minutes, and for a long job at most
    once per 5% of its estimated length. The estimate is recomputed after each
    probe, so the cadence stretches or tightens as the estimate does.

    Each pass is three steps: decide what to look at (locked), look at it
    (unlocked, because looking can block), write down what was seen (locked).
    """
    jid = args.job
    idle_since = time.time()
    last_probe = 0.0
    last_state = 0.0
    last_size = None
    interval = float(load_config().get("min_interval_seconds", 120))

    while True:
        now = time.time()
        finished = None

        # ---- decide, under the lock, what this pass should look at
        with state_rw() as st:
            job = st["jobs"].get(jid)
            if job is None or job.get("state") not in ACTIVE_STATES:
                return 0
            job["watcher_pid"] = os.getpid()
            job["watcher_host"] = HOST
            has_pid = bool(job.get("pid"))
            cfg = load_config()
            interval = args.interval or poll_interval(
                job, cfg, estimate(job, now).get("total_est"))
            due_progress = last_probe == 0.0 or (now - last_probe) >= interval
            # Asking the scheduler is cheap but not free, and a job can sit in a
            # queue for days. At most once a minute, and no less often than the
            # progress probe, so completion is noticed promptly either way.
            kind, _job_id = batch_of(job)
            queued = job.get("state") == "queued"
            # A queued job has exactly one signal - the queue - and the moment
            # worth catching is the one where it stops being queued. Asking
            # every minute means a job can read as waiting for a minute after
            # it started, which is the single most visible thing this can get
            # wrong, so a job that has not started is asked about four times as
            # often. It is one `scontrol` call, and nothing else is being done
            # for it: a running job goes back to the slower cadence, because it
            # has a log to read instead.
            gap = 15.0 if queued else 60.0
            due_state = (not has_pid and (bool(job.get("state_probe")) or kind)
                         and (now - last_state) >= min(interval, gap))
            if due_progress:
                last_probe = now
                job["last_probe"] = now
            if due_state:
                last_state = now
            job["next_probe"] = last_probe + interval
            job["interval_s"] = interval
            snapshot = dict(job)

        # ---- look, holding nothing
        # A pid is not proof of life: pids are reused, and a watcher that only
        # asks `is that pid alive` will wait forever on a number that now
        # belongs to something else. Our own jobs write an exit file the moment
        # they finish, which cannot be mistaken for anything, so it is believed
        # first; the pid check is what is left for jobs we merely attached to.
        # Only an exit file this plugin wrote itself. A job attached to a log
        # that already existed - `start --log` - has no exit file of ours, and a
        # stray one beside somebody else's log is not news about the job: reading
        # it as news ended live jobs and took their bars with them.
        own_exit = snapshot.get("exit_file")
        finished_file = bool(own_exit) and os.path.exists(own_exit)
        gone = bool(finished_file) or (has_pid and not job_pid_alive(snapshot))
        # a queued job has produced no output to read; the queue is the only
        # thing worth asking, and it is asked below
        reading, verdict, updates, info = observe(
            snapshot, now, progress=(due_progress or gone) and not queued,
            scheduler=due_state)
        if verdict in ("done", "failed") and not (due_progress or gone):
            # it is over, so take one last look before the bar stops moving
            reading, _v, extra, _i = observe(snapshot, now, progress=True)
            updates.update(extra)

        # ---- write down what was seen
        with state_rw() as st:
            job = st["jobs"].get(jid)
            if job is None or job.get("state") not in ACTIVE_STATES:
                return 0
            job.update(updates)
            if info:
                apply_batch_info(job, info, now)
                if job.get("state") == "running" and queued:
                    idle_since = now      # it has only just begun; give it time
            if reading is not None and apply_reading(job, reading, now):
                idle_since = now
            # Output is life, whether or not any of it parsed as progress. A
            # job watched by time alone, or one printing nothing the patterns
            # know, was being called stalled while its log grew by the second.
            size = _size_of(job.get("log"))
            if size is not None and size != last_size:
                if last_size is not None:
                    idle_since = now
                last_size = size
            if due_progress:
                # a fresh observation means a fresh total estimate
                e = estimate(job, now)
                if e.get("total_est"):
                    job["est_total_s"] = e["total_est"]
                    if not job.get("initial_est_total_s"):
                        job["initial_est_total_s"] = e["total_est"]
                interval = args.interval or poll_interval(job, cfg, job.get("est_total_s"))
                job["next_probe"] = last_probe + interval
                job["interval_s"] = interval

            if verdict in ("done", "failed"):
                job["note"] = "reported by the scheduler"
                finalize(job, 0 if verdict == "done" else 1, now, st)
                finished = dict(job)

            if not finished and gone:
                code = job.get("exit_code")
                wrote_status = False
                try:
                    with open((job.get("log") or "") + ".exit") as f:
                        code = int(f.read().strip())
                    wrote_status = True
                except Exception:
                    pass
                if not wrote_status and job.get("exit_file"):
                    # We started this one, so it should have written its status.
                    # It did not, which means it was killed before it could - a
                    # tool timeout, a reboot, the OOM killer. Calling that "done"
                    # puts a tick against a job that was cut short.
                    code = code if code not in (None, 0) else 137
                    job["note"] = job.get("note") or "killed before it finished"
                finalize(job, code, now, st)
                finished = dict(job)

            # Silence only means something when there is no process to ask.
            # A living pid is the better answer, and plenty of long jobs print
            # nothing for hours. A queued job is silent because it has not
            # started; that is the normal case, not a stall, and a job can sit
            # in a busy queue for longer than max_idle without anything at all
            # being wrong.
            # And silence means nothing at all when there is somebody to ask.
            # A scheduler job, or one with a state probe, is alive for as long
            # as that authority says so - a multi-day run that checkpoints
            # without printing is quiet, not stuck. Calling it stalled ended
            # the record and stopped the watcher, so the ending the scheduler
            # later reported reached nobody.
            if (not finished and not job.get("pid")
                    and job.get("state") != "queued"
                    and not (kind or job.get("state_probe"))
                    and (now - idle_since) > args.max_idle):
                job["note"] = ((job.get("note") or "") + " [no progress seen]").strip()
                job["state"] = "stalled"
                job["ended"] = now
                return 0

        if finished:
            ok = finished.get("state") == "done"
            cfg = load_config()
            dur = fmt_dur((finished.get("ended") or now) - (finished.get("started") or now))
            if ok:
                notify("%s %s" % (cfg["glyph_done"], finished.get("id")),
                       "finished after %s" % dur, True)
            else:
                notify("%s %s crashed" % (cfg["glyph_failed"], finished.get("id")),
                       "%s after %s" % (crash_reason(finished.get("exit_code"))[1], dur), False)
            return 0

        # Liveness ticks stay frequent; progress probes do not. Both branches
        # are capped so the loop comes back to look at the job record itself
        # within a few seconds - otherwise a job with no pid and a long probe
        # interval would leave its watcher asleep for hours after the job had
        # been cancelled or removed. Waking is nearly free; probing is not, and
        # probing stays gated on the interval.
        if has_pid:
            time.sleep(min(15.0, max(1.0, interval / 20.0)))
        else:
            time.sleep(max(1.0, min(15.0, interval,
                                    (last_probe + interval) - time.time())))


# ------------------------------------------------------------------- commands

def check_pattern(pattern):
    """Reject an unusable regex where the user can still see the message."""
    if not pattern:
        return pattern
    try:
        re.compile(pattern)
    except re.error as ex:
        raise SystemExit("--pattern is not a valid regex: %s\n  %s" % (ex, pattern))
    return pattern


def build_monitor(args, existing=None):
    """Turn the monitor flags into the job's monitor spec. Declared once, at
    start; the watcher needs no further instruction."""
    mon = dict(existing or {})
    kind = getattr(args, "monitor", None)

    miles = list(getattr(args, "milestone", None) or [])
    if getattr(args, "milestones", None):
        miles += [m.strip() for m in re.split(r"[;\n]", args.milestones) if m.strip()]
    if miles:
        mon["milestones"] = miles
        kind = kind or "milestones"
    if getattr(args, "glob", None):
        mon["glob"] = args.glob
        kind = kind or "files"
    if getattr(args, "path", None):
        mon["path"] = args.path
        kind = kind or "size"
    if getattr(args, "target_size", None):
        mon["target_bytes"] = parse_size(args.target_size)
        kind = kind or "size"
    if getattr(args, "probe", None):
        mon["cmd"] = args.probe
        kind = kind or "probe"
    if getattr(args, "total", None):
        mon["total"] = args.total
    if kind:
        mon["kind"] = kind
    if not mon:
        return None
    mon.setdefault("kind", "auto")
    return mon


UNIT_BY_MONITOR = {"milestones": "stage", "files": "file", "size": "", "time": ""}


def _new_job(args, cmd=None, log=None, exit_file=None, pid=None):
    now = time.time()
    eta = parse_duration(getattr(args, "eta", None))
    mon = build_monitor(args)
    unit = (getattr(args, "unit", None)
            or UNIT_BY_MONITOR.get((mon or {}).get("kind"), "it"))
    job = {
        "id": None,
        "desc": one_line(getattr(args, "desc", None)),
        "cmd": cmd,
        "log": log, "exit_file": exit_file,
        "pid": pid, "host": HOST,
        "unit": unit,
        "total": getattr(args, "total", None),
        "total_locked": bool(getattr(args, "total", None)),
        "step": None,
        "units": None,
        "pct": None,
        "state": "running",
        "exit_code": None,
        "started": now,
        "updated": now,
        "ended": None,
        "eta_end": (now + eta) if eta else None,
        "eta_prior_s": eta,
        "note": one_line(getattr(args, "note", None)),
        "pattern": check_pattern(getattr(args, "pattern", None)),
        "monitor": mon,
        "state_probe": getattr(args, "state_probe", None),
        "interval_override": parse_duration(getattr(args, "interval", None)),
        "est_total_s": eta,
        "initial_est_total_s": eta,
        "log_offset": 0,
        "force_show": bool(getattr(args, "force_show", False)),
        "auto_launched": bool(getattr(args, "auto_launched", False)),
        "samples": [],
        "session_id": current_session(),
            "bridge_id": current_bridge(),
        "cwd": os.getcwd(),
    }
    return job


def _announce(job):
    cfg = load_config()
    print(render_line(job, cfg))
    e = estimate(job)
    iv = poll_interval(job, cfg, e.get("total_est") or job.get("est_total_s"))
    est = job.get("est_total_s")
    print("  watching: %s   ·   updates every %s%s" % (
        describe_monitor(job), fmt_short(iv),
        " (5%% of the %s estimate)" % fmt_short(est)
        if est and iv > cfg.get("min_interval_seconds", 120) else ""))
    if session_is_new(current_session()) and statusline_wired():
        print("  Note: this session started before agent-progress was loaded, so its")
        print("  statusline was fixed at startup and no bar will appear here.")
        print("  Restart Claude Code to get one; tracking itself works either way,")
        print("  and `agent-progress ls` shows this job now.")
    if job.get("auto_launched"):
        print("  Tracked automatically, and now running detached - this command will not")
        print("  print its output here. To work with it:")
        print("    agent-progress log %s -n 40        read what it has printed so far"
              % job["id"])
        print("    agent-progress ls --json              progress, ETA and state")
        if not job.get("eta_end"):
            print("    agent-progress update %s --eta <duration>   give it an estimate"
                  % job["id"])
            print("  It has no estimate yet. Work out roughly how long it should take and")
            print("  set one - the bar has no ETA until you do.")
    if not job_visible(job, cfg):
        print("  (short job: tracked, but under the %s statusline threshold - "
              "--force-show to pin it)" % fmt_short(cfg["min_duration_seconds"]))
    if job.get("log"):
        print("  log: %s" % job["log"])
    print("  id:  %s   (agent-progress update %s --eta 40m --note '...')" % (job["id"], job["id"]))


def cmd_start(args):
    if args.pid is not None and args.pid <= 1:
        raise SystemExit("pid %s is not a process of yours" % args.pid)
    if args.pid and not alive(args.pid):
        raise SystemExit("pid %s is not running, so there is nothing to track"
                         % args.pid)
    log = os.path.abspath(args.log) if args.log else None
    with state_rw() as st:
        job = _new_job(args, cmd=args.cmd, log=log, pid=args.pid)
        job["id"] = new_id(st, args.name)
        st["jobs"][job["id"]] = job
        jid = job["id"]
    if args.no_watch:
        with state_rw() as st:
            st["jobs"][jid]["no_watch"] = True      # and nothing revives one later
    if (log or args.pid) and not args.no_watch:
        with state_rw() as st:
            st["jobs"][jid]["watcher_pid"] = spawn_watcher(jid)
    with state_rw() as st:
        job = st["jobs"][jid]
    _announce(job)
    return 0


def cmd_run(args):
    cmd_parts = list(args.command)
    if cmd_parts and cmd_parts[0] == "--":
        cmd_parts = cmd_parts[1:]
    if not cmd_parts or not "".join(cmd_parts).strip():
        raise SystemExit("nothing to run: agent-progress run --name train -- python train.py")
    # Re-quote each argument: the caller's shell already tokenized them, so a
    # naive join would break `-c "..."`, paths with spaces, and quoted flags.
    # A single argument is passed through raw, so `run -- "a && b"` still works.
    cmd = (" ".join(shlex.quote(p) for p in cmd_parts)
           if len(cmd_parts) > 1 else cmd_parts[0])
    args.cwd = check_cwd(args.cwd)

    ensure_dirs()
    with state_rw() as st:
        # the same name the wrapper would choose: `python train.py --epochs 10`
        # is `train`, not `10`
        jid = new_id(st, args.name or suggest_job_name(cmd))
        # reserve the id; "running" so a concurrent prune cannot reclaim it
        st["jobs"][jid] = {"id": jid, "state": "running", "started": time.time()}

    log = os.path.abspath(args.log) if args.log else os.path.join(LOGS, "%s.log" % jid)
    exitf = log + ".exit"
    for stale in (log, exitf):
        try:
            os.remove(stale)
        except OSError:
            pass

    # the command must be grouped: without the parentheses, `a && b` redirects
    # only b, and everything a printed is lost instead of captured
    # The newline before the close matters: `pytest  # quick` or a heredoc
    # ending in `EOF` would otherwise take the `)` onto the comment or the
    # terminator line, and the command fails with a syntax error and no output
    wrapper = "( %s\n) > %s 2>&1; echo $? > %s" % (
        cmd, shlex.quote(log), shlex.quote(exitf))
    try:
        proc = subprocess.Popen(
            [USER_SHELL, "-c", wrapper],
            cwd=args.cwd or os.getcwd(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as ex:
        # nothing started, so nothing is running: the reserved record would
        # otherwise sit there as a job that runs forever with no process
        with state_rw() as st:
            if st["jobs"].get(jid, {}).get("pid") is None:
                st["jobs"].pop(jid, None)
        raise SystemExit("could not start the command: %s" % os_error_message(ex))

    with state_rw() as st:
        job = _new_job(args, cmd=cmd, log=log, exit_file=exitf, pid=proc.pid)
        job["id"] = jid
        st["jobs"][jid] = job
    wpid = spawn_watcher(jid)
    with state_rw() as st:
        st["jobs"][jid]["watcher_pid"] = wpid
        job = dict(st["jobs"][jid])
    _announce(job)
    return 0


def _pump(path, offset, stream):
    """Copy new bytes from the log to a stream, so the command's output appears
    as it would have if nothing were wrapping it."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return offset
    if chunk:
        try:
            # Bytes, untouched. Decoding each read on its own turned any
            # multi-byte character that straddled two reads - an accent, an
            # ellipsis, a progress bar's block glyphs - into replacement
            # characters, which is the wrapper changing the output.
            raw = getattr(stream, "buffer", None)
            if raw is not None:
                raw.write(chunk)
            else:
                stream.write(chunk.decode("utf-8", "replace"))
            stream.flush()
        except Exception:
            pass
    return offset + len(chunk)


# ---------------------------------------------------------- interrupt handling
#
# The wrapped command is deliberately started in its own session, so that once
# it outlives the threshold it can be let go of and tracked. Until that moment
# it is still the caller's foreground command - and being in another session
# means nothing sent to the wrapper reaches it. Killing the wrapper (a harness
# timeout, ctrl-c, the user pressing escape) therefore used to leave the real
# work running, unwatched and with no job record, while the session went on
# believing the command had been stopped - and, often, relaunched it. Two
# training runs on the same GPUs is a worse failure than no progress bar.
#
# So signals are forwarded explicitly for exactly as long as the command is
# ours: from the moment it is spawned until it is either finished or handed to
# a watcher. A flag is set in the handler and acted on by the loop rather than
# killing from inside the handler, so no signal arrives in the middle of the
# state file being written.

_INTERRUPT = []
_PREV_HANDLERS = {}
_FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _forward_signals(proc=None):
    """Start recording interrupts. `proc` is unused - the handler only notes the
    signal, so this can be armed before the command exists."""
    def handler(sig, _frame):
        if not _INTERRUPT:
            _INTERRUPT.append(sig)
    for sig in _FORWARDED:
        try:
            _PREV_HANDLERS[sig] = signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            pass          # not the main thread, or the platform has no such signal


def _release_signals():
    """Stop forwarding: the command is finished, or is a tracked job now."""
    for sig, prev in list(_PREV_HANDLERS.items()):
        try:
            signal.signal(sig, prev)
        except (ValueError, OSError, RuntimeError):
            pass
        _PREV_HANDLERS.pop(sig, None)


def _signal_child(proc, sig):
    """Signal the command, process group and all - it has a group of its own."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def _interrupted(proc, log, sent):
    """Pass the interrupt on to the command and report it the way a shell does."""
    sig = _INTERRUPT[0]
    _release_signals()                 # a second ctrl-c must not be swallowed
    _signal_child(proc, sig)
    try:
        proc.wait(timeout=5)
    except Exception:
        _signal_child(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    _pump(log, sent, sys.stdout)       # whatever it managed to print
    return 128 + sig


def check_cwd(path):
    """A missing working directory is a typo, not a crash. Returns the path
    with `~` expanded, which is the form the process has to be started in."""
    if not path:
        return path
    full = os.path.expanduser(path)
    if not os.path.isdir(full):
        raise SystemExit("no such directory: %s" % path)
    return full


def attach_batch_job(kind, job_id, cwd, eta=None, name=None, desc=None,
                     interval=None):
    """Start tracking a job that now belongs to a scheduler.

    Slurm is asked about the job once, here, rather than left until the
    watcher's first pass: it is what decides whether the job starts its life on
    the bar as queued or as running, and where its output will be. Nothing here
    is fatal - a cluster that will not answer just means the job starts queued
    and the watcher finds out later."""
    now = time.time()
    info = slurm_probe(job_id, cwd) if kind == "slurm" else None
    log = slurm_log_path(job_id, cwd, info) if kind == "slurm" else None
    probe = None if kind == "slurm" else (
        STATE_CMDS.get(kind, SLURM_STATE_CMD) % {"id": job_id})
    with state_rw() as st:
        jid = new_id(st, name or "%s-%s" % (kind, job_id))
        st["jobs"][jid] = {
            "id": jid, "desc": desc or "%s job %s" % (kind, job_id), "cmd": None,
            # a scheduler job's output is written by the scheduler; there is no
            # exit file of ours to believe, so its end comes from the queue
            "log": log, "exit_file": None, "pid": None, "unit": "it", "total": None,
            "total_locked": False, "step": None, "units": None, "pct": None,
            # a job that has just been accepted by a queue is, by definition,
            # in the queue; the probe below corrects it if it started at once
            "state": "queued" if kind == "slurm" else "running",
            "exit_code": None, "started": now, "submitted": now, "updated": now,
            "ended": None, "eta_end": (now + eta) if eta else None,
            "eta_prior_s": eta, "note": None, "pattern": None,
            "monitor": {"kind": "auto"}, "interval_override": interval,
            "state_probe": probe, "batch": {"scheduler": kind, "job_id": job_id},
            "est_total_s": eta, "initial_est_total_s": eta, "log_offset": 0,
            "force_show": True, "auto_launched": True, "samples": [],
            "session_id": current_session(),
            "bridge_id": current_bridge(), "cwd": cwd or os.getcwd(),
        }
        if info:
            apply_batch_info(st["jobs"][jid], info, now)
    wpid = spawn_watcher(jid)
    with state_rw() as st:
        st["jobs"][jid]["watcher_pid"] = wpid
    return jid


def _passthrough(command, cwd):
    """Run the command as though this wrapper were never here."""
    try:
        if cwd:
            os.chdir(cwd)
    except OSError:
        pass
    os.execv(USER_SHELL, [USER_SHELL, "-c", command])


def cmd_exec(args):
    """Run a command normally, and start tracking it only if it turns out to be slow.

    This is what keeps the plugin free. A command that finishes inside the
    threshold is untouched: its output is forwarded, its exit code is passed
    through, no job is created, nothing is written, and Claude is told nothing.
    Only once a command has actually proven itself long-running does any of the
    machinery - the bar, the estimate, the reminders - come into existence.
    """
    cfg = load_config()
    args.cwd = check_cwd(args.cwd)
    if args.shell is not None and not args.shell.strip():
        # `( ) > log` is a shell syntax error, which is a puzzling way to
        # report that there was nothing to run
        raise SystemExit("nothing to run")
    after = (parse_duration(args.after) if args.after is not None
             else cfg["auto_track_after_seconds"])
    command = args.shell
    if not command:
        parts = list(args.command)
        if parts and parts[0] == "--":
            parts = parts[1:]
        if not parts:
            raise SystemExit("nothing to run")
        command = (" ".join(shlex.quote(p) for p in parts)
                   if len(parts) > 1 else parts[0])

    # Nothing about tracking may stop the command from running. If the state
    # directory is unwritable - bad permissions, a full disk, a read-only home -
    # hand the command straight to the shell and get out of the way. Anything
    # else would mean a broken plugin breaks every command it wraps.
    try:
        ensure_dirs()
        name = args.name or suggest_job_name(command)
        stamp = "%s-%d" % (slug(name), os.getpid())
        log = os.path.join(LOGS, "%s.log" % stamp)
        exitf = log + ".exit"
        for stale in (log, exitf):
            try:
                os.remove(stale)
            except OSError:
                pass
        open(log, "a").close()
    except (Exception, SystemExit):
        return _passthrough(command, args.cwd)

    # Armed before the child exists, not after. Installing the handlers on the
    # next line after Popen leaves a window - the length of a fork and exec, and
    # on a loaded machine that is not short - in which an interrupt is taken by
    # the default handler: the wrapper dies and the command it just started is
    # left running with nothing watching it. The handler only records the
    # signal, so arming it early costs nothing.
    _forward_signals(None)
    started = time.time()
    proc = subprocess.Popen(
        [USER_SHELL, "-c", "( %s\n) > %s 2>&1; echo $? > %s"
         % (command, shlex.quote(log), shlex.quote(exitf))],
        # No stdin, deliberately. This command may outlive the call that started
        # it, and a detached job holding the session's stdin gets stopped by the
        # kernel the moment it reads. It costs nothing: a command run by the
        # tool this sits in front of is already given a stdin that is at
        # end-of-file, so it sees exactly what it would have seen unwrapped.
        cwd=args.cwd or os.getcwd(), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if _INTERRUPT:
        # it arrived while the child was being started
        code = _interrupted(proc, log, 0)
        _discard_run_files(log, exitf, keep=args.keep_log)
        return code

    sent = 0
    bar_jid = None          # the job registered for this command's own bar
    while True:
        sent = _pump(log, sent, sys.stdout)
        if proc.poll() is not None:
            break
        if _INTERRUPT:
            code = _interrupted(proc, log, sent)
            # The command was cut short - a tool timeout, a ctrl-c - so the
            # caller did not see it end. Close the record now, with the signal
            # as its exit status, rather than leaving the bar running until the
            # watcher finds a dead pid; and report it, since a job that was
            # killed is exactly what they asked always to hear about.
            closed = _close_bar(bar_jid, code, note="killed by %s" % _signame(code - 128),
                                cut_short=True)
            _discard_run_files(log, exitf, keep=args.keep_log or not closed)
            return code
        if after is not None and (time.time() - started) >= after:
            # Long enough to be worth a bar. Register it and keep going: the
            # command is not taken away from the caller, its output is not
            # rerouted, and this call still ends when the command does. Whether
            # to put something in the background is the caller's decision.
            try:
                bar_jid = _register(command, name, log, exitf, proc.pid, started,
                                    cfg, args)
            except Exception:
                pass                 # no bar, then; the command is what matters
            after = None             # once only, whether or not it worked
        time.sleep(0.08)

    _release_signals()
    _pump(log, sent, sys.stdout)          # whatever it wrote on the way out
    code = proc.returncode
    try:
        # bounded: a verbose build can leave hundreds of megabytes here, and
        # the id a scheduler prints is in the first line either way
        with open(log) as f:
            kind, sub_id = detect_submission(f.read(65536), command)
    except Exception:
        kind, sub_id = None, None
    if kind and (proc.returncode in (0, None)):
        # the command did not do the work, it queued it. Follow the queue.
        jid = attach_batch_job(kind, sub_id, args.cwd or os.getcwd(),
                               desc=args.desc)
        job = state_ro()["jobs"].get(jid, {})
        state = job.get("state") or "queued"
        why = describe_queue(job)
        print("\n[agent-progress] %s job %s is %s, and is being tracked as '%s'.%s\n"
              "Progress comes from %s once the scheduler writes it, and the job's\n"
              "state comes from the scheduler itself, so its bar finishes on its own.\n"
              "Do not poll it with squeue - the bar already does, and says why it waits.\n"
              "  agent-progress update %s --eta <duration>   how long you expect it to take"
              % (kind, sub_id, state, jid, ("\n" + why.capitalize() + ".") if why else "",
                 job.get("log") or "the job's output file", jid))
    try:
        with open(exitf) as f:
            code = int(f.read().strip())
    except Exception:
        # no exit file: the wrapper itself was killed. Popen reports that as a
        # negative number, which sys.exit would turn into nonsense (-15 -> 241),
        # so report it the way a shell does.
        pass
    if code is not None and code < 0:
        code = 128 - code

    # It ran to the end in front of the caller, who now has its output and its
    # exit code. Close that record so the bar stops - with the command's own
    # code, which lives in the exit file rather than in the wrapper shell's
    # status - and queue no report: there is nothing to tell them that they
    # have not just read. Deliberately not the scheduler job the block above
    # may have attached: that one is only beginning.
    closed = _close_bar(bar_jid, code)
    # If the state file was too busy to take the close-out, the exit file
    # stays: it is the only proof the command finished the way it did. Delete
    # it and the watcher finds a dead pid with no status, which it can only
    # read as "killed" - a false obituary for a command that succeeded, on a
    # machine that was merely busy. With the file kept, the watcher reads the
    # true code and the sweeper removes the file later.
    _discard_run_files(log, exitf, keep=args.keep_log or not closed)
    return code


def _close_bar(jid, code, note=None, cut_short=False):
    """Close the record behind this command's own bar. True if it was closed
    (or there was none); False if the state file was too busy to take it.

    `cut_short` means the caller did not get to see the command end, so the
    usual "they watched it, tell them nothing" rule does not apply."""
    if not jid:
        return True
    try:
        with state_rw(timeout=2.0) as st:
            job = st["jobs"].get(jid)
            if job is not None and job.get("state") in ACTIVE_STATES:
                if note:
                    job["note"] = note
                if cut_short:
                    job["caller_waits"] = False
                finalize(job, code, time.time(), st if cut_short else None)
        return True
    except Exception:
        return False


def _signame(num):
    try:
        return signal.Signals(num).name
    except (ValueError, AttributeError):
        return "signal %s" % num


def _discard_run_files(log, exitf, keep):
    if keep:
        return
    for f in (log, exitf):
        try:
            os.remove(f)
        except OSError:
            pass


def _register(command, name, log, exitf, pid, started, cfg, args):
    """The command has been going a while: give it a bar, and carry on.

    It is not taken away from the caller. Its output keeps streaming exactly as
    it would have without any of this, and the call ends when the command ends,
    with the command's own exit code. All this adds is a job record and a
    watcher, so a bar can appear in the statusline. Deciding to put something in
    the background is the caller's business, not this plugin's."""
    with state_rw() as st:
        jid = new_id(st, name)
        st["jobs"][jid] = {
            "id": jid, "desc": args.desc, "cmd": command, "log": log,
            "exit_file": exitf, "pid": pid, "host": HOST,
            "unit": "it", "total": None, "total_locked": False, "step": None,
            "units": None, "pct": None, "state": "running", "exit_code": None,
            "started": started, "updated": time.time(), "ended": None,
            "eta_end": None, "eta_prior_s": None, "note": None, "pattern": None,
            "monitor": {"kind": "auto"}, "interval_override": None,
            "est_total_s": None, "initial_est_total_s": None,
            "log_offset": 0, "force_show": False, "auto_launched": True,
            # The caller is sitting in front of this command and will be handed
            # its output and its exit code, so nothing is reported about it
            # later - see finalize. Their pid comes too: if they are killed
            # outright the output goes nowhere, and then a report is the only
            # way the result comes back at all.
            "caller_waits": True,
            "waiter_pid": os.getpid(),
            "samples": [], "session_id": current_session(),
            "bridge_id": current_bridge(),
            "cwd": args.cwd or os.getcwd(),
        }
    wpid = spawn_watcher(jid)
    with state_rw() as st:
        st["jobs"][jid]["watcher_pid"] = wpid
    return jid


def cmd_slurm(args):
    """Follow a job that is already sitting in a queue."""
    jid = attach_batch_job(args.scheduler, args.job_id, args.cwd or os.getcwd(),
                           eta=parse_duration(args.eta), name=args.name, desc=args.desc,
                           interval=parse_duration(getattr(args, "interval", None)))
    with state_rw() as st:
        job = dict(st["jobs"][jid])
    _announce(job)
    print("  state comes from the scheduler, so this bar finishes on its own")
    if job.get("state") == "queued":
        why = describe_queue(job)
        print("  it has not started yet%s - the bar says so, and says why"
              % (": " + why if why else ""))
    return 0


def cmd_update(args):
    with state_rw() as st:
        jid = resolve(st, args.job, mutating=True,
                      any_session=getattr(args, "any_session", False))
        job = st["jobs"][jid]
        now = time.time()
        if args.step is not None:
            job["units"] = float(args.step)
            job["step"] = args.step
            record_sample(job, float(args.step), now)
        if args.total is not None:
            job["total"] = args.total
            job["total_locked"] = True
        if args.pct is not None:
            if not math.isfinite(args.pct):
                raise SystemExit("--pct wants a number between 0 and 100, not %r" % args.pct)
            job["pct"] = max(0.0, min(1.0, args.pct / 100.0))
        if args.eta is not None:
            secs = parse_duration(args.eta)
            if secs is None:
                raise SystemExit("--eta wants a duration, e.g. 45m or 2h30m")
            job["eta_end"] = now + secs
            job["eta_prior_s"] = secs
            # a fresh human/model estimate outranks a stale measured rate
            if args.reset_rate:
                job["samples"] = []
        if args.note is not None:
            job["note"] = one_line(args.note) or None
        if args.desc is not None:
            job["desc"] = one_line(args.desc) or None
        if args.unit is not None:
            job["unit"] = one_line(args.unit, 12)
        if args.pattern is not None:
            job["pattern"] = check_pattern(args.pattern) or None
        mon = build_monitor(args, job.get("monitor"))
        if mon and mon != job.get("monitor"):
            job["monitor"] = mon
            if mon.get("kind") == "milestones":
                job.setdefault("milestones_hit", [])
                job["log_offset"] = 0        # rescan for stages already passed
        if args.interval is not None:
            job["interval_override"] = parse_duration(args.interval)
        if args.force_show:
            job["force_show"] = True
        if getattr(args, "state_probe", None) is not None:
            job["state_probe"] = args.state_probe or None
        job["updated"] = now
        out = dict(job)
    if not args.quiet:
        _announce(out)
    return 0


def cmd_finish(args, state):
    with state_rw() as st:
        jid = resolve(st, args.job, mutating=True,
                      any_session=getattr(args, "any_session", False))
        snapshot = dict(st["jobs"][jid])
    if snapshot.get("state") not in ACTIVE_STATES and snapshot.get("state") != "stalled":
        if snapshot.get("state") == state:
            # saying it twice is harmless, and a note that came with it is kept
            if getattr(args, "note", None):
                with state_rw() as st:
                    if jid in st["jobs"]:
                        st["jobs"][jid]["note"] = one_line(args.note)
                        snapshot = dict(st["jobs"][jid])
            _announce(snapshot)
            return 0
        # but `cancel` on a job that finished must not rewrite how it ended
        raise SystemExit("%s already ended (%s); nothing to mark"
                         % (jid, snapshot.get("state")))
    said = None
    if state == "cancelled":
        # Outside the lock: a cluster can take its time answering, and nothing
        # else should wait on it.
        said = _stop_job(snapshot)
    with state_rw() as st:
        job = st["jobs"].get(jid)
        if job is None:
            raise SystemExit("job %s is gone" % jid)
        code = getattr(args, "exit_code", None)
        finalize(job, 0 if state == "done" else (code if code is not None else 1), time.time())
        job["state"] = state
        if getattr(args, "note", None):
            job["note"] = one_line(args.note)
        elif said:
            job["note"] = said
        out = dict(job)
    _announce(out)
    if said:
        print("  " + said)
    return 0


def _stop_job(job):
    """Make the job stop, whichever kind it is. Returns a line saying what was
    done, or None when there was nothing to do.

    A job with a live pid gets SIGTERM, process group and all. A job that
    belongs to a scheduler has no pid of ours to signal: it is cancelled
    through the scheduler, or not at all. Marking such a record cancelled
    while the job ran on was the bar saying one thing and the cluster doing
    another - an agent telling the user it had stopped a training run that
    was still burning GPU hours. If the scheduler will not take the
    cancellation, the record is left as it is and this fails, so nobody is
    told a job stopped that did not."""
    if job.get("pid") and not pid_here(job):
        raise SystemExit("%s runs on %s (pid %s), and a signal from here would reach some "
                         "other process by that number. Cancel it from that machine, or\n"
                         "  agent-progress rm %s --force   to stop tracking it"
                         % (job.get("id"), job.get("host"), job["pid"], job.get("id")))
    if job.get("pid") and alive(job["pid"]):
        try:
            pgid = os.getpgid(job["pid"])
        except OSError:
            pgid = None
        try:
            # A job started by `run` has a group of its own; a pid merely
            # attached with `start --pid` may share ours, and signalling that
            # group would take this command - and its caller - down with it.
            if pgid is not None and pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(job["pid"], signal.SIGTERM)
        except Exception:
            try:
                os.kill(job["pid"], signal.SIGTERM)
            except Exception:
                pass
        return None
    kind, sched_id = batch_of(job)
    if not kind or not sched_id:
        return None
    cmd = CANCEL_CMDS.get(kind)
    if not cmd:
        return None
    try:
        r = subprocess.run(["/bin/sh", "-c", cmd % {"id": shlex.quote(str(sched_id))}],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise SystemExit("%s did not answer in 30s; %s job %s is still queued or running, "
                         "and the record is unchanged" % (cmd.split()[0], kind, sched_id))
    except OSError as ex:
        raise SystemExit("could not run %s (%s); %s job %s is still queued or running, "
                         "and the record is unchanged"
                         % (cmd.split()[0], os_error_message(ex), kind, sched_id))
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip().splitlines()
        raise SystemExit("%s failed (exit %d)%s; %s job %s is still queued or running, "
                         "and the record is unchanged"
                         % (cmd.split()[0], r.returncode,
                            (": " + why[-1][:160]) if why else "", kind, sched_id))
    return "cancelled through %s (%s job %s)" % (cmd.split()[0], kind, sched_id)


def cmd_ls(args):
    st = state_ro()
    cfg = load_config()
    jobs = sorted(st["jobs"].values(), key=lambda j: j.get("started") or 0)
    if args.running:
        jobs = [j for j in jobs if j.get("state") in ACTIVE_STATES]
    if args.json:
        enriched = []
        for j in jobs:
            e = estimate(j)
            enriched.append({
                "id": j.get("id"), "state": j.get("state"), "desc": j.get("desc"),
                "cmd": j.get("cmd"), "log": j.get("log"), "note": j.get("note"),
                "unit": j.get("unit"), "step": j.get("step"), "total": j.get("total"),
                "percent": round(e["frac"] * 100, 1) if e["frac"] is not None else None,
                "elapsed_s": round(e["elapsed"]), "remaining_s": (
                    round(e["remaining"]) if e["remaining"] is not None else None),
                "elapsed_human": fmt_dur(e["elapsed"]),
                "remaining_human": fmt_short(e["remaining"]),
                "eta_clock": fmt_clock(e["eta_wall"]),
                "eta_source": e["source"], "overdue": e["overdue"],
                "total_estimate_s": round(e["total_est"]) if e.get("total_est") else None,
                "total_estimate_human": fmt_short(e.get("total_est")),
                "initial_estimate_human": fmt_short(j.get("initial_est_total_s")),
                "monitor": describe_monitor(j),
                "monitor_kind": (j.get("monitor") or {}).get("kind", "auto"),
                "update_interval_human": fmt_short(j.get("interval_s")),
                "next_update_in_s": (max(0, round(j["next_probe"] - time.time()))
                                     if j.get("next_probe") else None),
                "rate_per_s": e["rate"], "observations": e["nobs"],
                "exit_code": j.get("exit_code"),
                "progress_source": j.get("progress_source"),
                "session_id": j.get("session_id"),
                "bridge_id": j.get("bridge_id"),
                "mine": job_belongs_here(j),
                "scheduler": (j.get("batch") or {}).get("scheduler"),
                "scheduler_job_id": (j.get("batch") or {}).get("job_id"),
                "scheduler_state": j.get("scheduler_state"),
                "queue_reason": j.get("queue_reason"),
                "queue_reason_human": describe_queue(j) or None,
                "queued_s": (round(j["queued_seconds"])
                             if j.get("queued_seconds") else None),
                "nodes": j.get("nodes"), "partition": j.get("partition"),
            })
        print(json.dumps(enriched, indent=2))
        return 0
    if not jobs:
        print("no tracked jobs. start one:  agent-progress run --name train --eta 2h -- python train.py")
        return 0
    for j in jobs:
        print(render_line(j, cfg))
    return 0


def cmd_show(args):
    st = state_ro()
    jid = resolve(st, args.job)
    job = st["jobs"][jid]
    if args.json:
        out = dict(job)
        out["estimate"] = estimate(job)
        print(json.dumps(out, indent=2, default=str))
        return 0
    cfg = load_config()
    print(render_block(job, cfg, term_width()))
    return 0


def cmd_log(args):
    st = state_ro()
    job = st["jobs"][resolve(st, args.job)]
    log = job.get("log")
    if not log or not os.path.exists(log):
        raise SystemExit("job %s has no log file" % job.get("id"))
    text, _ = read_tail(log, 0, max_bytes=args.bytes)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    print("\n".join(lines[-args.lines:]))
    return 0


def _discard_unless_live(job):
    """A forgotten job's files go with it - unless the job is still running,
    when the wrapper is still streaming that log to its caller and will remove
    it itself on the way out. Unlinking it under the wrapper cut the caller's
    output short."""
    if job.get("state") not in ACTIVE_STATES:
        _discard_auto_files(job)


def cmd_rm(args):
    # `rm --all` inside a session clears that session's work, not the machine's.
    # Several agents share this file, and one of them tidying up should not throw
    # away another's jobs along with the records their watchers are writing to.
    here = current_session()
    scoped = bool(here) and not args.everywhere

    def ours(job):
        return not scoped or job_belongs_here(job, here)

    def live(job):
        """Is there really a process behind this record right now?

        Forgetting a job whose process is still running does not stop the work -
        it strips the work of its watcher and its bar, leaving it running with
        nothing following it and no way to get the tracking back except by
        finding the pid by hand. Records of jobs that have finished, and of jobs
        whose process is already gone, are ordinary rubbish and go freely."""
        pid = job.get("pid")
        return (job.get("state") in ACTIVE_STATES and pid and job_pid_alive(job)) or (
            job.get("state") in ACTIVE_STATES and batch_of(job)[0])

    def removable(job):
        return ours(job) and (args.force or not live(job))

    with state_rw() as st:
        def elsewhere(candidates):
            """How many of these belong to other sessions, and so were left alone.

            Without saying so, `rm` reports "removed 0" and the bars stay on
            screen with no hint of why - which is indistinguishable from the
            command not working."""
            return sum(1 for v in candidates if not ours(v))

        def note(kept_running, kept_others):
            bits = []
            if kept_running:
                bits.append("kept %d still running (--force)" % kept_running)
            if kept_others:
                bits.append("left %d belonging to other sessions (--everywhere)" % kept_others)
            return ("; " + ", ".join(bits)) if bits else ""

        if args.all:
            gone = [k for k, v in st["jobs"].items() if removable(v)]
            kept = sum(1 for k, v in st["jobs"].items() if ours(v) and k not in gone)
            others = elsewhere(st["jobs"].values())
            for k in gone:
                _discard_unless_live(st["jobs"].pop(k))
            print("removed %d job(s)%s%s"
                  % (len(gone), " from this session" if scoped else "",
                     note(kept, others)))
            return 0
        if args.finished:
            done_jobs = [v for v in st["jobs"].values()
                         if v.get("state") not in ACTIVE_STATES]
            gone = [k for k, v in st["jobs"].items()
                    if v.get("state") not in ACTIVE_STATES and ours(v)]
            others = elsewhere(done_jobs)
            for k in gone:
                _discard_auto_files(st["jobs"].pop(k))
            print("removed %d finished job(s)%s" % (len(gone), note(0, others)))
            return 0
        # Every name is looked at before anything is printed. Raising on the
        # second name after printing "removed" for the first left the state
        # unwritten - the first was not removed at all - with output saying it
        # was.
        removed, refused = [], []
        for ref in args.job:
            try:
                jid = resolve(st, ref, mutating=True, any_session=args.everywhere)
            except SystemExit as ex:
                refused.append(str(ex))
                continue
            if jid in removed:
                continue
            if live(st["jobs"][jid]) and not args.force:
                refused.append(
                    "%s is still running (pid %s). Forgetting it now would leave it "
                    "running with nothing watching it.\n"
                    "  agent-progress cancel %s     stop it\n"
                    "  agent-progress rm %s --force  forget it and let it run on"
                    % (jid, st["jobs"][jid].get("pid"), jid, jid))
                continue
            _discard_unless_live(st["jobs"].pop(jid))
            removed.append(jid)
    for jid in removed:
        print("removed %s" % jid)
    for why in refused:
        print(why, file=sys.stderr)
    return 1 if refused else 0


def term_width(default=100):
    try:
        import shutil
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def _complete_json(chunks):
    """The parsed object once the bytes so far form one, else None."""
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", "replace").strip())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else {}


def read_stdin_payload(timeout=3.0):
    """The JSON a host sends on stdin, or {} if it does not arrive in time.

    A bare read() waits for end-of-file, not for the data, so a caller that
    writes nothing - or writes and then holds the pipe open - blocks this
    process for as long as it likes. For the statusline that means no bar and a
    stuck process for every render; for a hook it means every command waiting
    behind it. Read what is there, stop at end-of-file or at the deadline,
    whichever comes first, and treat silence as an empty payload."""
    try:
        if sys.stdin.isatty():
            return {}
        fd = sys.stdin.fileno()
        deadline = time.time() + timeout
        chunks = []
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            ready, _w, _e = select.select([fd], [], [], left)
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break                   # end of file: the writer has finished
            chunks.append(chunk)
            # A complete object is all that was wanted; a writer that keeps the
            # pipe open afterwards should not cost the caller the whole deadline
            done = _complete_json(chunks)
            if done is not None:
                return done
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        payload = json.loads(raw or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def cmd_statusline(args):
    """Entry point wired into settings.json `statusLine`. Reads Claude's JSON on stdin."""
    payload = read_stdin_payload()
    if not isinstance(payload, dict):
        payload = {}          # valid json, wrong shape; the bar still has to draw
    cfg = load_config()
    width = args.width or int(payload.get("terminal_width") or 0) or term_width(120)
    st = state_ro()
    jobs = pick_jobs(st, cfg, payload.get("session_id"))

    lines = []
    for job in jobs[: cfg["max_jobs"]]:
        lines.append(render_line(job, cfg, width=width))
    extra = len(jobs) - cfg["max_jobs"]
    if extra > 0:
        lines.append(paint("  +%d more job(s) · agent-progress ls" % extra, "dim", cfg["color"]))

    if not lines and cfg["show_context_line"]:
        lines.append(context_line(payload, cfg))
    print("\n".join(lines))
    return 0


def context_line(payload, cfg):
    """What the statusline shows when nothing is running - keep the usual context."""
    color = cfg["color"]
    bits = []
    model = (payload.get("model") or {}).get("display_name")
    if model:
        bits.append(model)
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or os.getcwd()
    bits.append(os.path.basename(cwd.rstrip("/")) or cwd)
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
            stderr=subprocess.DEVNULL, timeout=0.4).decode().strip()
        if branch:
            bits.append("⎇ " + branch)
    except Exception:
        pass
    return paint("⏺ " + "  ·  ".join(bits), "dim", color)


def cmd_watch(args):
    """Full-terminal dashboard. Run it in a second pane next to Claude Code."""
    cfg = load_config()
    cfg["bar_width"] = args.bar_width or max(20, min(48, term_width() - 60))
    try:
        while True:
            st = state_ro()
            # a dashboard someone opened deliberately shows everything
            jobs = pick_jobs(st, cfg, apply_visibility=False, scoped=False)
            w = term_width()
            out = ["\033[H\033[J", paint("agent-progress  ·  %s" % time.strftime("%H:%M:%S"), "dim", cfg["color"]), ""]
            if not jobs:
                out.append(paint("  no active jobs", "dim", cfg["color"]))
            for j in jobs:
                out.append(render_block(j, cfg, w))
                out.append("")
            out.append(paint("  ctrl-c to exit", "dim", cfg["color"]))
            sys.stdout.write("\n".join(out) + "\n")
            sys.stdout.flush()
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


def cmd_demo(args):
    """End-to-end smoke test: launch a fake 'training run' and track it for real."""
    if args.tour:
        tour = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "demo", "tour.py")
        if not os.path.exists(tour):
            raise SystemExit("tour not found at %s" % tour)
        return subprocess.call([sys.executable, tour] +
                               (["--speed", str(args.speed)] if args.speed else []))
    ensure_dirs()
    script = os.path.join(ROOT, "_demo_job.py")
    with open(script, "w") as f:
        f.write(
            "import time, sys\n"
            "N=%d\n"
            "for i in range(1, N+1):\n"
            "    time.sleep(%f)\n"
            "    print('Epoch %%d/%%d  loss %%.3f' %% (i, N, 2.5/(i**0.5)), flush=True)\n"
            "print('done')\n" % (args.steps, args.step_seconds)
        )
    class A(object):
        pass
    a = A()
    a.name = "demo"
    a.desc = "simulated training run"
    a.eta = args.eta
    a.total = args.steps
    a.unit = "ep"
    a.log = None
    a.pattern = None
    a.note = "demo job"
    a.cwd = None
    a.interval = args.interval      # demos update fast so there is something to see
    a.force_show = True             # and are short, so pin them past the length floor
    a.command = [sys.executable, script]
    cmd_run(a)
    print("\nA real job updates every %s / 5%% of its estimate; this demo is forced to %s"
          % (fmt_short(load_config()["min_interval_seconds"]), args.interval))
    print("watch it live:   agent-progress watch")
    print("or just look at your Claude Code statusline.")
    return 0


def cmd_inbox(args):
    """Reports waiting to be handed to a session: jobs that ended, well or badly."""
    if args.drain:
        ev = take_crash(current_session())
        if not ev:
            print("nothing waiting to be reported")
            return 0
        print(format_report(ev))
        return 0
    evs = state_ro().get("inbox", [])
    if args.json:
        print(json.dumps(evs, indent=2))
        return 0
    if not evs:
        print("nothing waiting to be reported")
        return 0
    cfg = load_config()
    for e in evs[-args.limit:]:
        # a job that finished is not a crash, and saying "exited with status 0"
        # under a skull is the plugin misreporting its own good news
        finished = e.get("kind") == "done"
        print("%s %-16s %s  %-34s %s" % (
            cfg["glyph_done"] if finished else cfg["glyph_failed"], e.get("job"),
            time.strftime("%m-%d %H:%M", time.localtime(e.get("ts") or 0)),
            "finished" if finished else (e.get("reason") or "ended"),
            "PENDING" if not e.get("delivered") else "delivered"))
    return 0


def cmd_autotrack(args):
    """Explain what auto-tracking would do with a command - the hook's own view."""
    cfg = load_config()
    if not args.command:
        print("Auto-tracking is %s.\n" % cfg["auto_track"])
        print("  defer     run it normally, and track it only if it is still going")
        print("            after %s; one that finishes first costs nothing (default)"
              % fmt_short(cfg["auto_track_after_seconds"]))
        print("  instruct  interrupt it before it starts and ask Claude to relaunch")
        print("            it through agent-progress, with an estimate already chosen")
        print("  off       never intervene\n")
        print("Caught when a command is backgrounded, is given a timeout of %s or\n"
              "more, or matches one of %d built-in patterns.\n"
              % (fmt_short(cfg["auto_track_timeout_seconds"]), len(AUTO_TRACK_PATTERNS)))
        print("  agent-progress autotrack '<command>'    what would happen to this command")
        print("  agent-progress config --set auto_track=off")
        print("  agent-progress config --set auto_track_ignore='^my-script'")
        return 0

    command = " ".join(args.command)
    ti = {}
    if args.background:
        ti["run_in_background"] = True
    if args.timeout:
        ti["timeout"] = int(args.timeout * 1000)
    verdict = classify_command(command, ti, cfg)
    mark = "TRACK" if verdict["track"] else "leave alone"
    print("  %-12s %s" % (mark, command))
    print("  %-12s %s" % ("", verdict["why"]))
    if verdict["track"]:
        print("  %-12s %s" % ("name", verdict["name"]))
        print("  %-12s %s" % ("mode", cfg["auto_track"]))
        if cfg["auto_track"] == "defer":
            print("  %-12s %s" % ("becomes", wrap_command(
                command, verdict["name"], after=cfg["auto_track_after_seconds"])))
            print("  %-12s only tracked if still running after %s"
                  % ("", fmt_short(cfg["auto_track_after_seconds"])))
    return 0


def cmd_monitors(args):
    print("Ways a job's progress can be observed (pick one at start):\n")
    print(MONITOR_HELP)
    cfg = load_config()
    print("Update cadence: every max(%s, %d%% of the estimated total).\n"
          "Override per job with --interval, globally with\n"
          "  agent-progress config --set min_interval_seconds=%d --set interval_fraction=%s"
          % (fmt_short(cfg["min_interval_seconds"]), int(cfg["interval_fraction"] * 100),
             cfg["min_interval_seconds"], cfg["interval_fraction"]))
    return 0


def _fmt_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return repr(v) if (v == "" or v.strip() != v) else v
    return str(v)


def apply_settings(pairs, into, with_help=True):
    """Parse KEY=VALUE arguments into a dict, or explain what was wrong.

    Both `config --set` and `preview --set` take the same arguments and reject
    them the same way; the only difference was whether the complaint quoted the
    setting's help text."""
    for pair in (pairs or []):
        if "=" not in pair:
            raise SystemExit("--set expects KEY=VALUE, got %r" % pair)
        key, value = pair.split("=", 1)
        key = key.strip()
        if key not in CONFIG_SPEC:
            raise SystemExit(_unknown_key(key))
        try:
            into[key] = coerce(key, value)
        except ValueError as ex:
            raise SystemExit("bad value for %s: %s%s"
                             % (key, ex, "\n  " + CONFIG_SPEC[key]["help"]
                                if with_help else ""))
    return bool(pairs)


def cmd_config(args):
    ensure_dirs()
    if args.path:
        print(CONFIG)
        return 0

    def read_user():
        try:
            with open(CONFIG) as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    user = read_user()

    if args.edit:
        if not os.path.exists(CONFIG):
            with open(CONFIG, "w") as f:
                json.dump(user, f, indent=2)
        subprocess.call([os.environ.get("EDITOR", "vi"), CONFIG])
        load_config(force=True)
        return 0

    # Everything from here to the write happens under the lock. Reading the file,
    # changing one key in the copy and writing the whole thing back is a lost
    # update waiting to happen: two sessions doing it at once each keep their own
    # change and silently discard the other's, which is exactly what several
    # agents adjusting settings looks like.
    with hold_lock():
        user = read_user()
        changed = False
        if args.reset:
            user, changed = {}, True
        if args.preset:
            user.update(CONFIG_PRESETS[args.preset])
            changed = True
        for k in (args.unset or []):
            if k not in CONFIG_SPEC:
                raise SystemExit(_unknown_key(k))
            user.pop(k, None)
            changed = True
        changed = apply_settings(args.set, user) or changed

        if changed:
            # written the way the state file is: a reader must never catch this
            # half-done and silently fall back to the defaults
            tmp = CONFIG + ".tmp.%d" % os.getpid()
            with open(tmp, "w") as f:
                json.dump(user, f, indent=2, sort_keys=True)
            os.replace(tmp, CONFIG)
    cfg = load_config(force=True)

    if args.json:
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0

    for group, title in CONFIG_GROUPS:
        keys = sorted(k for k, s in CONFIG_SPEC.items() if s["group"] == group)
        if not keys:
            continue
        print("\n%s" % title)
        for k in keys:
            spec = CONFIG_SPEC[k]
            mine = k in user
            default_hint = ""
            if mine and cfg[k] != spec["default"]:
                default_hint = "  (default %s)" % _fmt_val(spec["default"])
            print("  %s %-26s %-10s %s%s" % (
                "*" if mine else " ", k, _fmt_val(cfg[k]), spec["help"], default_hint))

    print("\n  * = set by you.  File: %s" % CONFIG)
    print("  change:  agent-progress config --set bar_width=30 --set style=tqdm")
    print("  revert:  agent-progress config --unset bar_width   |   --reset")
    print("  presets: %s" % "  ".join("--preset %s" % p for p in sorted(CONFIG_PRESETS)))
    print("  env:     AGENT_PROGRESS_<KEY>=value overrides any key for one command")
    print("  see it:  agent-progress preview")
    return 0


def _unknown_key(k):
    near = difflib.get_close_matches(k, list(CONFIG_SPEC), 3, 0.5)
    msg = "unknown setting %r" % k
    if near:
        msg += " - did you mean %s?" % ", ".join(near)
    return msg + "\nrun `agent-progress config` to see every setting"


def cmd_preview(args):
    """Render sample bars with the current settings, so they can be tuned
    without waiting on a real job."""
    cfg = load_config(force=True)
    apply_settings(args.set, cfg, with_help=False)
    apply_theme(cfg)
    now = time.time()
    samples = [[now - 600 + i * 12, i] for i in range(51)]
    demo = [
        {"id": "training", "state": "running", "started": now - 600, "total": 100,
         "unit": "ep", "units": 50.0, "eta_end": now + 700, "samples": samples,
         "initial_est_total_s": 900, "note": "eval every 5 ep"},
        {"id": "no-counter", "state": "running", "started": now - 900,
         "eta_end": now + 900, "unit": "", "samples": []},
        {"id": "unknown", "state": "running", "started": now - 120, "unit": "", "samples": []},
        {"id": "finished", "state": "done", "started": now - 1400, "ended": now - 5,
         "total": 100, "unit": "ep", "units": 100.0, "samples": samples},
        {"id": "crashed", "state": "failed", "started": now - 700, "ended": now - 5,
         "exit_code": 137, "total": 100, "unit": "ep", "units": 61.0, "samples": samples},
    ]
    print("current settings, rendered:\n")
    for j in demo:
        print("  " + render_line(j, cfg, now=now))
    print("\nstyles:")
    for style in sorted(STYLES):
        c = dict(cfg)
        c["style"] = style
        c["fill_char"] = c["track_char"] = c["left_cap"] = c["right_cap"] = ""
        print("  %-8s %s" % (style, draw_bar(0.56, c, "run")))
    if args.colors:
        print("\n256-color codes for the color_* settings:")
        for row in range(0, 256, 16):
            print("  " + " ".join("\033[38;5;%dm%3d\033[0m" % (c, c)
                                  for c in range(row, min(row + 16, 256))))
    return 0


def cmd_doctor(args):
    ensure_dirs()
    cfg = load_config()
    print("state file : %s (%s)" % (STATE, "exists" if os.path.exists(STATE) else "not created yet"))
    print("logs dir   : %s" % LOGS)
    print("python     : %s" % sys.version.split()[0])
    st = state_ro()
    running = [j for j in st["jobs"].values() if j.get("state") in ACTIVE_STATES]
    queued = [j for j in running if j.get("state") == "queued"]
    mine = [x for x in st["jobs"].values() if job_belongs_here(x)]
    print("jobs       : %d total, %d active (%d queued)"
          % (len(st["jobs"]), len(running), len(queued)))
    if current_session():
        print("             %d of them belong to this session" % len(mine))
    for j in running:
        w = j.get("watcher_pid")
        print("  %-16s watcher=%s%s  pid=%s%s" % (
            j.get("id"), w, "" if watcher_alive(j) else " (DEAD)",
            j.get("pid"), (" (on %s)" % j.get("host")) if not pid_here(j)
            else ("" if alive(j.get("pid")) else " (exited)")))
    settings = os.path.join(HOME, ".claude", "settings.json")
    wired = False
    try:
        with open(settings) as f:
            wired = "agent_progress" in json.dumps(json.load(f).get("statusLine", {}))
    except Exception:
        pass
    print("autotrack  : %s" % cfg["auto_track"])
    sid = current_session()
    if not sid:
        print("session    : not running inside Claude Code")
    elif session_is_new(sid):
        print("session    : %s - started BEFORE the plugin was loaded, so this\n"
              "             session's statusline cannot show a bar. Tracking works;\n"
              "             restart Claude Code for the bar." % sid[:8])
    else:
        print("session    : %s - started with the plugin loaded" % sid[:8])
    print("statusline : %s" % ("wired into settings.json" if wired else
                               "NOT wired - run scripts/install-statusline.sh"))
    print("config     : %s" % json.dumps(cfg))
    return 0


# ----------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="agent-progress",
        description="tqdm-style progress bars for long-running jobs, for Claude Code.")
    sub = p.add_subparsers(dest="cmd")

    def monitor_flags(sp):
        sp.add_argument("--monitor", choices=MONITOR_KINDS,
                        help="how to observe progress (see: agent-progress monitors)")
        sp.add_argument("--milestone", action="append", metavar="TEXT",
                        help="a stage to look for in the log (repeatable, in order)")
        sp.add_argument("--milestones", metavar="A;B;C",
                        help="';'-separated stages, in order")
        sp.add_argument("--glob", metavar="PATTERN",
                        help="files monitor: glob of the outputs being produced")
        sp.add_argument("--path", help="size monitor: file or directory to measure")
        sp.add_argument("--target-size", metavar="SIZE",
                        help="size monitor: expected final size, e.g. 12GB")
        sp.add_argument("--probe", metavar="CMD",
                        help="probe monitor: shell command printing k/N, k or NN%%")
        sp.add_argument("--interval", metavar="DUR",
                        help="override the update cadence, e.g. 5m")

    def common(sp):
        sp.add_argument("--eta", help="estimated time REMAINING, e.g. 45m, 2h30m, 90s")
        sp.add_argument("--total", type=int, help="total steps/epochs, if known")
        sp.add_argument("--unit", default=None, help="unit label, e.g. ep, step, img")
        sp.add_argument("--desc", help="human description of the job")
        sp.add_argument("--note", help="short status note shown on the bar")
        sp.add_argument("--pattern", help="custom regex with (?P<step>) (?P<total>) groups")
        sp.add_argument("--log", help="log file to tail for progress")
        sp.add_argument("--force-show", action="store_true",
                        help="show on the statusline even if it is a short job")
        sp.add_argument("--state-probe", metavar="CMD",
                        help="command printing the job's state, for work running "
                             "elsewhere: COMPLETED/FAILED/RUNNING, or an exit code")

    sp = sub.add_parser("run", help="launch a command detached and track it")
    sp.add_argument("--name", help="short job name (default: derived from the command)")
    sp.add_argument("--cwd", help="working directory for the command")
    sp.add_argument("--auto-launched", action="store_true",
                    help="mark as started by the auto-track hook rather than a person")
    common(sp)
    monitor_flags(sp)
    sp.add_argument("command", nargs=argparse.REMAINDER, help="-- the command to run")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("exec", help="run a command, tracking it only if it proves slow")
    sp.add_argument("--after", help="start tracking after this long (default: config)")
    sp.add_argument("--name", help="job name if it does get tracked")
    sp.add_argument("--shell", help="the command, as one string, run with bash -c (sh if there is no bash)")
    sp.add_argument("--cwd", help="working directory for the command")
    sp.add_argument("--desc", help="human description of the job")
    sp.add_argument("--keep-log", action="store_true",
                    help="keep the capture file even if no job was created")
    sp.add_argument("command", nargs=argparse.REMAINDER,
                    help="-- the command to run, if not given with --shell")
    sp.set_defaults(fn=cmd_exec)

    sp = sub.add_parser("slurm", help="track a job that is already in a scheduler queue")
    sp.add_argument("job_id", help="the scheduler's job id")
    sp.add_argument("--scheduler", default="slurm", choices=sorted(STATE_CMDS),
                    help="which queue the job is in")
    sp.add_argument("--eta", help="how long you expect it to take")
    sp.add_argument("--name", help="short job name (default: the scheduler id)")
    sp.add_argument("--desc", help="human description of the job")
    sp.add_argument("--cwd", help="where the scheduler writes its log")
    sp.add_argument("--interval", metavar="DUR",
                    help="how often to ask the scheduler (default: the cadence policy)")
    sp.set_defaults(fn=cmd_slurm)

    sp = sub.add_parser("start", help="track an already-running job (by pid and/or log)")
    sp.add_argument("name", help="what to call it on the bar")
    sp.add_argument("--pid", type=int, help="pid to watch for completion")
    sp.add_argument("--cmd", help="command string, for display")
    sp.add_argument("--no-watch", action="store_true", help="do not spawn the log watcher")
    common(sp)
    monitor_flags(sp)
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("update", help="revise progress or the ETA (Claude calls this)")
    sp.add_argument("job", help="job id, or enough of one to be unambiguous")
    sp.add_argument("--step", type=int, help="current step or count")
    sp.add_argument("--total", type=int, help="total steps/epochs, if known")
    sp.add_argument("--pct", type=float, help="percent complete, 0-100")
    sp.add_argument("--eta", help="revised time REMAINING from now")
    sp.add_argument("--reset-rate", action="store_true",
                    help="discard measured samples (use after a phase change)")
    sp.add_argument("--note", help="short status note shown on the bar")
    sp.add_argument("--desc", help="human description of the job")
    sp.add_argument("--unit", help="unit label, e.g. ep, step, img")
    sp.add_argument("--pattern", help="custom regex with (?P<step>) (?P<total>) groups")
    sp.add_argument("--quiet", action="store_true", help="change it without printing the bar")
    sp.add_argument("--force-show", action="store_true",
                    help="show on the statusline even if it is a short job")
    sp.add_argument("--any-session", action="store_true",
                    help="allow acting on a job another session started")
    monitor_flags(sp)
    sp.set_defaults(fn=cmd_update)

    for name, state in (("done", "done"), ("fail", "failed"), ("cancel", "cancelled")):
        sp = sub.add_parser(name, help="mark a job %s" % state)
        sp.add_argument("job", help="job id, or enough of one to be unambiguous")
        sp.add_argument("--note", help="a line saying why, kept with the job")
        sp.add_argument("--any-session", action="store_true",
                        help="allow acting on a job another session started")
        if name == "fail":
            sp.add_argument("--exit-code", type=int, default=1,
                            help="the exit code to record")
        sp.set_defaults(fn=(lambda s: (lambda a: cmd_finish(a, s)))(state))

    sp = sub.add_parser("ls", help="list tracked jobs")
    sp.add_argument("--json", action="store_true", help="machine-readable (Claude reads this)")
    sp.add_argument("--running", action="store_true", help="only jobs still going")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("show", help="details for one job")
    sp.add_argument("job", help="job id, or enough of one to be unambiguous")
    sp.add_argument("--json", action="store_true", help="machine-readable")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("log", help="tail a tracked job's log")
    sp.add_argument("job", help="job id, or enough of one to be unambiguous")
    sp.add_argument("-n", "--lines", type=int, default=40, help="how many lines to show")
    sp.add_argument("--bytes", type=int, default=262144,
                    help="how much of the end of the log to read")
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("rm", help="forget jobs")
    sp.add_argument("job", nargs="*", help="jobs to forget; none means use a flag below")
    sp.add_argument("--all", action="store_true", help="every job of this session's")
    sp.add_argument("--finished", action="store_true", help="only jobs that have ended")
    sp.add_argument("--everywhere", action="store_true",
                    help="also remove jobs belonging to other sessions")
    sp.add_argument("--force", action="store_true",
                    help="forget jobs whose process is still running")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("statusline", help="render for the Claude Code statusline")
    sp.add_argument("--width", type=int, help="terminal width to render for")
    sp.set_defaults(fn=cmd_statusline)

    sp = sub.add_parser("watch", help="live dashboard for a second terminal pane")
    sp.add_argument("--interval", type=float, default=1.0, help="seconds between redraws")
    sp.add_argument("--bar-width", type=int, help="bar width to draw")
    sp.add_argument("--once", action="store_true", help="draw once and exit")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("demo", help="run a simulated job end-to-end")
    sp.add_argument("--steps", type=int, default=40, help="steps the simulated job takes")
    sp.add_argument("--step-seconds", type=float, default=1.5,
                    help="seconds per simulated step")
    sp.add_argument("--eta", default="30s", help="deliberately-wrong prior, to show blending")
    sp.add_argument("--interval", default="2s", help="probe cadence for the demo")
    sp.add_argument("--tour", action="store_true",
                    help="the full narrated tour: every monitor kind, plus a crash")
    sp.add_argument("--speed", type=float, help="tour speed multiplier")
    sp.set_defaults(fn=cmd_demo)

    sp = sub.add_parser("config", help="show or change any setting")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="change a setting (repeatable)")
    sp.add_argument("--unset", action="append", metavar="KEY",
                    help="restore one setting to its default")
    sp.add_argument("--preset", choices=sorted(CONFIG_PRESETS),
                    help="apply a bundle of settings")
    sp.add_argument("--reset", action="store_true", help="restore every default")
    sp.add_argument("--json", action="store_true", help="print the effective config as JSON")
    sp.add_argument("--path", action="store_true", help="print the config file path")
    sp.add_argument("--edit", action="store_true", help="open the config file in $EDITOR")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("preview", help="render sample bars with the current settings")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="try a setting without saving it")
    sp.add_argument("--colors", action="store_true", help="also print the 256-color codes")
    sp.set_defaults(fn=cmd_preview)

    sp = sub.add_parser("autotrack", help="explain automatic tracking, or test a command")
    sp.add_argument("command", nargs="*", help="a command to classify")
    sp.add_argument("--background", action="store_true", help="as if run in the background")
    sp.add_argument("--timeout", type=float, help="as if given this timeout, in seconds")
    sp.set_defaults(fn=cmd_autotrack)

    sp = sub.add_parser("inbox", help="crash reports queued for the Claude session")
    sp.add_argument("--drain", action="store_true",
                    help="claim and print the next undelivered report")
    sp.add_argument("--json", action="store_true", help="machine-readable")
    sp.add_argument("--limit", type=int, default=10, help="how many reports to list")
    sp.set_defaults(fn=cmd_inbox)

    sp = sub.add_parser("monitors", help="explain the ways progress can be observed")
    sp.set_defaults(fn=cmd_monitors)

    sp = sub.add_parser("doctor", help="check the install")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("_watch", help=argparse.SUPPRESS)
    sp.add_argument("job")
    sp.add_argument("--interval", type=float, default=None,
                    help="force a fixed probe interval (default: the cadence policy)")
    sp.add_argument("--max-idle", type=float, default=86400.0,
                    help="give up after this long with no sign of life")
    sp.set_defaults(fn=cmd_watch_daemon)

    return p


def os_error_message(ex):
    """What to say when the filesystem refuses.

    A full disk, a read-only directory, a permission that changed underneath -
    all ordinary on a machine that trains models, and none of them a bug in
    this. Say which one, in a sentence, and say that what failed is the
    tracking rather than the work."""
    hint = {
        errno.ENOSPC: "the disk holding %s is full" % ROOT,
        errno.EROFS: "%s is on a read-only filesystem" % ROOT,
        errno.EACCES: "no permission to write to %s" % ROOT,
        errno.EPERM: "no permission to write to %s" % ROOT,
        errno.EDQUOT: "the disk quota for %s is exhausted" % ROOT,
    }.get(getattr(ex, "errno", None))
    return ("agent-progress: %s.\nThe job itself is unaffected - this is the "
            "progress tracking failing, not your work.\n"
            % (hint or "could not write its state (%s)" % ex))


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "fn", None):
        build_parser().print_help()
        return 1
    return args.fn(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        sys.exit(130)
    except StateBusy as ex:
        # a plain sentence, not a traceback: this is a busy file, not a bug
        sys.stderr.write("agent-progress: %s. Nothing was changed.\n" % ex)
        sys.exit(1)
    except OSError as ex:
        sys.stderr.write(os_error_message(ex))
        sys.exit(1)

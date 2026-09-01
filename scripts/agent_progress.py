#!/usr/bin/env python3
"""agent-progress - tqdm-style progress bars for long-running jobs, driven by Claude Code.

Design in one paragraph: a job's ETA starts as Claude's *prior* (a guess made from
reading the training script), and is progressively replaced by a *measured* rate
scraped out of the job's log by a small background watcher. The statusline renderer
blends the two, so the bar is useful from second one and accurate by the end.

No third-party dependencies. Python 3.8+.
"""

from __future__ import print_function

import argparse
import contextlib
import errno
import difflib
import fcntl
import glob as globmod
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import unicodedata

# --------------------------------------------------------------------------- paths

HOME = os.path.expanduser("~")
ROOT = os.environ.get("AGENT_PROGRESS_HOME") or os.path.join(HOME, ".claude", "agent-progress")
STATE = os.path.join(ROOT, "state.json")
LOCK = os.path.join(ROOT, ".lock")
LOGS = os.path.join(ROOT, "logs")
CONFIG = os.path.join(ROOT, "config.json")

STATE_VERSION = 1

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
    "crash_alert": _spec("behavior", True,
                         "interrupt Claude when a job crashes, so it tells you right away", "bool"),
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
        elif t == "int":
            value = int(float(s))
        elif t == "float":
            value = float(s)
        else:
            value = s
    elif t == "bool":
        value = bool(value)
    elif t == "int":
        value = int(value)
    elif t == "float":
        value = float(value)
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
                   "pid", "watcher_pid", "log_offset", "size_bytes")


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
            for key in ("id", "state", "unit", "note", "desc", "cmd", "log", "pattern",
                        "state_probe"):
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
    st["inbox"] = [e for e in st["inbox"] if isinstance(e, dict)]
    for key in ("auto_track_seen", "context_sent"):
        if not isinstance(st.get(key), dict):
            st[key] = {}
    for key in ("sessions",):
        if not isinstance(st.get(key), dict):
            st[key] = {}
    st.setdefault("version", STATE_VERSION)
    return st


def _read_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    return _sanitize(st)


def _write_state(st):
    tmp = STATE + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


@contextlib.contextmanager
def state_rw():
    """Read-modify-write the state file under an exclusive flock."""
    ensure_dirs()
    lf = open(LOCK, "a+")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        st = _read_state()
        yield st
        _prune(st)
        _write_state(st)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


def state_ro():
    """Lock-free read. Rendering runs many times a second; a torn read just
    means one stale frame, which is cheaper than contending on the lock."""
    return _read_state()


def _prune(st):
    cfg = load_config()
    cutoff = time.time() - cfg["prune_after_hours"] * 3600
    for jid in list(st["jobs"]):
        j = st["jobs"][jid]
        if j.get("state") != "running" and (j.get("ended") or 0) < cutoff:
            del st["jobs"][jid]


def current_session():
    """The session this process belongs to.

    Claude Code exports CLAUDE_CODE_SESSION_ID; the shorter name was a guess,
    and reading it meant every job recorded a session of None, which quietly
    disabled putting the current session's jobs first on the statusline."""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or current_session())


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
    # reuse the slot if the previous job of this name is finished
    if st["jobs"][base].get("state") != "running":
        return base
    n = 2
    while "%s-%d" % (base, n) in st["jobs"]:
        n += 1
    return "%s-%d" % (base, n)


def resolve(st, ref):
    """Find a job by exact id, then unique prefix, then substring."""
    jobs = st["jobs"]
    if ref in jobs:
        return ref
    for match in (
        [k for k in jobs if k.startswith(ref)],
        [k for k in jobs if ref.lower() in k.lower()],
    ):
        running = [k for k in match if jobs[k].get("state") == "running"]
        pool = running or match
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:
            raise SystemExit("ambiguous job ref %r: matches %s" % (ref, ", ".join(sorted(pool))))
    raise SystemExit("no such job: %r (try: agent-progress ls)" % ref)


def alive(pid):
    if not pid:
        return False
    try:
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
    ("percent", re.compile(r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%")),
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


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    samples = job.get("samples") or []
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

    if job.get("state") != "running":
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
    if s is None:
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
    return time.strftime(fmt, time.localtime(ts))


def parse_duration(text):
    """'90', '90s', '5m', '2h30m', '1h', '1:30:00' -> seconds."""
    if text is None:
        return None
    text = str(text).strip().lower()
    if not text:
        return None
    if ":" in text:
        parts = [float(p) for p in text.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    total, found = 0.0, False
    for val, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([hms]?)", text):
        if not val:
            continue
        found = True
        mult = {"h": 3600, "m": 60, "s": 1, "": 1}[unit]
        total += float(val) * mult
    if not found:
        raise SystemExit("could not parse duration: %r (try 45m, 2h30m, 90s)" % text)
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
    if st == "stalled":
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

    parts = []
    if cfg["show_spinner"]:
        parts.append(status_glyph(job, cfg))
    if cfg["show_name"]:
        parts.append(paint(job.get("id", "job")[:cfg["name_width"]], "text", color))
    parts.append(draw_bar(e["frac"], cfg, tone))

    if cfg["show_percent"] and e["frac"] is not None:
        parts.append(paint("%3d%%" % int(e["frac"] * 100), "text", color))

    total, units = job.get("total"), job.get("units")
    if cfg["show_counts"] and total and units is not None:
        parts.append(paint("%g/%g%s" % (round(units, 1), total, unit), "dim", color))

    if job.get("state") == "running":
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

    if cfg["show_note"] and job.get("note"):
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
    return text[:limit] if limit else text


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
    return "".join(out) + "…\033[0m"


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
        bits.append("$ " + one_line(job["cmd"], 160))
    bits.append(one_line("watching " + describe_monitor(job), 160))
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
    threshold = cfg["min_duration_seconds"]
    if threshold <= 0:
        return True
    e = estimate(job, now, cfg)
    if e["elapsed"] >= threshold:
        return True
    total = e.get("total_est")
    return total is not None and total >= threshold


def pick_jobs(st, cfg, session_id=None, apply_visibility=True):
    """Jobs worth showing: everything running, plus recently finished ones."""
    now = time.time()
    out = []
    for j in st["jobs"].values():
        if j.get("state") == "running":
            pass
        else:
            linger = (cfg["keep_failed_seconds"] if j.get("state") in ("failed", "stalled")
                      else cfg["keep_done_seconds"])
            if now - (j.get("ended") or 0) >= linger:
                continue
        if apply_visibility and not job_visible(j, cfg, now):
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
    r"^\s*(?:sudo\s+)?(?:\S*/)?agent[-_]progress\b",
    r"\bagent_progress\.py\b",
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
    r"\b(?:npm|pnpm|yarn|cargo|go|docker|terraform|dbt|dvc|bazel)\s+(?:run\s+)?(\w{1,40})",
    r"^\s*(?:sudo\s+)?([\w.-]{1,80})",
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

    for rx in AUTO_TRACK_IGNORE + _split_patterns(cfg["auto_track_ignore"]):
        try:
            if re.search(rx, head):
                result["why"] = "matches an ignore rule"
                return result
        except re.error:
            continue

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
            if re.search(rx, head):
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


def wrap_command(command, name, launcher=None, after=None, background=False):
    """The tracked form of a command.

    The original is passed as a single quoted string, never interpolated raw:
    a command containing `&&`, `|` or a redirect would otherwise be cut in half,
    with the tail applying to the wrapper instead of to the command."""
    launcher = launcher or launcher_prefix()
    if background:
        # already detached by the caller, so there is nothing to wait and see
        return "%s run --name %s --auto-launched -- %s" % (
            launcher, shlex.quote(name), shlex.quote(command))
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
    with state_rw() as st:
        seen = st.setdefault("auto_track_seen", {})
        for k, ts in list(seen.items()):
            if now - ts > ttl:
                del seen[k]
        hit = key in seen
        if remember and not hit:
            seen[key] = now
        return hit


# ------------------------------------------------------------- batch schedulers
#
# A job submitted to a queue has no process here to watch. `sbatch` returns in
# under a second having handed the work to a scheduler, and the run itself
# happens on some other machine for the next several hours. Two things are
# therefore needed that a local job gets for free: somewhere to read progress
# from, which is the file the scheduler writes, and some way to learn that it
# ended, which is the scheduler's own record of it.

# What a submission command prints back, and how to read the id out of it.
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
                 "SPECIAL_EXIT", "REVOKED"}

SLURM_STATE_CMD = (
    's=$(sacct -j %(id)s -n -o State -X 2>/dev/null | head -1); '
    '[ -z "$s" ] && s=$(squeue -j %(id)s -h -o %%T 2>/dev/null | head -1); '
    'echo "$s"')
LSF_STATE_CMD = "bjobs -noheader -o stat %(id)s 2>/dev/null | head -1"
PBS_STATE_CMD = "qstat -x -f %(id)s 2>/dev/null | grep -o 'job_state = .' | head -1"

STATE_CMDS = {"slurm": SLURM_STATE_CMD, "lsf": LSF_STATE_CMD, "pbs": PBS_STATE_CMD}


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


def slurm_log_path(job_id, cwd=None):
    """Where the scheduler will write the job's output."""
    try:
        out = subprocess.check_output(
            ["/bin/sh", "-c", "scontrol show job %s 2>/dev/null" % shlex.quote(job_id)],
            stderr=subprocess.DEVNULL, timeout=10).decode("utf-8", "replace")
        m = re.search(r"StdOut=(\S+)", out)
        if m:
            return m.group(1).replace("%j", job_id)
    except Exception:
        pass
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


def enqueue_crash(st, job, now):
    """Queue a crash for delivery to whichever Claude session asks next."""
    tail = ""
    if job.get("log"):
        text, _ = read_tail(job["log"], 0, 65536)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        tail = "\n".join(lines[-15:])
    short, why = crash_reason(job.get("exit_code"))
    if job.get("scheduler_state"):
        # the scheduler's own word beats an invented exit code
        short = job["scheduler_state"]
        why = "%s, as reported by the scheduler" % job["scheduler_state"]
    st.setdefault("inbox", []).append({
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
        "delivered": None,
    })
    del st["inbox"][:-50]


def take_crash(session_id=None):
    """Claim the oldest undelivered crash. Returns it, or None."""
    claimed = None
    with state_rw() as st:
        for ev in st.get("inbox", []):
            if not ev.get("delivered"):
                ev["delivered"] = {"session_id": session_id, "ts": time.time()}
                claimed = dict(ev)
                break
    return claimed


def pending_crashes():
    return [e for e in state_ro().get("inbox", []) if not e.get("delivered")]


def format_crash(ev, cfg=None):
    """The message a Claude session is handed when a job dies."""
    cfg = cfg or load_config()
    lines = [
        "%s A tracked job CRASHED while you were working: '%s'" % (
            cfg["glyph_failed"], ev.get("job")),
        "  %s after %s" % (ev.get("reason"), fmt_dur(ev.get("duration"))),
    ]
    if ev.get("cmd"):
        cmd = " ".join(ev["cmd"].split())          # collapse multi-line commands
        lines.append("  command: %s" % (cmd[:200] + ("..." if len(cmd) > 200 else "")))
    if ev.get("log"):
        lines.append("  log: %s" % ev["log"])
    if ev.get("log_tail"):
        lines.append("  last output:")
        for ln in ev["log_tail"].splitlines()[-15:]:
            lines.append("    " + ln[:200])
    lines.append("Tell the user this job crashed, summarize why from the output above, "
                 "and suggest a fix if the cause is clear. Do not re-run it without asking.")
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
    m = re.match(r"^\s*([\d.]+)\s*([kmgtp]?)i?b?\s*$", str(text).strip().lower())
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
    if samples and units <= samples[-1][1]:
        return
    samples.append([now, units])
    del samples[:-int(load_config()["rate_window"]) * 3]


def apply_reading(job, reading, now):
    """Fold one parsed log reading into the job's counters."""
    if not reading:
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
    if job["state"] == "failed" and st is not None:
        enqueue_crash(st, job, now)


def cmd_watch_daemon(args):
    """Background loop that keeps one job's state fresh.

    Two clocks run here. A cheap liveness check (a signal-0 kill) ticks every
    few seconds so completion is noticed promptly. The actual progress probe -
    the only part that costs anything - runs on the job's own slow cadence from
    poll_interval(): at most once every 2 minutes, and for a long job at most
    once per 5% of its estimated length. The estimate is recomputed after each
    probe, so the cadence stretches or tightens as the estimate does.
    """
    jid = args.job
    idle_since = time.time()
    last_probe = 0.0
    last_state = 0.0
    interval = float(load_config().get("min_interval_seconds", 120))

    while True:
        now = time.time()
        finished = None
        has_pid = False

        with state_rw() as st:
            job = st["jobs"].get(jid)
            if job is None or job.get("state") != "running":
                return 0
            job["watcher_pid"] = os.getpid()
            has_pid = bool(job.get("pid"))
            cfg = load_config()
            interval = args.interval or poll_interval(
                job, cfg, estimate(job, now).get("total_est"))

            if last_probe == 0.0 or (now - last_probe) >= interval:
                last_probe = now
                job["last_probe"] = now
                try:
                    reading = monitor_reading(job, now)
                except Exception:
                    reading = None            # a broken probe must not kill the bar
                if apply_reading(job, reading, now):
                    idle_since = now
                # a fresh observation means a fresh total estimate
                e = estimate(job, now)
                if e.get("total_est"):
                    job["est_total_s"] = e["total_est"]
                    if not job.get("initial_est_total_s"):
                        job["initial_est_total_s"] = e["total_est"]
                interval = args.interval or poll_interval(job, cfg, job.get("est_total_s"))

            job["next_probe"] = last_probe + interval
            job["interval_s"] = interval

            # Asking the scheduler is cheap but not free, and a job can sit in a
            # queue for days. At most once a minute, and no less often than the
            # progress probe, so completion is noticed promptly either way.
            state_gap = min(interval, 60.0)
            if (not job.get("pid") and job.get("state_probe")
                    and (now - last_state) >= state_gap):
                last_state = now
                verdict = read_state_probe(job)
                if verdict in ("done", "failed"):
                    try:
                        apply_reading(job, monitor_reading(job, now), now)
                    except Exception:
                        pass
                    job["note"] = "reported by the scheduler"
                    finalize(job, 0 if verdict == "done" else 1, now, st)
                    finished = dict(job)

            pid = job.get("pid")
            if not finished and pid and not alive(pid):
                try:
                    apply_reading(job, monitor_reading(job, now), now)   # last look
                except Exception:
                    pass
                code = job.get("exit_code")
                try:
                    with open((job.get("log") or "") + ".exit") as f:
                        code = int(f.read().strip())
                except Exception:
                    pass
                finalize(job, code, now, st)
                finished = dict(job)

            # Silence only means something when there is no process to ask.
            # A living pid is the better answer, and plenty of long jobs print
            # nothing for hours.
            if (not finished and not job.get("pid")
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


def _new_job(args, cmd=None, log=None, pid=None):
    now = time.time()
    eta = parse_duration(getattr(args, "eta", None))
    mon = build_monitor(args)
    unit = (getattr(args, "unit", None)
            or UNIT_BY_MONITOR.get((mon or {}).get("kind"), "it"))
    job = {
        "id": None,
        "desc": one_line(getattr(args, "desc", None)),
        "cmd": cmd,
        "log": log,
        "pid": pid,
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
    if args.pid and not alive(args.pid):
        raise SystemExit("pid %s is not running, so there is nothing to track"
                         % args.pid)
    log = os.path.abspath(args.log) if args.log else None
    with state_rw() as st:
        job = _new_job(args, cmd=args.cmd, log=log, pid=args.pid)
        job["id"] = new_id(st, args.name)
        st["jobs"][job["id"]] = job
        jid = job["id"]
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
    check_cwd(args.cwd)

    ensure_dirs()
    with state_rw() as st:
        jid = new_id(st, args.name or slug(cmd_parts[0] if len(cmd_parts) == 1 else cmd_parts[-1]))
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
    wrapper = "( %s ) > %s 2>&1; echo $? > %s" % (
        cmd, shlex.quote(log), shlex.quote(exitf))
    proc = subprocess.Popen(
        ["/bin/sh", "-c", wrapper],
        cwd=args.cwd or os.getcwd(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    with state_rw() as st:
        job = _new_job(args, cmd=cmd, log=log, pid=proc.pid)
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
            stream.write(chunk.decode("utf-8", "replace"))
            stream.flush()
        except Exception:
            pass
    return offset + len(chunk)


def check_cwd(path):
    """A missing working directory is a typo, not a crash."""
    if path and not os.path.isdir(os.path.expanduser(path)):
        raise SystemExit("no such directory: %s" % path)
    return path


def attach_batch_job(kind, job_id, cwd, eta=None, name=None, desc=None):
    """Start tracking a job that now belongs to a scheduler."""
    now = time.time()
    log = slurm_log_path(job_id, cwd) if kind == "slurm" else None
    probe = STATE_CMDS.get(kind, SLURM_STATE_CMD) % {"id": job_id}
    with state_rw() as st:
        jid = new_id(st, name or "%s-%s" % (kind, job_id))
        st["jobs"][jid] = {
            "id": jid, "desc": desc or "%s job %s" % (kind, job_id), "cmd": None,
            "log": log, "pid": None, "unit": "it", "total": None,
            "total_locked": False, "step": None, "units": None, "pct": None,
            "state": "running", "exit_code": None, "started": now, "updated": now,
            "ended": None, "eta_end": (now + eta) if eta else None,
            "eta_prior_s": eta, "note": None, "pattern": None,
            "monitor": {"kind": "auto"}, "interval_override": None,
            "state_probe": probe, "batch": {"scheduler": kind, "job_id": job_id},
            "est_total_s": eta, "initial_est_total_s": eta, "log_offset": 0,
            "force_show": True, "auto_launched": True, "samples": [],
            "session_id": current_session(), "cwd": cwd or os.getcwd(),
        }
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
    os.execv("/bin/sh", ["/bin/sh", "-c", command])


def cmd_exec(args):
    """Run a command normally, and start tracking it only if it turns out to be slow.

    This is what keeps the plugin free. A command that finishes inside the
    threshold is untouched: its output is forwarded, its exit code is passed
    through, no job is created, nothing is written, and Claude is told nothing.
    Only once a command has actually proven itself long-running does any of the
    machinery - the bar, the estimate, the reminders - come into existence.
    """
    cfg = load_config()
    check_cwd(args.cwd)
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

    started = time.time()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "( %s ) > %s 2>&1; echo $? > %s"
         % (command, shlex.quote(log), shlex.quote(exitf))],
        cwd=args.cwd or os.getcwd(), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    sent = 0
    while True:
        sent = _pump(log, sent, sys.stdout)
        if proc.poll() is not None:
            break
        if after is not None and (time.time() - started) >= after:
            try:
                return _handoff(command, name, log, proc.pid, started, sent, cfg, args)
            except Exception:
                # could not register the job; the command is still running, so
                # keep forwarding it rather than abandoning it
                after = None
        time.sleep(0.08)

    _pump(log, sent, sys.stdout)          # whatever it wrote on the way out
    code = proc.returncode
    try:
        with open(log) as f:
            kind, sub_id = detect_submission(f.read(), command)
    except Exception:
        kind, sub_id = None, None
    if kind and (proc.returncode in (0, None)):
        # the command did not do the work, it queued it. Follow the queue.
        jid = attach_batch_job(kind, sub_id, args.cwd or os.getcwd(),
                               eta=parse_duration(args.after) if False else None,
                               desc=args.desc)
        job = state_ro()["jobs"].get(jid, {})
        print("\n[agent-progress] %s job %s is queued, and is being tracked as '%s'.\n"
              "Progress comes from %s once the scheduler writes it, and the job's\n"
              "state comes from the scheduler itself, so its bar finishes on its own.\n"
              "  agent-progress update %s --eta <duration>   how long you expect it to take"
              % (kind, sub_id, jid, job.get("log") or "the job's output file", jid))
    try:
        with open(exitf) as f:
            code = int(f.read().strip())
    except Exception:
        # no exit file: the wrapper itself was killed. Popen reports that as a
        # negative number, which sys.exit would turn into nonsense (-15 -> 241),
        # so report it the way a shell does.
        if code is not None and code < 0:
            code = 128 - code
    if code is not None and code < 0:
        code = 128 - code
    if not args.keep_log:
        for f in (log, exitf):
            try:
                os.remove(f)
            except OSError:
                pass
    return code


def _handoff(command, name, log, pid, started, sent, cfg, args):
    """The command outlived the threshold: register it and let go of it."""
    with state_rw() as st:
        jid = new_id(st, name)
        st["jobs"][jid] = {
            "id": jid, "desc": args.desc, "cmd": command, "log": log, "pid": pid,
            "unit": "it", "total": None, "total_locked": False, "step": None,
            "units": None, "pct": None, "state": "running", "exit_code": None,
            "started": started, "updated": time.time(), "ended": None,
            "eta_end": None, "eta_prior_s": None, "note": None, "pattern": None,
            "monitor": {"kind": "auto"}, "interval_override": None,
            "est_total_s": None, "initial_est_total_s": None,
            "log_offset": 0, "force_show": False, "auto_launched": True,
            "samples": [], "session_id": current_session(),
            "cwd": args.cwd or os.getcwd(),
        }
    wpid = spawn_watcher(jid)
    with state_rw() as st:
        st["jobs"][jid]["watcher_pid"] = wpid

    floor = fmt_short(cfg["min_duration_seconds"])
    print("\n[agent-progress] Still going after %s, so it is now tracked as '%s' and left\n"
          "running in the background. Its remaining output goes to the log, not here.\n"
          "  agent-progress log %s -n 40      what it has printed\n"
          "  agent-progress ls --json            progress and state\n"
          "If you expect it to run for more than about %s, give it an estimate so the\n"
          "bar can say when it will finish. If not, just carry on - it needs nothing.\n"
          "  agent-progress update %s --eta <duration>"
          % (fmt_short(time.time() - started), jid, jid, floor, jid))
    return 0


def cmd_slurm(args):
    """Follow a job that is already sitting in a queue."""
    jid = attach_batch_job(args.scheduler, args.job_id, args.cwd or os.getcwd(),
                           eta=parse_duration(args.eta), name=args.name, desc=args.desc)
    with state_rw() as st:
        job = dict(st["jobs"][jid])
    _announce(job)
    print("  state comes from the scheduler, so this bar finishes on its own")
    return 0


def cmd_update(args):
    with state_rw() as st:
        jid = resolve(st, args.job)
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
            job["pct"] = max(0.0, min(1.0, args.pct / 100.0))
        if args.eta is not None:
            secs = parse_duration(args.eta)
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
        jid = resolve(st, args.job)
        job = st["jobs"][jid]
        if state == "cancelled" and job.get("pid") and alive(job["pid"]):
            try:
                os.killpg(os.getpgid(job["pid"]), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(job["pid"], signal.SIGTERM)
                except Exception:
                    pass
        code = getattr(args, "exit_code", None)
        finalize(job, 0 if state == "done" else (code if code is not None else 1), time.time())
        job["state"] = state
        if getattr(args, "note", None):
            job["note"] = args.note
        out = dict(job)
    _announce(out)
    return 0


def cmd_ls(args):
    st = state_ro()
    cfg = load_config()
    jobs = sorted(st["jobs"].values(), key=lambda j: j.get("started") or 0)
    if args.running:
        jobs = [j for j in jobs if j.get("state") == "running"]
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


def cmd_rm(args):
    with state_rw() as st:
        if args.all:
            n = len(st["jobs"])
            st["jobs"] = {}
            print("removed %d job(s)" % n)
            return 0
        if args.finished:
            gone = [k for k, v in st["jobs"].items() if v.get("state") != "running"]
            for k in gone:
                del st["jobs"][k]
            print("removed %d finished job(s)" % len(gone))
            return 0
        for ref in args.job:
            jid = resolve(st, ref)
            del st["jobs"][jid]
            print("removed %s" % jid)
    return 0


def term_width(default=100):
    try:
        import shutil
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def cmd_statusline(args):
    """Entry point wired into settings.json `statusLine`. Reads Claude's JSON on stdin."""
    payload = {}
    try:
        if not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
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
            jobs = pick_jobs(st, cfg, apply_visibility=False)
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
    """Crash reports waiting to be handed to a Claude session."""
    if args.drain:
        ev = take_crash(current_session())
        if not ev:
            print("no undelivered crash reports")
            return 0
        print(format_crash(ev))
        return 0
    evs = state_ro().get("inbox", [])
    if args.json:
        print(json.dumps(evs, indent=2))
        return 0
    if not evs:
        print("no crash reports")
        return 0
    cfg = load_config()
    for e in evs[-args.limit:]:
        print("%s %-16s %s  %-34s %s" % (
            cfg["glyph_failed"], e.get("job"),
            time.strftime("%m-%d %H:%M", time.localtime(e.get("ts") or 0)),
            e.get("reason"), "PENDING" if not e.get("delivered") else "delivered"))
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


def cmd_config(args):
    ensure_dirs()
    if args.path:
        print(CONFIG)
        return 0

    user = {}
    try:
        with open(CONFIG) as f:
            user = json.load(f)
    except Exception:
        pass

    if args.edit:
        if not os.path.exists(CONFIG):
            with open(CONFIG, "w") as f:
                json.dump(user, f, indent=2)
        subprocess.call([os.environ.get("EDITOR", "vi"), CONFIG])
        load_config(force=True)
        return 0

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
    for pair in (args.set or []):
        if "=" not in pair:
            raise SystemExit("--set expects KEY=VALUE, got %r" % pair)
        k, v = pair.split("=", 1)
        k = k.strip()
        if k not in CONFIG_SPEC:
            raise SystemExit(_unknown_key(k))
        try:
            user[k] = coerce(k, v)
        except ValueError as ex:
            raise SystemExit("bad value for %s: %s\n  %s" % (k, ex, CONFIG_SPEC[k]["help"]))
        changed = True

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
    for pair in (args.set or []):
        if "=" not in pair:
            raise SystemExit("--set expects KEY=VALUE, got %r" % pair)
        k, v = pair.split("=", 1)
        k = k.strip()
        if k not in CONFIG_SPEC:
            raise SystemExit(_unknown_key(k))
        try:
            cfg[k] = coerce(k, v)
        except ValueError as ex:
            raise SystemExit("bad value for %s: %s" % (k, ex))
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
    running = [j for j in st["jobs"].values() if j.get("state") == "running"]
    print("jobs       : %d total, %d running" % (len(st["jobs"]), len(running)))
    for j in running:
        w = j.get("watcher_pid")
        print("  %-16s watcher=%s%s  pid=%s%s" % (
            j.get("id"), w, "" if alive(w) else " (DEAD)",
            j.get("pid"), "" if alive(j.get("pid")) else " (exited)"))
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
    sp.add_argument("--shell", help="the command, as one string, run with /bin/sh -c")
    sp.add_argument("--cwd")
    sp.add_argument("--desc")
    sp.add_argument("--keep-log", action="store_true",
                    help="keep the capture file even if no job was created")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_exec)

    sp = sub.add_parser("slurm", help="track a job that is already in a scheduler queue")
    sp.add_argument("job_id", help="the scheduler's job id")
    sp.add_argument("--scheduler", default="slurm", choices=sorted(STATE_CMDS))
    sp.add_argument("--eta", help="how long you expect it to take")
    sp.add_argument("--name")
    sp.add_argument("--desc")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_slurm)

    sp = sub.add_parser("start", help="track an already-running job (by pid and/or log)")
    sp.add_argument("name")
    sp.add_argument("--pid", type=int, help="pid to watch for completion")
    sp.add_argument("--cmd", help="command string, for display")
    sp.add_argument("--no-watch", action="store_true", help="do not spawn the log watcher")
    common(sp)
    monitor_flags(sp)
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("update", help="revise progress or the ETA (Claude calls this)")
    sp.add_argument("job")
    sp.add_argument("--step", type=int, help="current step or count")
    sp.add_argument("--total", type=int)
    sp.add_argument("--pct", type=float, help="percent complete, 0-100")
    sp.add_argument("--eta", help="revised time REMAINING from now")
    sp.add_argument("--reset-rate", action="store_true",
                    help="discard measured samples (use after a phase change)")
    sp.add_argument("--note")
    sp.add_argument("--desc")
    sp.add_argument("--unit")
    sp.add_argument("--pattern")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--force-show", action="store_true")
    monitor_flags(sp)
    sp.set_defaults(fn=cmd_update)

    for name, state in (("done", "done"), ("fail", "failed"), ("cancel", "cancelled")):
        sp = sub.add_parser(name, help="mark a job %s" % state)
        sp.add_argument("job")
        sp.add_argument("--note")
        if name == "fail":
            sp.add_argument("--exit-code", type=int, default=1)
        sp.set_defaults(fn=(lambda s: (lambda a: cmd_finish(a, s)))(state))

    sp = sub.add_parser("ls", help="list tracked jobs")
    sp.add_argument("--json", action="store_true", help="machine-readable (Claude reads this)")
    sp.add_argument("--running", action="store_true")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("show", help="details for one job")
    sp.add_argument("job")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("log", help="tail a tracked job's log")
    sp.add_argument("job")
    sp.add_argument("-n", "--lines", type=int, default=40)
    sp.add_argument("--bytes", type=int, default=262144)
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("rm", help="forget jobs")
    sp.add_argument("job", nargs="*")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--finished", action="store_true")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("statusline", help="render for the Claude Code statusline")
    sp.add_argument("--width", type=int)
    sp.set_defaults(fn=cmd_statusline)

    sp = sub.add_parser("watch", help="live dashboard for a second terminal pane")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--bar-width", type=int)
    sp.add_argument("--once", action="store_true")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("demo", help="run a simulated job end-to-end")
    sp.add_argument("--steps", type=int, default=40)
    sp.add_argument("--step-seconds", type=float, default=1.5)
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
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(fn=cmd_inbox)

    sp = sub.add_parser("monitors", help="explain the ways progress can be observed")
    sp.set_defaults(fn=cmd_monitors)

    sp = sub.add_parser("doctor", help="check the install")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("_watch", help=argparse.SUPPRESS)
    sp.add_argument("job")
    sp.add_argument("--interval", type=float, default=None,
                    help="force a fixed probe interval (default: the cadence policy)")
    sp.add_argument("--max-idle", type=float, default=86400.0)
    sp.set_defaults(fn=cmd_watch_daemon)

    return p


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

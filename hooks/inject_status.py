#!/usr/bin/env python3
"""agent-progress hooks.

Two jobs:

1. Keep Claude aware of tracked jobs, cheaply, so it can answer "how's that
   going?" without running anything.
2. Deliver crash reports. Nothing can push a message into a running Claude
   session from outside, so a crash is queued by the watcher and collected here
   at the first opportunity. On Stop - the moment Claude finishes a turn - a
   pending crash blocks the stop once, which makes Claude report it straight
   away instead of waiting for the user to type something.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "scripts", "agent_progress.py")


def load_engine():
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_payload(timeout=3.0):
    """The JSON Claude Code sends on stdin.

    Guarded with select(): a hook is handed its payload on a pipe that is then
    closed, but if it is ever run with stdin left open - by hand, or by a
    harness that forgets - a bare read() would block until the hook is killed.
    Returning an empty payload instead makes the hook a no-op, which is the
    right failure for something that sits in front of every command."""
    try:
        if sys.stdin.isatty():
            return {}
        import os as _os
        import select
        import time as _time
        # select() alone only proves something arrived; read() then waits for
        # end-of-file, so a writer that sends the payload and holds the pipe
        # open still blocks here. Read what is there and stop at the deadline.
        deadline = _time.time() + timeout
        chunks = []
        fd = sys.stdin.fileno()
        while True:
            left = deadline - _time.time()
            if left <= 0:
                break
            ready, _w, _e = select.select([fd], [], [], left)
            if not ready:
                break
            chunk = _os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            try:                        # a complete object is all that was wanted
                got = json.loads(b"".join(chunks).decode("utf-8", "replace").strip())
                return got if isinstance(got, dict) else {}
            except Exception:
                pass
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        payload = json.loads(raw or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def emit(event, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}))


def still_waiting(cc, cfg, session_id):
    """Crashes this session is owed that did not fit in this report."""
    try:
        pending = [e for e in (cc.state_ro().get("inbox") or [])
                   if not e.get("delivered")
                   and (cfg["scope"] == "all"
                        or not cc.know_who_we_are(session_id)
                        or cc.job_belongs_here(e, session_id))]
        return len(pending)
    except Exception:
        return 0


def collect_crashes(cc, cfg, session_id, limit=3):
    """Up to `limit` crash reports, and a line about any that did not fit.

    Reporting three of seven without saying so tells the user three jobs died
    when seven did."""
    out = []
    try:
        if not cc.pending_crashes():
            return out
        while len(out) < limit:
            ev = cc.take_crash(session_id)
            if not ev:
                break
            out.append(cc.format_crash(ev, cfg))
        left = still_waiting(cc, cfg, session_id)
        if out and left:
            out.append("%d more tracked job(s) also crashed and are still queued; "
                       "`agent-progress inbox` lists them, and they will be reported "
                       "as you go." % left)
    except Exception:
        pass
    return out


def job_lines(cc, cfg, session_id=None):
    """One compact line per visible running job."""
    import time
    try:
        st = cc.state_ro()
        running = [j for j in st["jobs"].values()
                   if j.get("state") in cc.ACTIVE_STATES]
    except Exception:
        return [], 0, ""
    if not running:
        return [], 0, ""
    # only this session's work: several agents share one state file, and an
    # agent has no use for another agent's job in its context
    shown = [j for j in running
             if cc.job_visible(j, cfg)
             and (cfg["scope"] == "all"
                  or not cc.know_who_we_are(session_id)
                  or cc.job_belongs_here(j, session_id))]
    hidden = len(running) - len(shown)
    lines = []
    for j in shown[:5]:
        e = cc.estimate(j)
        bits = [j.get("id", "?")]
        if j.get("state") == "queued":
            # the distinction matters to Claude more than to anyone: a queued
            # job has not started, so there is nothing to read and no progress
            # to report, and "it is 0% done after an hour" is actively wrong
            kind = (j.get("batch") or {}).get("scheduler") or "the scheduler"
            bits.append("QUEUED in %s (job %s), not started yet, waited %s"
                        % (kind, (j.get("batch") or {}).get("job_id") or "?",
                           cc.fmt_dur(e["elapsed"])))
            why = cc.describe_queue(j)
            if why:
                bits.append(why)
            if j.get("total") and j.get("step") is not None:
                bits.append("%s/%s tasks finished" % (j["step"], j["total"]))
            lines.append("- " + ", ".join(bits))
            continue
        if j.get("nodes"):
            bits.append("on " + j["nodes"])
        if e["frac"] is not None:
            bits.append("%d%%" % int(e["frac"] * 100))
        if j.get("total") and j.get("step") is not None:
            bits.append("%s/%s %s" % (j["step"], j["total"], j.get("unit") or "it"))
        bits.append("elapsed %s" % cc.fmt_dur(e["elapsed"]))
        if e["remaining"] is not None:
            bits.append("~%s left (finishing %s, estimate from: %s)" % (
                cc.fmt_short(e["remaining"]), cc.fmt_clock(e["eta_wall"]), e["source"]))
        else:
            bits.append("no estimate yet")
        if e.get("total_est"):
            init = j.get("initial_est_total_s")
            drift = (" - first guessed %s" % cc.fmt_short(init)) if init and abs(
                e["total_est"] - init) / float(init) > cfg["drift_threshold"] else ""
            bits.append("est total %s%s" % (cc.fmt_short(e["total_est"]), drift))
        bits.append("watching %s" % cc.describe_monitor(j))
        if j.get("next_probe"):
            bits.append("next update in %s"
                        % cc.fmt_short(max(0, j["next_probe"] - time.time())))
        if j.get("note"):
            bits.append("note: %s" % j["note"])
        if j.get("auto_launched") and not j.get("eta_end"):
            bits.append("TRACKED AUTOMATICALLY, still has no estimate - set one with "
                        "`agent-progress update %s --eta <duration>`" % j.get("id"))
        lines.append("- " + ", ".join(bits))
    # what counts as "the picture changed": which jobs exist, their state, and
    # whether each has an estimate yet. Not progress, which Claude can ask for.
    signature = ";".join(sorted(
        "%s/%s/%s" % (j.get("id"), j.get("state"), bool(j.get("eta_end")))
        for j in shown)) + "|%d" % hidden
    return lines, hidden, signature


def should_send(cc, cfg, session_id, signature):
    """Whether this summary is worth spending context on.

    Job status was previously re-sent on every single prompt, which is a steady
    tax for repeating something Claude already knows. Now it goes out when the
    picture actually changes - a job appears, finishes, or gains an estimate -
    and otherwise no more than once per `context_min_interval_seconds`."""
    import time
    interval = cfg["context_min_interval_seconds"]
    if not interval:
        return True
    key = session_id or "-"
    try:
        with cc.state_rw() as st:
            seen = st.setdefault("context_sent", {})
            now = time.time()
            for k, v in list(seen.items()):
                if now - (v.get("ts") or 0) > 24 * 3600:
                    del seen[k]
            last = seen.get(key) or {}
            if last.get("sig") == signature and (now - (last.get("ts") or 0)) < interval:
                return False
            seen[key] = {"sig": signature, "ts": now}
            return True
    except Exception:
        return True


def count_prompt(cc, cfg, session_id):
    """Tick the "messages since it finished" counter on this session's finished jobs.

    A completed bar is there to be noticed, not to be lived with, and time is a
    poor measure of that: five minutes is many messages if you are working and
    none at all if you stepped away. Counting your messages instead retires it
    when it has actually been seen. Only the session's own jobs, and only while
    they are still on screen - otherwise this would take the lock on every
    prompt for nothing."""
    keep = cfg["keep_done_prompts"]
    if not keep:
        return
    try:
        jobs = cc.state_ro()["jobs"]
        due = [k for k, j in jobs.items()
               if j.get("state") not in cc.ACTIVE_STATES
               and j.get("state") not in ("failed", "stalled")
               and (j.get("prompts_since_done") or 0) < keep
               and (cfg["scope"] == "all"
                    or not cc.know_who_we_are(session_id)
                    or cc.job_belongs_here(j, session_id))]
        if not due:
            return                      # the usual case, and it took no lock
        with cc.state_rw(timeout=1.0) as st:
            for k in due:
                job = st["jobs"].get(k)
                if job is not None:
                    job["prompts_since_done"] = (job.get("prompts_since_done") or 0) + 1
    except Exception:
        pass


def remember_session(cc, session_id):
    """Note that this session began with the plugin already loaded.

    Anything not in this list was already running when the plugin arrived, and
    its statusline - read once, at startup - cannot show a bar."""
    if not session_id:
        return
    try:
        import time as _t
        with cc.state_rw() as st:
            seen = st.setdefault("sessions", {})
            now = _t.time()
            for k, v in list(seen.items()):
                if now - (v or 0) > 30 * 86400:
                    del seen[k]
            seen[session_id] = now
    except Exception:
        pass


def revive_watchers(cc):
    """A watcher killed by a reboot leaves a frozen bar; restart it.

    The liveness check and the spawn have to happen inside the same lock. Two
    sessions starting at once would otherwise both read the same dead pid and
    both spawn, leaving several watchers polling one job - and running its
    probe command once each."""
    try:
        st = cc.state_ro()
        if not any(j.get("state") in cc.ACTIVE_STATES
                   and not cc.alive(j.get("watcher_pid"))
                   for j in st["jobs"].values()):
            return                      # nothing to do, and no need to take the lock
        with cc.state_rw() as w:
            for jid, job in w["jobs"].items():
                if job.get("state") not in cc.ACTIVE_STATES:
                    continue
                if cc.alive(job.get("watcher_pid")):
                    continue            # re-checked under the lock, so only one wins
                job["watcher_pid"] = cc.spawn_watcher(jid)
    except Exception:
        pass


def main():
    # Same ten-second budget as every other hook, and nothing here is worth
    # spending it on: a status summary that cannot be written is a summary
    # that goes out next turn instead.
    os.environ.setdefault("AGENT_PROGRESS_LOCK_TIMEOUT", "3")
    event = sys.argv[1] if len(sys.argv) > 1 else "SessionStart"
    payload = read_payload()
    session_id = payload.get("session_id")

    try:
        cc = load_engine()
        cfg = cc.load_config()
    except Exception:
        return 0

    if event == "Stop":
        # Never block twice in a row - that is how a stop hook becomes a loop.
        if payload.get("stop_hook_active") or not cfg["crash_alert"]:
            return 0
        # Every crash this session is owed, in one block. Taking them one at a
        # time meant a machine that killed four jobs at once - an OOM, a GPU
        # falling over, a node going away - stopped the turn four times in a
        # row, each with a single obituary, when what is wanted is one report
        # saying four things died.
        reports = collect_crashes(cc, cfg, session_id)
        if not reports:
            return 0
        if len(reports) > 1:
            reports.insert(0, "%d tracked jobs crashed. All of them, in order:"
                           % len(reports))
        print(json.dumps({"decision": "block", "reason": "\n\n".join(reports)}))
        return 0

    if event == "SessionStart":
        remember_session(cc, session_id)

    # Also on every prompt, not only when a session starts. A watcher can die -
    # killed, out of memory, the machine rebooted - and reviving them only at
    # session start means that inside a session which stays open for hours,
    # nothing ever brings one back: the job's record stops being updated and its
    # bar sits at whatever it last said, still calling a finished job running.
    # The check in front of this is a lock-free read that answers "no" in
    # microseconds when every watcher is alive, which is almost always.
    revive_watchers(cc)

    if event == "UserPromptSubmit":
        count_prompt(cc, cfg, session_id)

    blocks = collect_crashes(cc, cfg, session_id)
    lines, hidden, signature = job_lines(cc, cfg, session_id)

    # a crash is always worth sending; a routine status update is not
    if not blocks and (lines or hidden) and not should_send(
            cc, cfg, session_id, signature):
        return 0

    parts = list(blocks)
    if lines:
        parts.append(
            "%d tracked job(s) active (agent-progress plugin). These observe themselves "
            "on a timer - do not poll them, re-launch them, or wait on them.\n%s\n"
            "Intervene only if something looks wrong or the user asks: "
            "`agent-progress log <id> -n 40` to read output, then "
            "`agent-progress update <id> --eta <dur> --note '...'` to correct the estimate."
            % (len(lines), "\n".join(lines)))
    elif hidden:
        parts.append("%d short job(s) tracked by the agent-progress plugin, below the "
                     "statusline threshold. Do not re-launch them." % hidden)
    if not parts:
        return 0
    emit(event, "\n\n".join(parts))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # a hook must never break the session

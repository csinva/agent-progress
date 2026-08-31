#!/usr/bin/env python3
"""agent-tqdm hooks.

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
ENGINE = os.path.join(os.path.dirname(HERE), "scripts", "agent_tqdm.py")


def load_engine():
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_tqdm", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_payload():
    try:
        if not sys.stdin.isatty():
            return json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    return {}


def emit(event, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}))


def collect_crashes(cc, cfg, session_id, limit=3):
    out = []
    try:
        if not cc.pending_crashes():
            return out
        while len(out) < limit:
            ev = cc.take_crash(session_id)
            if not ev:
                break
            out.append(cc.format_crash(ev, cfg))
    except Exception:
        pass
    return out


def job_lines(cc, cfg):
    """One compact line per visible running job."""
    import time
    try:
        st = cc.state_ro()
        running = [j for j in st["jobs"].values() if j.get("state") == "running"]
    except Exception:
        return [], 0
    if not running:
        return [], 0
    shown = [j for j in running if cc.job_visible(j, cfg)]
    hidden = len(running) - len(shown)
    lines = []
    for j in shown[:5]:
        e = cc.estimate(j)
        bits = [j.get("id", "?")]
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
        lines.append("- " + ", ".join(bits))
    return lines, hidden


def revive_watchers(cc):
    """A watcher killed by a reboot leaves a frozen bar; restart it."""
    try:
        st = cc.state_ro()
        dead = [j for j in st["jobs"].values()
                if j.get("state") == "running" and not cc.alive(j.get("watcher_pid"))]
        if not dead:
            return
        with cc.state_rw() as w:
            for j in dead:
                jid = j.get("id")
                if jid in w["jobs"] and w["jobs"][jid].get("state") == "running":
                    w["jobs"][jid]["watcher_pid"] = cc.spawn_watcher(jid)
    except Exception:
        pass


def main():
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
        try:
            if not cc.pending_crashes():
                return 0
            ev = cc.take_crash(session_id)
        except Exception:
            return 0
        if not ev:
            return 0
        print(json.dumps({"decision": "block", "reason": cc.format_crash(ev, cfg)}))
        return 0

    if event == "SessionStart":
        revive_watchers(cc)

    blocks = collect_crashes(cc, cfg, session_id)
    lines, hidden = job_lines(cc, cfg)

    parts = list(blocks)
    if lines:
        parts.append(
            "%d tracked job(s) running (agent-tqdm plugin). These observe themselves "
            "on a timer - do not poll them, re-launch them, or wait on them.\n%s\n"
            "Intervene only if something looks wrong or the user asks: "
            "`agent-tqdm log <id> -n 40` to read output, then "
            "`agent-tqdm update <id> --eta <dur> --note '...'` to correct the estimate."
            % (len(lines), "\n".join(lines)))
    elif hidden:
        parts.append("%d short job(s) tracked by the agent-tqdm plugin, below the "
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

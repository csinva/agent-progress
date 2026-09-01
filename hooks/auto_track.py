#!/usr/bin/env python3
"""PreToolUse hook: notice long-running commands as they are launched.

This is what makes tracking automatic. Without it, a job only gets a progress
bar if somebody remembers to ask for one.

Two modes, set by the `auto_track` setting:

  defer     (default) run the command normally, and start tracking it only if
            it is still going after `auto_track_after_seconds`. A command that
            finishes first is untouched and costs nothing at all - no job, no
            message, no tokens.
  instruct  interrupt the command before it starts and tell Claude to relaunch
            it through agent-tqdm, with an estimate and a monitor chosen first.

Any command is interrupted at most once per session, so a command deliberately
re-run untracked is left alone the second time.
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


INSTRUCTIONS = """\
This command looks long-running (%(why)s), so it was stopped once to be tracked
instead. Relaunch it through agent-tqdm, which gives it a live progress bar in
the statusline and tells you when it finishes or crashes:

  agent-tqdm run --name %(name)s --eta <your estimate> <monitor flags> -- %(command)s

Two things to decide first, both covered by the agent-tqdm skill:

- How progress can be observed. Read the command or script: does it print a
  counter, narrate named stages, write output files, grow one file, or expose
  nothing? Pass the matching flags (--milestones, --glob, --path, --probe), or
  nothing at all if it prints a normal counter. `agent-tqdm monitors` lists them.
- Roughly how long it should take. Look for a previous run's logs or outputs,
  the size of the work, then the hardware. Rough is fine - the estimate corrects
  itself. If you have no basis at all, leave --eta off.

Say the estimate and what you are watching, in one line, then continue. If this
job genuinely should not be tracked - it is quick, or you need its output inline
- just run the original command again and it will go through untouched."""


def emit(payload):
    print(json.dumps(payload))


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
        import select
        ready, _w, _e = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return {}
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def main():
    data = read_payload()
    if data.get("tool_name") != "Bash":
        return 0

    tool_input = data.get("tool_input") or {}
    command = (tool_input.get("command") or "").strip()
    if not command:
        return 0

    try:
        cc = load_engine()
        cfg = cc.load_config()
    except Exception:
        return 0            # a broken hook must never block a command

    if cfg["auto_track"] == "off" or os.environ.get("AGENT_TQDM_NO_AUTO"):
        return 0

    try:
        verdict = cc.classify_command(command, tool_input, cfg)
    except Exception:
        return 0
    if not verdict["track"]:
        return 0

    try:
        if cc.auto_seen(command, data.get("session_id")):
            return 0        # already offered once; let it through
    except Exception:
        pass

    if cfg["auto_track"] == "defer":
        background = bool(tool_input.get("run_in_background"))
        wrapped = cc.wrap_command(
            command, verdict["name"],
            after=None if background else cfg["auto_track_after_seconds"],
            background=background)
        updated = dict(tool_input, command=wrapped)
        if background:
            updated["run_in_background"] = False   # agent-tqdm detaches it itself
        emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            # deliberately no permissionDecision: the rewritten command still
            # goes through the normal permission flow, so this cannot be used
            # to slip a command past approval
            "updatedInput": updated,
        }})
        return 0

    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": INSTRUCTIONS % {
            "why": verdict["why"], "name": verdict["name"], "command": command},
    }})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)

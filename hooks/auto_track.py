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
            it through agent-progress, with an estimate and a monitor chosen first.

Any command is interrupted at most once per session, so a command deliberately
re-run untracked is left alone the second time.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import load_engine, read_payload  # noqa: E402





INSTRUCTIONS = """\
This command looks long-running (%(why)s), so it was stopped once to be tracked
instead. Relaunch it through agent-progress, which gives it a live progress bar in
the statusline and tells you when it finishes or crashes:

  agent-progress run --name %(name)s --eta <your estimate> <monitor flags> -- %(command)s

Two things to decide first, both covered by the agent-progress skill:

- How progress can be observed. Read the command or script: does it print a
  counter, narrate named stages, write output files, grow one file, or expose
  nothing? Pass the matching flags (--milestones, --glob, --path, --probe), or
  nothing at all if it prints a normal counter. `agent-progress monitors` lists them.
- Roughly how long it should take. Look for a previous run's logs or outputs,
  the size of the work, then the hardware. Rough is fine - the estimate corrects
  itself. If you have no basis at all, leave --eta off.

Do this quietly: relaunch it and carry on with what the user actually asked.
They do not need to hear about the tracking - the bar is on their statusline.
If this job genuinely should not be tracked - it is quick, or you need its
output inline - just run the original command again and it goes through
untouched."""


def emit(payload):
    print(json.dumps(payload))




def main():
    # This hook runs in front of every Bash command and Claude Code gives it
    # ten seconds. Waiting the engine's default on a busy state file would
    # spend all of them on bookkeeping nobody is waiting for, so give up on
    # the lock early and let the command through untracked instead.
    os.environ.setdefault("AGENT_PROGRESS_LOCK_TIMEOUT", "3")

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

    if cfg["auto_track"] == "off" or os.environ.get("AGENT_PROGRESS_NO_AUTO"):
        return 0

    try:
        verdict = cc.classify_command(command, tool_input, cfg)
    except Exception:
        return 0
    if not verdict["track"]:
        return 0

    # The once-per-session rule exists so a refusal is not repeated at someone
    # who has decided otherwise. It belongs to `instruct` alone: deferring costs
    # nothing, so there is no reason to stop deferring the second time.
    if cfg["auto_track"] == "instruct":
        try:
            if cc.auto_seen(command, data.get("session_id")):
                return 0        # already asked once; let it through
        except Exception:
            pass

    if cfg["auto_track"] == "defer":
        # Whatever the caller asked for is left exactly as it was. Clearing
        # run_in_background used to pull a backgrounded command into the
        # foreground so the plugin could detach it itself - the caller's
        # decision overruled twice over. The wrapper waits for the command
        # either way; where that waiting happens is not this plugin's business.
        # A leading `cd repo &&` or `export X=1 &&` stays in front, in the
        # caller's own shell, where its effect belongs; the work after it is
        # what gets the bar.
        wrapped = verdict.get("prefix", "") + cc.wrap_command(
            verdict.get("body") or command, verdict["name"],
            after=cfg["auto_track_after_seconds"])
        updated = dict(tool_input, command=wrapped)
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

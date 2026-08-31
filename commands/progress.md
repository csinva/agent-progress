---
description: "Show tracked jobs, their progress bars and ETAs; fix a monitor or stop a job"
argument-hint: "[ls | <job> | done <job> | cancel <job>]"
---

# Job progress

Arguments: `$ARGUMENTS`

1. Run `agent-tqdm ls --json` once for the state of every tracked job. (If
   `agent-tqdm` is not on PATH use
   `python3 ~/.claude/skills/agent-tqdm/scripts/agent_tqdm.py`.)
2. If `$ARGUMENTS` names an action (`done X`, `cancel X`, `rm X`), do it.
3. Otherwise report:
   - Run `agent-tqdm ls` and show the rendered bars verbatim in a code block.
   - Give each running job's expected wall-clock finish time.
   - Where `eta_source` is `"claude"`, say the estimate is still a guess with no
     measured throughput behind it.
   - Where `total_estimate_human` differs from `initial_estimate_human`, say how
     the estimate has moved and why, if the log shows why.
4. **Check the monitors are actually working.** For any job showing no movement
   whose `monitor_kind` is not `time`, read `agent-tqdm log <id> -n 40` and
   either fix the monitor (`agent-tqdm update <id> --monitor … --glob …`) or say
   the job appears stalled.
5. If nothing is tracked, say so and offer `/agent-tqdm:track <command>`.

Keep it short: bars, finish times, and anything that looks wrong. Do not poll —
report the state as of now and stop.

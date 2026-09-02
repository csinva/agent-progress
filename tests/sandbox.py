#!/usr/bin/env python3
"""Give the tests a state directory of their own.

The suites clear jobs, reset the config and write deliberate garbage into the
state file, and they used to do all of that to `~/.claude/agent-progress` - the
real one. Running the tests on a machine with work in flight therefore threw
away the user's tracked jobs and their settings, and left the watchers of live
jobs looking for records that no longer existed. It also made the tests
unreliable in the other direction: a real job present on the machine failed the
checks that count how many jobs exist.

Importing this module first points AGENT_PROGRESS_HOME at a fresh temporary
directory. It has to be imported *before* the engine module is loaded, because
the engine reads the variable once at import time, and it is set in os.environ
rather than passed per-call so that the subprocesses the tests spawn - the CLI,
the hooks, the watchers - all inherit it too.
"""

import atexit
import os
import shutil
import tempfile

# Every suite imports this before the engine, so AGENT_PROGRESS_HOME points at
# a fresh directory of this run's own before the engine ever reads it. The
# suites call `rm --all` and `config --reset` freely; none of it can reach a
# real state directory, whatever the shell's environment says.
HOME = tempfile.mkdtemp(prefix="agent-progress-tests-")
os.environ["AGENT_PROGRESS_HOME"] = HOME
atexit.register(shutil.rmtree, HOME, ignore_errors=True)

# The suites start and finish dozens of jobs, and a finished job posts a desktop
# notification. Running the tests should not fill somebody's screen with notices
# about jobs that never existed. This is set in the environment rather than the
# sandbox's config file because the suites call `config --reset` fifteen times
# between them, and a reset would put the notifications back.
os.environ["AGENT_PROGRESS_NOTIFY"] = "false"

STATE = os.path.join(HOME, "state.json")
CONFIG = os.path.join(HOME, "config.json")
LOGS = os.path.join(HOME, "logs")


# A sandbox isolates the state directory, not the process table. Killing or
# counting watchers with `pkill -f agent_progress.py _watch` therefore reaches
# every watcher on the machine - another test run's, and a user's real jobs,
# whose tracking simply stops. TAG makes this run's job names unique so a
# pattern can be narrowed to them, and kill_watchers only signals pids this
# sandbox's own state file knows about.
TAG = "t%d" % os.getpid()


def kill_watchers(cc, jobs=None):
    """Stop the watchers belonging to this sandbox, and nobody else's."""
    import signal
    try:
        records = (cc.state_ro().get("jobs") or {}).values()
    except Exception:
        return
    for job in records:
        if jobs is not None and job.get("id") not in jobs:
            continue
        pid = job.get("watcher_pid")
        if not pid:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            pass

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

HOME = tempfile.mkdtemp(prefix="agent-progress-tests-")
os.environ["AGENT_PROGRESS_HOME"] = HOME
atexit.register(shutil.rmtree, HOME, ignore_errors=True)

STATE = os.path.join(HOME, "state.json")
CONFIG = os.path.join(HOME, "config.json")
LOGS = os.path.join(HOME, "logs")

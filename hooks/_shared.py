#!/usr/bin/env python3
"""The two lines of bootstrap both hooks need, in one place.

Each hook has to find the engine and read its payload before it can do
anything, and both were carrying their own copy of each - including the whole
bounded stdin read, which is fiddly enough that having two of it meant fixing it
twice. The engine is loaded once and remembered, so a hook that asks for the
payload and then asks for the engine pays for one module load, not two.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "scripts", "agent_progress.py")

_engine = None


def load_engine():
    """The engine module, loaded at most once per process."""
    global _engine
    if _engine is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _engine = mod
    return _engine


def read_payload(timeout=3.0):
    """The JSON the host sends on stdin, or {} if it does not arrive.

    The engine owns the bounded read - it needs the same thing for the
    statusline - so there is one implementation of it rather than three. A hook
    that cannot even load the engine has nothing to do anyway, so failing to
    read is the same answer as reading nothing.
    """
    try:
        return load_engine().read_stdin_payload(timeout)
    except Exception:
        return {}

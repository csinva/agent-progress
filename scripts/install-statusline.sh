#!/usr/bin/env bash
# Wire the progress bar into the Claude Code statusline, and put `agent-progress`
# on PATH. Re-runnable. Undo with: install-statusline.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$HERE/agent_progress.py"
SETTINGS="$HOME/.claude/settings.json"
BINDIR="$HOME/.local/bin"
MODE="${1:-install}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "agent-progress needs python3 (3.8 or newer) and none is on PATH." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
  echo "agent-progress needs Python 3.8 or newer; python3 here is $(python3 --version 2>&1)." >&2
  exit 1
fi

PY="$(command -v python3)"
python3 - "$SETTINGS" "$ENGINE" "$MODE" "$PY" <<'PYEOF'
import json, os, shutil, sys, time
settings, engine, mode, py = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

data = {}
if os.path.exists(settings):
    try:
        with open(settings) as f:
            data = json.load(f)
    except ValueError as ex:
        # refuse rather than overwrite: this file is the user's, and a parse
        # error here almost always means they are mid-edit
        sys.stderr.write(
            "%s is not valid JSON (%s).\nFix it and run this again; nothing was "
            "changed.\n" % (settings, ex))
        raise SystemExit(1)
    backup = "%s.bak-%s" % (settings, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(settings, backup)
    print("backed up settings -> %s" % backup)

if mode == "--uninstall":
    sl = data.get("statusLine") or {}
    if "agent_progress" in json.dumps(sl):
        data.pop("statusLine", None)
        print("removed the agent-progress statusLine")
    else:
        print("statusLine was not ours; left alone")
else:
    existing = data.get("statusLine") or {}
    if existing and "agent_progress" not in json.dumps(existing):
        print("NOTE: replacing your existing statusLine:\n  %s" % json.dumps(existing))
        print("      (restore it from the backup above if you want it back)")
    data["statusLine"] = {
        "type": "command",
        # the interpreter by full path: a Claude Code started from a launcher
        # rather than a terminal may not have the same PATH as this shell
        "command": '"%s" "%s" statusline' % (py, engine),
        "padding": 0,
    }
    print("statusLine wired to %s" % engine)

os.makedirs(os.path.dirname(settings), exist_ok=True)
tmp = settings + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, settings)
PYEOF

if [ "$MODE" != "--uninstall" ]; then
  mkdir -p "$BINDIR"
  PY="$(command -v python3 || echo /usr/bin/python3)"
  cat > "$BINDIR/agent-progress" <<EOF
#!/bin/sh
exec "$PY" "$ENGINE" "\$@"
EOF
  chmod +x "$BINDIR/agent-progress"
  echo "shim installed -> $BINDIR/agent-progress"
  case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) echo "NOTE: $BINDIR is not on your PATH, so \`agent-progress\` will not resolve"
       echo "      in Claude's shell. Everything still works meanwhile by full path -"
       echo "      $BINDIR/agent-progress - and Claude is told so at session start."
       echo "      To fix it for good, add this to ~/.zshrc (or ~/.bashrc):"
       echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
       echo "      then open a new terminal before starting Claude Code: a session"
       echo "      inherits PATH from the terminal it was started in." ;;
  esac
else
  rm -f "$BINDIR/agent-progress" && echo "removed $BINDIR/agent-progress"
fi

echo
if [ "$MODE" != "--uninstall" ]; then
  cat <<'MSG'
Sessions already running:
  /reload-plugins   brings in the skills and hooks, so tracking starts working
  restart           needed for the bar itself - a session reads its statusline
                    setting once, when it starts, so already-open sessions
                    cannot show one however they are reloaded
Either way `agent-progress ls` and the desktop notification work immediately, and
a job tracked from an older session says so when it starts.
MSG
else
  echo "Run /reload-plugins in open sessions to drop the hooks."
fi

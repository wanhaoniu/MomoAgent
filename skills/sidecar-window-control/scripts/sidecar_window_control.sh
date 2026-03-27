#!/usr/bin/env bash
set -euo pipefail

mouse_tool="/Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh"

usage() {
  cat <<'EOF'
Usage:
  sidecar_window_control.sh start-server
  sidecar_window_control.sh trust
  sidecar_window_control.sh frame
  sidecar_window_control.sh move-front-window [--process NAME] [--padding PX] [--width-ratio R] [--height-ratio R] [--x-offset PX] [--y-offset PX]
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 64
fi

command_name="$1"
shift

case "$command_name" in
  start-server)
    exec bash "$mouse_tool" start-server
    ;;
  trust)
    exec bash "$mouse_tool" trust
    ;;
  frame)
    exec bash "$mouse_tool" sidecar-frame
    ;;
  move-front-window)
    exec bash "$mouse_tool" sidecar-move-front-window "$@"
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac

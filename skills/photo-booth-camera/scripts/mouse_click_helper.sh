#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script_path="${script_dir}/$(basename "${BASH_SOURCE[0]}")"
app_bundle="${HOME}/Applications/MouseClickHelper.app"
helper="${app_bundle}/Contents/MacOS/MouseClickHelper"
server_url="${MOUSE_CLICK_HELPER_URL:-http://127.0.0.1:47328}"

usage() {
  cat <<'EOF'
Usage:
  mouse_click_helper.sh start-server
  mouse_click_helper.sh health
  mouse_click_helper.sh trust
  mouse_click_helper.sh point [--json]
  mouse_click_helper.sh displays
  mouse_click_helper.sh move --x VALUE --y VALUE
  mouse_click_helper.sh click --x VALUE --y VALUE [--count VALUE]
  mouse_click_helper.sh type --text TEXT
  mouse_click_helper.sh key --key NAME [--modifiers Command,Shift]
  mouse_click_helper.sh open --identifier VALUE
  mouse_click_helper.sh activate --identifier VALUE
  mouse_click_helper.sh sidecar-frame
  mouse_click_helper.sh sidecar-move-front-window [--process NAME] [--padding PX] [--width-ratio R] [--height-ratio R] [--x-offset PX] [--y-offset PX]
EOF
}

ensure_installed() {
  if [[ ! -x "$helper" ]]; then
    cat >&2 <<'EOF'
MouseClickHelper is not installed.

Install it with:
  bash /Users/moce/Documents/Project/SO-ARM-Moce/tools/mouse-click-helper/install_mouse_click_helper.sh
EOF
    exit 1
  fi
}

health_check() {
  curl --noproxy '*' -fsS "${server_url}/health"
}

start_server() {
  ensure_installed
  if pgrep -f "${helper} serve --port 47328" >/dev/null 2>&1; then
    echo "MouseClickHelper server is already running at ${server_url}"
    return
  fi

  osascript -e "tell application \"Terminal\" to do script \"\\\"${helper}\\\" serve --port 47328\" " >/dev/null
  for _ in {1..25}; do
    if curl --noproxy '*' -fsS "${server_url}/health" >/dev/null 2>&1; then
      echo "MouseClickHelper server is ready at ${server_url}"
      return
    fi
    sleep 0.2
  done

  cat >&2 <<EOF
MouseClickHelper server did not start.

Try opening ${app_bundle} manually once, then run:
  bash ${script_path} trust
EOF
  exit 1
}

ensure_server() {
  if ! curl --noproxy '*' -fsS "${server_url}/health" >/dev/null 2>&1; then
    start_server >/dev/null
  fi
}

api_get() {
  local path="$1"
  ensure_server
  curl --noproxy '*' -fsS "${server_url}${path}"
}

api_post() {
  local path="$1"
  local payload="$2"
  ensure_server
  curl --noproxy '*' -fsS -X POST "${server_url}${path}" \
    -H 'Content-Type: application/json' \
    -d "$payload"
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 64
fi

command_name="$1"
shift

case "$command_name" in
  start-server)
    start_server
    ;;

  health)
    health_check
    ;;

  trust)
    ensure_installed
    exec "$helper" trust --prompt
    ;;

  point)
    response="$(api_get /point)"
    if [[ "${1-}" == "--json" ]]; then
      python3 - "$response" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
print(json.dumps(payload["point"], ensure_ascii=False))
PY
    else
      python3 - "$response" <<'PY'
import json, sys
point = json.loads(sys.argv[1])["point"]
print(f'{point["x"]:.2f} {point["y"]:.2f}')
PY
    fi
    ;;

  displays)
    response="$(api_get /displays)"
    python3 - "$response" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
for display in payload["displays"]:
    tags = [display["role"]]
    if display.get("cursor"):
        tags.append("cursor")
    print(
        f'{".".join(tags).replace(".", ",")}\tid={display["id"]}\t'
        f'x={display["x"]:.2f}\ty={display["y"]:.2f}\t'
        f'w={display["width"]:.2f}\th={display["height"]:.2f}'
    )
PY
    ;;

  move)
    x=""
    y=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --x) x="${2-}"; shift 2 ;;
        --y) y="${2-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 64 ;;
      esac
    done
    payload="$(python3 - "$x" "$y" <<'PY'
import json, sys
print(json.dumps({"x": float(sys.argv[1]), "y": float(sys.argv[2])}))
PY
)"
    api_post /move "$payload"
    ;;

  click)
    x=""
    y=""
    count="1"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --x) x="${2-}"; shift 2 ;;
        --y) y="${2-}"; shift 2 ;;
        --count) count="${2-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 64 ;;
      esac
    done
    payload="$(python3 - "$x" "$y" "$count" <<'PY'
import json, sys
print(json.dumps({"x": float(sys.argv[1]), "y": float(sys.argv[2]), "count": int(sys.argv[3])}))
PY
)"
    api_post /click "$payload"
    ;;

  type)
    text=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --text) text="${2-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 64 ;;
      esac
    done
    payload="$(python3 - "$text" <<'PY'
import json, sys
print(json.dumps({"text": sys.argv[1]}, ensure_ascii=False))
PY
)"
    api_post /type "$payload"
    ;;

  key)
    key_name=""
    modifiers=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --key) key_name="${2-}"; shift 2 ;;
        --modifiers) modifiers="${2-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 64 ;;
      esac
    done
    payload="$(python3 - "$key_name" "$modifiers" <<'PY'
import json, sys
payload = {"key": sys.argv[1]}
if sys.argv[2]:
    payload["modifiers"] = sys.argv[2]
print(json.dumps(payload, ensure_ascii=False))
PY
)"
    api_post /key "$payload"
    ;;

  open|activate)
    identifier=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --identifier) identifier="${2-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 64 ;;
      esac
    done
    payload="$(python3 - "$identifier" <<'PY'
import json, sys
print(json.dumps({"identifier": sys.argv[1]}, ensure_ascii=False))
PY
)"
    api_post "/${command_name}" "$payload"
    ;;

  sidecar-frame)
    api_get /sidecar/frame
    ;;

  sidecar-move-front-window)
    process_name=""
    padding=""
    width_ratio=""
    height_ratio=""
    x_offset=""
    y_offset=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --process) process_name="${2-}"; shift 2 ;;
        --padding) padding="${2-}"; shift 2 ;;
        --width-ratio) width_ratio="${2-}"; shift 2 ;;
        --height-ratio) height_ratio="${2-}"; shift 2 ;;
        --x-offset) x_offset="${2-}"; shift 2 ;;
        --y-offset) y_offset="${2-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 64 ;;
      esac
    done
    payload="$(python3 - "$process_name" "$padding" "$width_ratio" "$height_ratio" "$x_offset" "$y_offset" <<'PY'
import json, sys
payload = {}
if sys.argv[1]:
    payload["process"] = sys.argv[1]
if sys.argv[2]:
    payload["padding"] = float(sys.argv[2])
if sys.argv[3]:
    payload["widthRatio"] = float(sys.argv[3])
if sys.argv[4]:
    payload["heightRatio"] = float(sys.argv[4])
if sys.argv[5]:
    payload["xOffset"] = float(sys.argv[5])
if sys.argv[6]:
    payload["yOffset"] = float(sys.argv[6])
print(json.dumps(payload, ensure_ascii=False))
PY
)"
    api_post /sidecar/move-front-window "$payload"
    ;;

  *)
    usage >&2
    exit 64
    ;;
esac

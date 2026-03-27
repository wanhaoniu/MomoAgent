#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skills_dir="$(cd "$script_dir/../.." && pwd)"
macos_use="${PHOTO_BOOTH_MACOS_USE_CONTROL_SCRIPT:-$skills_dir/macos-use-desktop-control/scripts/macos_use_control.py}"
latest_photo_script="${PHOTO_BOOTH_LATEST_PHOTO_SCRIPT:-$skills_dir/photo-booth-camera/scripts/photo-booth-latest-photo.sh}"

before_shot_delay="0"
save_timeout="15"
reveal_result="0"

usage() {
  cat <<'EOF'
Usage: photo_booth_take_photo_macos_use_direct.sh [options]

Open Photo Booth, switch back to the live preview when needed, trigger one still
photo through macos-use, and print the saved photo path.

Options:
  --before-shot-delay SEC   Wait before pressing the shutter (default: 0)
  --save-timeout SEC        Wait this long for a new saved photo (default: 15)
  --reveal                  Reveal the saved photo in Finder
  -h, --help                Show this help text
EOF
}

require_value() {
  local flag="$1"
  local value="${2-}"
  if [[ -z "$value" ]]; then
    echo "Missing value for $flag" >&2
    exit 64
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --before-shot-delay)
      require_value "$1" "${2-}"
      before_shot_delay="$2"
      shift 2
      ;;
    --save-timeout)
      require_value "$1" "${2-}"
      save_timeout="$2"
      shift 2
      ;;
    --reveal)
      reveal_result="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

if [[ ! -x "$macos_use" ]]; then
  echo "macos-use control script not found: $macos_use" >&2
  exit 1
fi

if [[ ! -x "$latest_photo_script" ]]; then
  echo "Photo Booth latest-photo helper not found: $latest_photo_script" >&2
  exit 1
fi

latest_photo_path() {
  bash "$latest_photo_script" "$@"
}

json_get() {
  local json_payload="$1"
  local key_path="$2"
  python3 - "$json_payload" "$key_path" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
path = sys.argv[2].split(".")
value = payload
for key in path:
    if key:
        value = value[key]
if value is None:
    print("")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
PY
}

file_contains() {
  local file_path="$1"
  local needle="$2"
  [[ -f "$file_path" ]] && grep -Fq "$needle" "$file_path"
}

wait_for_new_photo() {
  local baseline="$1"
  local timeout_seconds="$2"
  local deadline
  deadline=$(( $(date +%s) + timeout_seconds ))

  while (( $(date +%s) <= deadline )); do
    local current
    if current="$(latest_photo_path 2>/dev/null)"; then
      if [[ -n "$current" && "$current" != "$baseline" ]]; then
        printf '%s\n' "$current"
        return 0
      fi
    fi
    sleep 1
  done

  return 1
}

baseline_photo="$(latest_photo_path 2>/dev/null || true)"

open_json="$(python3 "$macos_use" --json open --app com.apple.PhotoBooth)"
open_status="$(json_get "$open_json" status)"
if [[ "$open_status" != "success" ]]; then
  echo "$open_json" >&2
  exit 1
fi

pid="$(json_get "$open_json" pid)"
traversal_file="$(json_get "$open_json" file)"

if file_contains "$traversal_file" '"查看视频预览"'; then
  python3 "$macos_use" click-text --pid "$pid" --text "查看视频预览" >/dev/null
  sleep 0.5
  refresh_json="$(python3 "$macos_use" --json refresh --pid "$pid")"
  traversal_file="$(json_get "$refresh_json" file)"
else
  refresh_json="$open_json"
fi

if ! file_contains "$traversal_file" '"拍照"'; then
  if file_contains "$traversal_file" '"照片 拍照"'; then
    python3 "$macos_use" click-text --pid "$pid" --text "照片 拍照" >/dev/null
    sleep 0.4
    refresh_json="$(python3 "$macos_use" --json refresh --pid "$pid")"
    traversal_file="$(json_get "$refresh_json" file)"
  fi
fi

if ! file_contains "$traversal_file" '"拍照"'; then
  echo "Could not find a Photo Booth shutter button in $traversal_file" >&2
  echo "$refresh_json" >&2
  exit 1
fi

if [[ "$before_shot_delay" != "0" ]]; then
  sleep "$before_shot_delay"
fi

python3 "$macos_use" click-text --pid "$pid" --text "拍照" >/dev/null

if new_photo="$(wait_for_new_photo "$baseline_photo" "$save_timeout")"; then
  if [[ "$reveal_result" == "1" ]]; then
    latest_photo_path --reveal >/dev/null
  fi
  echo "$new_photo"
  exit 0
fi

echo "Photo Booth did not save a new photo within ${save_timeout}s." >&2
if [[ -n "$baseline_photo" ]]; then
  echo "Latest known photo before trigger: $baseline_photo" >&2
fi
exit 1

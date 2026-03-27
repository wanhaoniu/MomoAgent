#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script_path="$script_dir/$(basename "${BASH_SOURCE[0]}")"
skills_dir="$(cd "$script_dir/../.." && pwd)"

macos_use="${PHOTO_BOOTH_MACOS_USE_CONTROL_SCRIPT:-${DJI_SHOW_DEMO_MACOS_USE_CONTROL_SCRIPT:-$skills_dir/macos-use-desktop-control/scripts/macos_use_control.py}}"
library_dir="${DJI_SHOW_DEMO_PHOTO_BOOTH_LIBRARY_DIR:-$HOME/Pictures/Photo Booth Library}"
recents_plist="$library_dir/Recents.plist"
pictures_dir="$library_dir/Pictures"
originals_dir="$library_dir/Originals"

mode=""
record_start_settle_s="1.0"
save_timeout="30"
baseline_recent_name=""
baseline_recent_count="0"
reveal_result="0"
button_bounds=""

usage() {
  cat <<'EOF'
Usage:
  photo-booth-record-video.sh start [--record-start-settle-s SEC]
  photo-booth-record-video.sh stop [--baseline-recent-name NAME] [--baseline-recent-count N] [--video-save-timeout SEC] [--button-bounds X,Y,W,H] [--reveal]

This wrapper launches a Terminal child-process so the real Photo Booth video
automation can inherit Terminal's permissions. Successful output is always one
prefixed line:

  BASELINE_RECENT_NAME<TAB>...
  VIDEO_PATH<TAB>/tmp/...

Internal direct modes:
  --direct-start
  --direct-stop
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

integer_seconds() {
  python3 - "$1" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except Exception:
    raise SystemExit(64)

print(max(0, int(math.ceil(value))))
PY
}

json_get() {
  local json_payload="$1"
  local key_path="$2"
  python3 - "$json_payload" "$key_path" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
path = [part for part in sys.argv[2].split(".") if part]
value = payload
for key in path:
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

file_contains_any() {
  local file_path="$1"
  shift
  local needle
  for needle in "$@"; do
    if file_contains "$file_path" "$needle"; then
      return 0
    fi
  done
  return 1
}

visible_bounds_by_text() {
  local traversal_file="$1"
  local role_hint="$2"
  local exact_text="$3"
  python3 - "$traversal_file" "$role_hint" "$exact_text" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
role_hint = sys.argv[2]
exact_text = sys.argv[3]
if not path.exists():
    print("")
    raise SystemExit(0)

pattern = re.compile(
    r'^\s*\[(?P<role>[^\]]+)\]'
    r'(?:\s+"(?P<text>.*)")?'
    r'\s+x:(?P<x>-?\d+(?:\.\d+)?)'
    r'\s+y:(?P<y>-?\d+(?:\.\d+)?)'
    r'\s+w:(?P<w>\d+(?:\.\d+)?)'
    r'\s+h:(?P<h>\d+(?:\.\d+)?)'
    r'(?:\s+visible\b)?'
)

for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
    match = pattern.match(raw_line)
    if not match:
        continue
    role = match.group("role") or ""
    text = match.group("text") or ""
    if role_hint and role_hint not in role:
        continue
    if text != exact_text:
        continue
    print(",".join((match.group("x"), match.group("y"), match.group("w"), match.group("h"))))
    raise SystemExit(0)

print("")
PY
}

first_visible_bounds_for_candidates() {
  local traversal_file="$1"
  local role_hint="$2"
  shift 2
  local candidate bounds=""
  for candidate in "$@"; do
    bounds="$(visible_bounds_by_text "$traversal_file" "$role_hint" "$candidate")"
    if [[ -n "$bounds" ]]; then
      printf '%s\n' "$bounds"
      return 0
    fi
  done
  return 1
}

click_bounds() {
  local _pid="$1"
  local bounds="$2"
  printf '%s\n' "$bounds"
}

click_visible_candidates() {
  local pid="$1"
  local traversal_file="$2"
  local role_hint="$3"
  shift 3
  local bounds candidate click_spec click_x click_y width height

  if bounds="$(first_visible_bounds_for_candidates "$traversal_file" "$role_hint" "$@")"; then
    click_spec="$(click_bounds "$pid" "$bounds")"
    IFS=',' read -r click_x click_y width height <<<"$click_spec"
    if python3 "$macos_use" click-coord --pid "$pid" --x "$click_x" --y "$click_y" --width "$width" --height "$height" >/dev/null 2>&1; then
      printf '%s\n' "$bounds"
      return 0
    fi
  fi

  for candidate in "$@"; do
    if [[ -n "$role_hint" ]]; then
      if python3 "$macos_use" click-text --pid "$pid" --role "$role_hint" --text "$candidate" >/dev/null 2>&1; then
        printf '%s\n' "${bounds:-}"
        return 0
      fi
    else
      if python3 "$macos_use" click-text --pid "$pid" --text "$candidate" >/dev/null 2>&1; then
        printf '%s\n' "${bounds:-}"
        return 0
      fi
    fi
  done

  return 1
}

latest_recent_snapshot() {
  python3 - "$recents_plist" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
if not path.exists():
    print("0\t")
    raise SystemExit(0)

with path.open("rb") as fh:
    payload = plistlib.load(fh)

if isinstance(payload, list) and payload:
    latest = payload[-1]
    latest_name = "" if latest is None else str(latest).strip()
    print(f"{len(payload)}\t{latest_name}")
else:
    print("0\t")
PY
}

latest_recent_name() {
  local snapshot
  snapshot="$(latest_recent_snapshot)"
  printf '%s\n' "${snapshot#*$'\t'}"
}

resolve_recent_path() {
  local item_name="$1"
  python3 - "$library_dir" "$item_name" <<'PY'
import sys
from pathlib import Path

library_dir = Path(sys.argv[1]).expanduser()
item_name = str(sys.argv[2]).strip()

if not item_name:
    print("")
    raise SystemExit(0)

candidates = [
    library_dir / "Pictures" / item_name,
    library_dir / "Originals" / item_name,
    library_dir / item_name,
]
for candidate in candidates:
    if candidate.exists():
        print(candidate)
        raise SystemExit(0)

for candidate in library_dir.rglob(item_name):
    if candidate.exists():
        print(candidate)
        raise SystemExit(0)

print(library_dir / "Pictures" / item_name)
PY
}

wait_for_new_recent_path() {
  local baseline_count="$1"
  local baseline_name="$2"
  local timeout_seconds="$3"
  local deadline
  deadline=$(( $(date +%s) + timeout_seconds ))

  while (( $(date +%s) <= deadline )); do
    local current_snapshot current_count current_name current_path
    current_snapshot="$(latest_recent_snapshot)"
    current_count="${current_snapshot%%$'\t'*}"
    current_name="${current_snapshot#*$'\t'}"
    if [[ "$current_count" != "$baseline_count" || ( -n "$current_name" && "$current_name" != "$baseline_name" ) ]]; then
      current_path="$(resolve_recent_path "$current_name")"
      if [[ -n "$current_path" ]]; then
        printf '%s\n' "$current_path"
        return 0
      fi
    fi
    sleep 1
  done

  return 1
}

click_first_text() {
  local pid="$1"
  shift
  local candidate
  local last_error=""
  for candidate in "$@"; do
    if python3 "$macos_use" click-text --pid "$pid" --text "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
    last_error="$candidate"
  done
  echo "Could not click any expected Photo Booth control text. Last candidate: ${last_error:-<none>}" >&2
  return 1
}

refresh_traversal_file() {
  local pid="$1"
  local refresh_json
  refresh_json="$(python3 "$macos_use" --json refresh --pid "$pid")"
  json_get "$refresh_json" "file"
}

ensure_common_prereqs() {
  if [[ ! -f "$macos_use" ]]; then
    echo "macos-use control script not found: $macos_use" >&2
    exit 1
  fi
  if [[ ! -f "$recents_plist" ]]; then
    echo "Photo Booth recents file not found at $recents_plist" >&2
    exit 1
  fi
}

direct_start() {
  ensure_common_prereqs

  local baseline baseline_count baseline_snapshot open_json open_status pid traversal_file start_bounds
  baseline_snapshot="$(latest_recent_snapshot)"
  baseline_count="${baseline_snapshot%%$'\t'*}"
  baseline="${baseline_snapshot#*$'\t'}"

  open_json="$(python3 "$macos_use" --json open --app com.apple.PhotoBooth)"
  open_status="$(json_get "$open_json" status)"
  if [[ "$open_status" != "success" ]]; then
    echo "$open_json" >&2
    exit 1
  fi

  pid="$(json_get "$open_json" pid)"
  traversal_file="$(json_get "$open_json" file)"

  if file_contains_any "$traversal_file" '查看视频预览' 'View Video Preview'; then
    click_visible_candidates "$pid" "$traversal_file" "AXButton" "查看视频预览" "View Video Preview" >/dev/null || \
      click_first_text "$pid" "查看视频预览" "View Video Preview" >/dev/null
    sleep 1
    traversal_file="$(refresh_traversal_file "$pid")"
  fi

  if file_contains_any "$traversal_file" '停止录制' 'Stop Recording' '停止' 'Stop'; then
    echo "Photo Booth appears to already be recording." >&2
    exit 1
  fi

  if ! file_contains_any "$traversal_file" '录制视频' 'Start Recording' 'Record Video' '开始录制'; then
    click_visible_candidates "$pid" "$traversal_file" "AXRadioButton" "视频 录制影片剪辑" "Video" "Movie" >/dev/null || \
      click_first_text "$pid" "视频 录制影片剪辑" "视频 录制" "Video" "Movie" >/dev/null
    sleep 1
    traversal_file="$(refresh_traversal_file "$pid")"
  fi

  start_bounds="$(click_visible_candidates "$pid" "$traversal_file" "AXButton" "录制视频" "Start Recording" "Record Video")" || {
    echo "Could not find the Photo Booth record button in $traversal_file" >&2
    exit 1
  }

  if [[ "$record_start_settle_s" != "0" ]]; then
    sleep "$record_start_settle_s"
  fi

  printf 'BASELINE_RECENT_COUNT\t%s\n' "$baseline_count"
  printf 'BASELINE_RECENT_NAME\t%s\n' "$baseline"
  printf 'RECORD_BUTTON_BOUNDS\t%s\n' "$start_bounds"
}

direct_stop() {
  ensure_common_prereqs

  local open_json open_status pid traversal_file saved_path save_timeout_seconds stop_bounds click_spec click_x click_y width height

  open_json="$(python3 "$macos_use" --json open --app com.apple.PhotoBooth)"
  open_status="$(json_get "$open_json" status)"
  if [[ "$open_status" != "success" ]]; then
    echo "$open_json" >&2
    exit 1
  fi

  pid="$(json_get "$open_json" pid)"
  traversal_file="$(json_get "$open_json" file)"

  if file_contains_any "$traversal_file" '查看视频预览' 'View Video Preview'; then
    click_visible_candidates "$pid" "$traversal_file" "AXButton" "查看视频预览" "View Video Preview" >/dev/null || \
      click_first_text "$pid" "查看视频预览" "View Video Preview" >/dev/null
    sleep 1
    traversal_file="$(refresh_traversal_file "$pid")"
  fi

  if ! file_contains_any "$traversal_file" '停止录制视频' '停止录制' 'Stop Recording'; then
    sleep 0.5
    traversal_file="$(refresh_traversal_file "$pid")"
  fi

  stop_bounds="$(first_visible_bounds_for_candidates "$traversal_file" "AXButton" "停止录制视频" "停止录制" "Stop Recording" "停止")" || true
  if [[ -n "$stop_bounds" ]]; then
    click_spec="$(click_bounds "$pid" "$stop_bounds")"
    IFS=',' read -r click_x click_y width height <<<"$click_spec"
    python3 "$macos_use" click-coord --pid "$pid" --x "$click_x" --y "$click_y" --width "$width" --height "$height" >/dev/null 2>&1 || true
  elif [[ -n "$button_bounds" ]]; then
    click_spec="$(click_bounds "$pid" "$button_bounds")"
    IFS=',' read -r click_x click_y width height <<<"$click_spec"
    python3 "$macos_use" click-coord --pid "$pid" --x "$click_x" --y "$click_y" --width "$width" --height "$height" >/dev/null 2>&1 || true
  else
    echo "Photo Booth stop-record button is not visible, and no fallback button bounds were provided." >&2
    exit 1
  fi

  save_timeout_seconds="$(integer_seconds "$save_timeout")"
  if ! saved_path="$(wait_for_new_recent_path "$baseline_recent_count" "$baseline_recent_name" "$save_timeout_seconds")"; then
    echo "Photo Booth did not save a new video item within ${save_timeout}s." >&2
    if [[ -n "$baseline_recent_name" ]]; then
      echo "Latest known recent item before recording: $baseline_recent_name" >&2
    fi
    exit 1
  fi

  printf 'ORIGINAL_VIDEO_PATH\t%s\n' "$saved_path"
}

run_via_terminal() {
  local direct_mode="$1"
  local timeout_window="$2"
  local quoted_script quoted_settle quoted_timeout quoted_baseline quoted_macos_use quoted_library_dir
  local runner_script log_file exit_file cleanup_cmd
  local command_snippet=""

  quoted_script="$(printf '%q' "$script_path")"
  quoted_settle="$(printf '%q' "$record_start_settle_s")"
  quoted_timeout="$(printf '%q' "$save_timeout")"
  quoted_baseline="$(printf '%q' "$baseline_recent_name")"
  quoted_macos_use="$(printf '%q' "$macos_use")"
  quoted_library_dir="$(printf '%q' "$library_dir")"

  case "$direct_mode" in
    start)
      command_snippet="bash $quoted_script --direct-start --record-start-settle-s $quoted_settle"
      ;;
    stop)
      command_snippet="bash $quoted_script --direct-stop --video-save-timeout $quoted_timeout"
      if [[ -n "$baseline_recent_name" ]]; then
        command_snippet="$command_snippet --baseline-recent-name $quoted_baseline"
      fi
      if [[ -n "$baseline_recent_count" && "$baseline_recent_count" != "0" ]]; then
        command_snippet="$command_snippet --baseline-recent-count $(printf '%q' "$baseline_recent_count")"
      fi
      if [[ -n "$button_bounds" ]]; then
        command_snippet="$command_snippet --button-bounds $(printf '%q' "$button_bounds")"
      fi
      ;;
    *)
      echo "Unknown direct mode: $direct_mode" >&2
      exit 64
      ;;
  esac

  runner_script="$(mktemp /tmp/photo_booth_record_video_runner.XXXXXX)"
  log_file="$(mktemp /tmp/photo_booth_record_video_runner.XXXXXX)"
  exit_file="$(mktemp /tmp/photo_booth_record_video_runner.XXXXXX)"
  rm -f "$exit_file"

  # EXIT traps run after function locals are gone, so bake the temp paths in now.
  cleanup_cmd="rm -f $(printf '%q ' "$runner_script" "$log_file" "$exit_file")"
  trap "$cleanup_cmd" EXIT

cat >"$runner_script" <<EOF
#!/bin/bash
set -euo pipefail
export PHOTO_BOOTH_MACOS_USE_CONTROL_SCRIPT=$quoted_macos_use
export DJI_SHOW_DEMO_MACOS_USE_CONTROL_SCRIPT=$quoted_macos_use
export DJI_SHOW_DEMO_PHOTO_BOOTH_LIBRARY_DIR=$quoted_library_dir
if output=\$($command_snippet 2>&1); then
  printf '%s\n' "\$output" >"$log_file"
  if [[ "$direct_mode" == "stop" ]]; then
    media_path="\$(printf '%s\n' "\$output" | awk -F '\t' '/^ORIGINAL_VIDEO_PATH\t/ {print \$2}' | tail -n 1)"
    if [[ -z "\$media_path" ]]; then
      echo "Direct stop flow did not report ORIGINAL_VIDEO_PATH." >&2
      exit 1
    fi
    dest="/tmp/\$(basename "\$media_path")"
    cp "\$media_path" "\$dest"
    chmod 644 "\$dest"
    if [[ "$reveal_result" == "1" ]]; then
      open "\$dest" >/dev/null 2>&1 || true
    fi
    printf 'VIDEO_PATH\t%s\n' "\$dest" >>"$log_file"
  fi
  echo "0" >"$exit_file"
else
  status=\$?
  printf '%s\n' "\$output" >"$log_file"
  echo "\$status" >"$exit_file"
fi
EOF
  chmod +x "$runner_script"

  open -a Terminal "$runner_script" >/dev/null

  local deadline
  deadline=$(( $(date +%s) + timeout_window ))
  while (( $(date +%s) <= deadline )); do
    if [[ -f "$exit_file" ]]; then
      local exit_code
      exit_code="$(cat "$exit_file")"
      if [[ "$exit_code" == "0" && -s "$log_file" ]]; then
        grep $'\t' "$log_file" || true
        return 0
      fi
      break
    fi
    sleep 1
  done

  if [[ -s "$log_file" ]]; then
    echo "--- video runner log ---" >&2
    cat "$log_file" >&2
  fi

  if [[ "$direct_mode" == "start" ]]; then
    echo "Photo Booth Terminal-runner start-record flow did not complete successfully." >&2
  else
    echo "Photo Booth Terminal-runner stop-record flow did not complete successfully." >&2
  fi
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|--direct-start|--direct-stop)
      mode="$1"
      shift
      ;;
    --record-start-settle-s)
      require_value "$1" "${2-}"
      record_start_settle_s="$2"
      shift 2
      ;;
    --video-save-timeout|--save-timeout)
      require_value "$1" "${2-}"
      save_timeout="$2"
      shift 2
      ;;
    --baseline-recent-name)
      require_value "$1" "${2-}"
      baseline_recent_name="$2"
      shift 2
      ;;
    --baseline-recent-count)
      require_value "$1" "${2-}"
      baseline_recent_count="$2"
      shift 2
      ;;
    --button-bounds)
      require_value "$1" "${2-}"
      button_bounds="$2"
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

case "$mode" in
  start)
    run_via_terminal "start" 25
    ;;
  stop)
    run_via_terminal "stop" $(( $(integer_seconds "$save_timeout") + 20 ))
    ;;
  --direct-start)
    direct_start
    ;;
  --direct-stop)
    direct_stop
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac

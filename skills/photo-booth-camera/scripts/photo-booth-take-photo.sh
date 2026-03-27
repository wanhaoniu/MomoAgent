#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skills_dir="$(cd "$script_dir/../.." && pwd)"
app_path="/System/Applications/Photo Booth.app"
library_dir="$HOME/Pictures/Photo Booth Library"
recents_plist="$library_dir/Recents.plist"
pictures_dir="$library_dir/Pictures"
mouse_helper_script="$script_dir/mouse_click_helper.sh"
prefer_helper_click="${PHOTO_BOOTH_USE_MOUSE_HELPER:-1}"
terminal_runner_script="${PHOTO_BOOTH_TERMINAL_RUNNER_SCRIPT:-$skills_dir/photo-booth-macos-use/scripts/photo_booth_take_photo_macos_use.sh}"
prefer_terminal_runner="${PHOTO_BOOTH_USE_TERMINAL_RUNNER:-1}"
launch_delay="2"
before_shot_delay="0"
after_shot_delay="1"
save_timeout="15"
quit_after="0"
dry_run="0"
wait_only="0"
no_countdown="0"
no_flash="0"
reveal_result="0"

usage() {
  cat <<'EOF'
Usage: photo-booth-take-photo.sh [options]

Open Photo Booth, trigger a still-photo capture, and verify that a new photo
appeared in Photo Booth Library before reporting success.

Options:
  --launch-delay SEC        Wait after opening Photo Booth (default: 2)
  --before-shot-delay SEC   Wait before pressing the shutter (default: 0)
  --after-shot-delay SEC    Wait after a verified capture before quitting (default: 1)
  --save-timeout SEC        Wait this long for Photo Booth to save a new photo (default: 15)
  --quit-after              Quit Photo Booth after a verified capture
  --reveal                  Reveal the new photo in Finder after capture
  --dry-run                 Only verify that Photo Booth can be opened and focused
  --wait-only               Open Photo Booth and wait for a manual shutter press
  --no-countdown            Hold Option while clicking Take Photo
  --no-flash                Hold Shift while clicking Take Photo
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

require_number() {
  local flag="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "Expected a number for $flag, got: $value" >&2
    exit 64
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --launch-delay)
      require_value "$1" "${2-}"
      require_number "$1" "$2"
      launch_delay="$2"
      shift 2
      ;;
    --before-shot-delay)
      require_value "$1" "${2-}"
      require_number "$1" "$2"
      before_shot_delay="$2"
      shift 2
      ;;
    --after-shot-delay)
      require_value "$1" "${2-}"
      require_number "$1" "$2"
      after_shot_delay="$2"
      shift 2
      ;;
    --save-timeout)
      require_value "$1" "${2-}"
      if [[ ! "$2" =~ ^[0-9]+$ ]]; then
        echo "Expected an integer for $1, got: $2" >&2
        exit 64
      fi
      save_timeout="$2"
      shift 2
      ;;
    --quit-after)
      quit_after="1"
      shift
      ;;
    --reveal)
      reveal_result="1"
      shift
      ;;
    --dry-run)
      dry_run="1"
      shift
      ;;
    --wait-only)
      wait_only="1"
      shift
      ;;
    --no-countdown)
      no_countdown="1"
      shift
      ;;
    --no-flash)
      no_flash="1"
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

if [[ ! -d "$app_path" ]]; then
  echo "Photo Booth not found at $app_path" >&2
  exit 1
fi

if [[ ! -f "$recents_plist" ]]; then
  echo "Photo Booth recents file not found at $recents_plist" >&2
  exit 1
fi

recent_snapshot() {
  osascript -l JavaScript <<'JXA'
ObjC.import('Foundation');

(() => {
  const home = ObjC.unwrap($.NSHomeDirectory())
  const path = home + '/Pictures/Photo Booth Library/Recents.plist'
  const data = $.NSData.dataWithContentsOfFile(path)

  if (!data) {
    return '0\t'
  }

  const plist = $.NSPropertyListSerialization.propertyListWithDataOptionsFormatError(data, 0, null, null)
  const items = plist ? ObjC.deepUnwrap(plist) : []
  const last = items.length > 0 ? String(items[items.length - 1]) : ''
  return String(items.length) + '\t' + last
})()
JXA
}

latest_recent_name() {
  local snapshot
  snapshot="$(recent_snapshot)"
  printf '%s\n' "${snapshot#*$'\t'}"
}

latest_recent_path() {
  local recent_name
  recent_name="$(latest_recent_name)"
  if [[ -z "$recent_name" ]]; then
    return 1
  fi
  printf '%s/%s\n' "$pictures_dir" "$recent_name"
}

wait_for_new_recent() {
  local baseline_count="$1"
  local baseline_name="$2"
  local timeout_seconds="$3"
  local deadline
  deadline=$(( $(date +%s) + timeout_seconds ))

  while (( $(date +%s) <= deadline )); do
    local snapshot current_count current_name
    snapshot="$(recent_snapshot)"
    current_count="${snapshot%%$'\t'*}"
    current_name="${snapshot#*$'\t'}"

    if [[ "$current_count" != "$baseline_count" || "$current_name" != "$baseline_name" ]]; then
      if [[ -n "$current_name" ]]; then
        printf '%s/%s\n' "$pictures_dir" "$current_name"
        return 0
      fi
    fi

    sleep 1
  done

  return 1
}

reveal_in_finder() {
  local target_path="$1"
  local attempt
  for attempt in 1 2 3 4 5; do
    if osascript - "$target_path" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set targetPath to item 1 of argv
tell application "Finder"
  reveal POSIX file targetPath
  activate
end tell
end run
APPLESCRIPT
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

quit_photo_booth() {
  osascript -e 'tell application id "com.apple.PhotoBooth" to quit' >/dev/null
}

terminal_runner_supported() {
  [[ "$prefer_terminal_runner" == "1" ]] || return 1
  [[ "$dry_run" == "0" ]] || return 1
  [[ "$wait_only" == "0" ]] || return 1
  [[ "$no_countdown" == "0" ]] || return 1
  [[ "$no_flash" == "0" ]] || return 1
  [[ -x "$terminal_runner_script" ]] || return 1
}

run_terminal_runner_capture() {
  local command=(
    bash
    "$terminal_runner_script"
    --before-shot-delay
    "$before_shot_delay"
    --save-timeout
    "$save_timeout"
  )
  local runner_output capture_path

  if [[ "$reveal_result" == "1" ]]; then
    command+=(--reveal)
  fi

  if ! runner_output="$("${command[@]}" 2>&1)"; then
    printf '%s\n' "$runner_output" >&2
    return 1
  fi

  capture_path="$(printf '%s\n' "$runner_output" | tail -n 1)"
  if [[ -z "$capture_path" ]]; then
    echo "Terminal-run Photo Booth capture did not return a saved photo path." >&2
    if [[ -n "$runner_output" ]]; then
      printf '%s\n' "$runner_output" >&2
    fi
    return 1
  fi

  printf '%s\n' "$capture_path"
}

prepare_photo_booth() {
  local dry_flag="$1"
  osascript - "$launch_delay" "$dry_flag" <<'APPLESCRIPT'
on argToReal(textValue)
  return textValue as real
end argToReal

on argToBool(textValue)
  if textValue is "1" then return true
  if textValue is "true" then return true
  return false
end argToBool

on ensureAccessibility()
  try
    tell application "System Events"
      get name of first application process
    end tell
  on error errMsg number errNum
    error "Accessibility permission is required. Enable Codex or the terminal host in System Settings > Privacy & Security > Accessibility. Original error: " & errMsg number errNum
  end try
end ensureAccessibility

on waitForControls()
  repeat 20 times
    try
      tell application "System Events"
        tell process "Photo Booth"
          set frontmost to true
          tell group 1 of window 1
            if (count of buttons) is greater than or equal to 1 and (count of radio groups) is greater than or equal to 1 then
              return
            end if
          end tell
        end tell
      end tell
    end try
    delay 0.2
  end repeat
  error "Photo Booth camera controls did not appear."
end waitForControls

on run argv
  set launchDelay to argToReal(item 1 of argv)
  set dryRun to argToBool(item 2 of argv)

  my ensureAccessibility()
  tell application id "com.apple.PhotoBooth" to activate
  delay launchDelay
  my waitForControls()

  if dryRun then
    return "READY"
  end if

  return "OPEN"
end run
APPLESCRIPT
}

photo_booth_control_geometry() {
  local control_name="$1"
  osascript - "$control_name" <<'APPLESCRIPT'
on run argv
  set controlName to item 1 of argv
  tell application "System Events"
    tell process "Photo Booth"
      set frontmost to true
      if controlName is "picture" then
        set targetControl to radio button 2 of radio group 1 of group 1 of window 1
      else if controlName is "shutter" then
        set targetControl to button 1 of group 1 of window 1
      else
        error "Unknown Photo Booth control: " & controlName
      end if

      set {xPos, yPos} to position of targetControl
      set {wSize, hSize} to size of targetControl
      return (xPos as text) & "," & (yPos as text) & "," & (wSize as text) & "," & (hSize as text)
    end tell
  end tell
end run
APPLESCRIPT
}

hid_click() {
  local x="$1"
  local y="$2"
  local hold_option="$3"
  local hold_shift="$4"

  if [[ "$prefer_helper_click" == "1" && "$hold_option" == "0" && "$hold_shift" == "0" && -x "$mouse_helper_script" ]]; then
    if bash "$mouse_helper_script" click --x "$x" --y "$y" --count 1 >/dev/null 2>&1; then
      return 0
    fi
  fi

  swift - "$x" "$y" "$hold_option" "$hold_shift" <<'SWIFT'
import ApplicationServices
import Foundation

let args = CommandLine.arguments
let x = Double(args[1]) ?? 0
let y = Double(args[2]) ?? 0
let holdOption = args[3] == "1"
let holdShift = args[4] == "1"
let point = CGPoint(x: x, y: y)

var flags: CGEventFlags = []
if holdOption {
    flags.insert(.maskAlternate)
}
if holdShift {
    flags.insert(.maskShift)
}

let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)!
move.flags = flags
move.post(tap: .cghidEventTap)
usleep(100000)

let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)!
down.flags = flags
down.setIntegerValueField(.mouseEventClickState, value: 1)
down.post(tap: .cghidEventTap)
usleep(80000)

let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)!
up.flags = flags
up.setIntegerValueField(.mouseEventClickState, value: 1)
up.post(tap: .cghidEventTap)
SWIFT
}

click_photo_booth_control() {
  local control_name="$1"
  local hold_option="${2:-0}"
  local hold_shift="${3:-0}"
  local geometry x_pos y_pos width height center_x center_y

  geometry="$(photo_booth_control_geometry "$control_name")"
  IFS=',' read -r x_pos y_pos width height <<<"$geometry"
  center_x=$(( x_pos + (width / 2) ))
  center_y=$(( y_pos + (height / 2) ))

  hid_click "$center_x" "$center_y" "$hold_option" "$hold_shift"
}

if terminal_runner_supported; then
  if capture_path="$(run_terminal_runner_capture)"; then
    if [[ "$quit_after" == "1" ]]; then
      sleep "$after_shot_delay"
      quit_photo_booth
    fi
    echo "Photo captured: $capture_path"
    exit 0
  fi
  echo "Warning: Terminal-run capture path failed, falling back to direct Photo Booth automation." >&2
fi

snapshot_before="$(recent_snapshot)"
baseline_count="${snapshot_before%%$'\t'*}"
baseline_name="${snapshot_before#*$'\t'}"

result="$(prepare_photo_booth "$dry_run")"

if [[ "$result" == "READY" ]]; then
  echo "Photo Booth is ready."
  exit 0
fi

click_photo_booth_control "picture" "0" "0"
sleep 0.25

if [[ "$wait_only" == "1" ]]; then
  result="WAITING"
else
  if [[ "$before_shot_delay" != "0" ]]; then
    sleep "$before_shot_delay"
  fi
  click_photo_booth_control "shutter" "$no_countdown" "$no_flash"
  result="TRIGGERED"
fi

capture_path=""
if capture_path="$(wait_for_new_recent "$baseline_count" "$baseline_name" "$save_timeout")"; then
  if [[ "$quit_after" == "1" ]]; then
    sleep "$after_shot_delay"
    quit_photo_booth
  fi
  if [[ "$reveal_result" == "1" ]]; then
    if ! reveal_in_finder "$capture_path"; then
      echo "Warning: Photo was captured, but Finder could not reveal it yet." >&2
    fi
  fi
  echo "Photo captured: $capture_path"
  exit 0
fi

if [[ "$result" == "WAITING" ]]; then
  echo "Photo Booth did not report a new manual photo within ${save_timeout}s." >&2
else
  echo "Photo Booth did not report a new saved photo within ${save_timeout}s." >&2
fi
echo "Latest known Photo Booth item: ${baseline_name:-<none>}" >&2
exit 1

#!/usr/bin/env bash
set -euo pipefail

# iPad Photo Booth Camera Script
# Uses move_mouse_to_ipad.swift for precise coordinate control on iPad

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skills_dir="$(cd "$script_dir/../.." && pwd)"
mouse_helper_script="${PHOTO_BOOTH_IPAD_MOUSE_HELPER_SCRIPT:-$skills_dir/screen-pointer-tools/scripts/mouse_click_helper.sh}"
swift_tool="${PHOTO_BOOTH_IPAD_SWIFT_TOOL:-$skills_dir/screen-pointer-tools/scripts/move_mouse_to_ipad.swift}"
prefer_helper_click="${PHOTO_BOOTH_IPAD_USE_MOUSE_HELPER:-1}"
dry_run="false"
show_delay_ms=1200
reveal="true"

usage() {
  cat <<'EOF'
Usage:
  photo-booth-ipad.sh open [--dry-run] [--show-delay-ms N]
  photo-booth-ipad.sh take-photo [--dry-run] [--wait-only]
  photo-booth-ipad.sh close [--dry-run]
  photo-booth-ipad.sh run --steps open,take-photo,close [--dry-run]

Coordinates:
  Open: x=1211.04 y=46.68
  Shutter: x=1301.97 y=519.86
EOF
}

ensure_click_backend() {
  if [[ "$prefer_helper_click" == "1" && -x "$mouse_helper_script" ]]; then
    bash "$mouse_helper_script" start-server >/dev/null 2>&1 || true
    return 0
  fi

  if [[ -x "$swift_tool" ]]; then
    return 0
  fi

  cat >&2 <<EOF
No iPad click backend is available.
Checked helper: $mouse_helper_script
Checked swift tool: $swift_tool
EOF
  exit 1
}

click_point() {
  local x="$1"
  local y="$2"

  if [[ "$dry_run" == "true" ]]; then
    echo "  click x=$x y=$y"
    return
  fi

  if [[ "$prefer_helper_click" == "1" && -x "$mouse_helper_script" ]]; then
    if bash "$mouse_helper_script" click --x "$x" --y "$y" --count 1 >/dev/null 2>&1; then
      return
    fi
  fi

  if [[ -x "$swift_tool" ]]; then
    swift "$swift_tool" point --x "$x" --y "$y" >/dev/null
    return
  fi

  echo "Failed to click iPad point ($x, $y): no usable helper or swift backend." >&2
  exit 1
}

run_step() {
  local step="$1"
  case "$step" in
    open)
      [[ "$dry_run" == "true" ]] && echo "step open"
      click_point "1211.04" "46.68"
      if [[ "$dry_run" == "false" ]]; then
        sleep "$(awk "BEGIN { printf \"%.3f\", $show_delay_ms / 1000 }")"
      fi
      ;;
    take-photo)
      [[ "$dry_run" == "true" ]] && echo "step take-photo"
      click_point "1301.97" "519.86"
      ;;
    close)
      [[ "$dry_run" == "true" ]] && echo "step close"
      # Close Photo Booth on iPad - typically top-left corner or command+q
      click_point "1211.04" "46.68"  # Click back to exit camera mode
      sleep 0.5
      click_point "1211.04" "46.68"  # Click again to close
      ;;
    *)
      echo "Unknown step: $step" >&2
      exit 64
      ;;
  esac
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 64
fi

command_name="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run="true"
      shift
      ;;
    --show-delay-ms)
      show_delay_ms="${2-}"
      shift 2
      ;;
    --steps)
      steps_value="${2-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

ensure_click_backend

case "$command_name" in
  open|take-photo|close)
    run_step "$command_name"
    ;;
  run)
    if [[ -z "${steps_value:-}" ]]; then
      echo "Missing --steps for run." >&2
      exit 64
    fi
    IFS=',' read -r -a step_list <<<"$steps_value"
    if [[ "$dry_run" == "true" ]]; then
      echo "sequence $steps_value"
    fi
    for index in "${!step_list[@]}"; do
      step="$(echo "${step_list[$index]}" | xargs)"
      run_step "$step"
    done
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac

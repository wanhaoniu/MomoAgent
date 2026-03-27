#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skills_dir="$(cd "$script_dir/../.." && pwd)"
direct_script="${PHOTO_BOOTH_MACOS_USE_DIRECT_SCRIPT:-$script_dir/photo_booth_take_photo_macos_use_direct.sh}"
latest_photo_script="${PHOTO_BOOTH_LATEST_PHOTO_SCRIPT:-$skills_dir/photo-booth-camera/scripts/photo-booth-latest-photo.sh}"

before_shot_delay="0"
save_timeout="15"
reveal_result="0"

usage() {
  cat <<'EOF'
Usage: photo_booth_take_photo_macos_use.sh [options]

Launch the real Photo Booth capture flow inside Terminal (so it inherits
Terminal's Accessibility permission), then wait for the new saved photo path.

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

if [[ ! -x "$direct_script" ]]; then
  echo "Direct capture script not found: $direct_script" >&2
  exit 1
fi

if [[ ! -x "$latest_photo_script" ]]; then
  echo "Photo Booth latest-photo helper not found: $latest_photo_script" >&2
  exit 1
fi

latest_photo_path() {
  bash "$latest_photo_script" "$@"
}

baseline_photo="$(latest_photo_path 2>/dev/null || true)"

runner_script="$(mktemp /tmp/photo_booth_take_photo_runner.XXXXXX)"
log_file="$(mktemp /tmp/photo_booth_take_photo_runner.XXXXXX)"
exit_file="$(mktemp /tmp/photo_booth_take_photo_runner.XXXXXX)"
rm -f "$exit_file"

cleanup() {
  rm -f "$runner_script" "$exit_file"
}
trap cleanup EXIT

cat >"$runner_script" <<EOF
#!/bin/bash
set -euo pipefail
output=\$(bash "$direct_script" --before-shot-delay "$before_shot_delay" --save-timeout "$save_timeout" $( [[ "$reveal_result" == "1" ]] && printf '%s' '--reveal' ) 2>&1)
exit_code=\$?
printf '%s\n' "\$output" >"$log_file"
if [[ \$exit_code == 0 ]]; then
  photo_path="\$(printf '%s\n' "\$output" | tail -n 1)"
  dest="/tmp/\$(basename "\$photo_path")"
  cp "\$photo_path" "\$dest" && chmod 644 "\$dest"
  echo "\$dest" >> "$log_file"
fi
echo \$? >"$exit_file"
EOF
chmod +x "$runner_script"

open -a Terminal "$runner_script" >/dev/null

deadline=$(( $(date +%s) + save_timeout + 10 ))
while (( $(date +%s) <= deadline )); do
  current_photo="$(latest_photo_path 2>/dev/null || true)"
  if [[ -n "$current_photo" && "$current_photo" != "$baseline_photo" ]]; then
    echo "$current_photo"
    exit 0
  fi

  if [[ -f "$exit_file" ]]; then
    exit_code="$(cat "$exit_file")"
    if [[ "$exit_code" == "0" && -s "$log_file" ]]; then
      tail -n 1 "$log_file"
      exit 0
    fi
    break
  fi

  sleep 1
done

echo "Photo Booth did not report a new photo from the Terminal-run capture flow." >&2
if [[ -s "$log_file" ]]; then
  echo "--- capture log ---" >&2
  cat "$log_file" >&2
fi
exit 1

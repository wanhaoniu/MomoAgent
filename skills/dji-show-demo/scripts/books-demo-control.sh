#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
direct_script="${DJI_SHOW_DEMO_BOOKS_DIRECT_SCRIPT:-$script_dir/books-demo-control-direct.sh}"
prefer_terminal_runner="${DJI_SHOW_DEMO_BOOKS_USE_TERMINAL_RUNNER:-1}"
runner_timeout_s="${DJI_SHOW_DEMO_BOOKS_TERMINAL_RUNNER_TIMEOUT_S:-45}"

usage() {
  cat <<'EOF'
Usage: books-demo-control.sh COMMAND [args...]

Wrapper around the main-screen Books demo helper.

By default, UI-driving commands are launched via a Terminal child-process so the
real automation can inherit Terminal Accessibility permission, similar to the
Photo Booth capture and recording flows.

Environment overrides:
  DJI_SHOW_DEMO_BOOKS_USE_TERMINAL_RUNNER=0   Force direct execution
  DJI_SHOW_DEMO_BOOKS_TERMINAL_RUNNER_TIMEOUT_S=NN
                                              Override Terminal runner wait time
EOF
}

supports_terminal_runner() {
  [[ "$prefer_terminal_runner" == "1" ]] || return 1
  [[ -f "$direct_script" ]] || return 1

  local arg
  for arg in "$@"; do
    case "$arg" in
      --dry-run|--direct)
        return 1
        ;;
    esac
  done

  case "${1-}" in
    ""|-h|--help|help|health|trust|point|targets|move-books-window)
      return 1
      ;;
  esac

  return 0
}

run_direct() {
  exec bash "$direct_script" "$@"
}

run_via_terminal() {
  local runner_script log_file exit_file cleanup_cmd deadline exit_code
  local quoted_direct command_snippet arg

  quoted_direct="$(printf '%q' "$direct_script")"
  command_snippet="bash $quoted_direct"
  for arg in "$@"; do
    command_snippet="$command_snippet $(printf '%q' "$arg")"
  done

  runner_script="$(mktemp /tmp/books_demo_runner.XXXXXX)"
  log_file="$(mktemp /tmp/books_demo_runner.XXXXXX)"
  exit_file="$(mktemp /tmp/books_demo_runner.XXXXXX)"
  rm -f "$exit_file"

  cleanup_cmd="rm -f $(printf '%q ' "$runner_script" "$log_file" "$exit_file")"
  trap "$cleanup_cmd" EXIT

  cat >"$runner_script" <<EOF
#!/bin/bash
set -euo pipefail
if output=\$($command_snippet 2>&1); then
  printf '%s\n' "\$output" >"$log_file"
  echo "0" >"$exit_file"
else
  status=\$?
  printf '%s\n' "\$output" >"$log_file"
  echo "\$status" >"$exit_file"
fi
EOF
  chmod +x "$runner_script"

  open -a Terminal "$runner_script" >/dev/null

  deadline=$(( $(date +%s) + runner_timeout_s ))
  while (( $(date +%s) <= deadline )); do
    if [[ -f "$exit_file" ]]; then
      exit_code="$(cat "$exit_file")"
      if [[ "$exit_code" == "0" ]]; then
        if [[ -s "$log_file" ]]; then
          cat "$log_file"
        fi
        return 0
      fi
      break
    fi
    sleep 1
  done

  if [[ -s "$log_file" ]]; then
    echo "--- books runner log ---" >&2
    cat "$log_file" >&2
  fi

  if [[ -f "$exit_file" ]]; then
    echo "Books Terminal-runner flow did not complete successfully." >&2
  else
    echo "Books Terminal-runner flow timed out after ${runner_timeout_s}s." >&2
  fi
  return 1
}

case "${1-}" in
  -h|--help|help)
    usage
    exit 0
    ;;
esac

if supports_terminal_runner "$@"; then
  run_via_terminal "$@"
else
  run_direct "$@"
fi

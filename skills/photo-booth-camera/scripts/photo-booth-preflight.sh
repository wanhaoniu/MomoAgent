#!/usr/bin/env bash
set -euo pipefail

app_path="/System/Applications/Photo Booth.app"

if [[ ! -d "$app_path" ]]; then
  echo "Photo Booth not found at $app_path" >&2
  exit 1
fi

if ! command -v osascript >/dev/null 2>&1; then
  echo "osascript is not available on this Mac." >&2
  exit 1
fi

echo "Photo Booth app: OK"
echo "AppleScript runtime: OK"

if osascript <<'APPLESCRIPT' >/dev/null 2>&1
tell application "System Events"
  get name of first application process
end tell
APPLESCRIPT
then
  echo "Accessibility permission: OK"
else
  cat >&2 <<'EOF'
Accessibility permission: missing

Enable Accessibility access for Codex or the terminal host:
System Settings > Privacy & Security > Accessibility
EOF
  exit 2
fi

cat <<'EOF'
Preflight complete.

If you want to use your iPhone camera, make sure Continuity Camera is already selected inside Photo Booth.
EOF

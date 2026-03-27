#!/usr/bin/env bash
set -euo pipefail

library_dir="$HOME/Pictures/Photo Booth Library"
recents_plist="$library_dir/Recents.plist"
pictures_dir="$library_dir/Pictures"
reveal_result="0"

usage() {
  cat <<'EOF'
Usage: photo-booth-latest-photo.sh [--reveal]

Print the most recent Photo Booth photo path using Photo Booth Library/Recents.plist.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

if [[ ! -f "$recents_plist" ]]; then
  echo "Photo Booth recents file not found at $recents_plist" >&2
  exit 1
fi

latest_name="$(
  osascript -l JavaScript <<'JXA'
ObjC.import('Foundation');

(() => {
  const home = ObjC.unwrap($.NSHomeDirectory())
  const path = home + '/Pictures/Photo Booth Library/Recents.plist'
  const data = $.NSData.dataWithContentsOfFile(path)

  if (!data) {
    return ''
  }

  const plist = $.NSPropertyListSerialization.propertyListWithDataOptionsFormatError(data, 0, null, null)
  const items = plist ? ObjC.deepUnwrap(plist) : []
  return items.length > 0 ? String(items[items.length - 1]) : ''
})()
JXA
)"

if [[ -z "$latest_name" ]]; then
  echo "Photo Booth has no recent photos recorded yet." >&2
  exit 1
fi

latest_path="$pictures_dir/$latest_name"

if [[ "$reveal_result" == "1" ]]; then
  if ! osascript - "$latest_path" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set latestPath to item 1 of argv
tell application "Finder"
  reveal POSIX file latestPath
  activate
end tell
end run
APPLESCRIPT
  then
    if ! osascript - "$pictures_dir" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set picturesPath to item 1 of argv
tell application "Finder"
  reveal POSIX file picturesPath
  activate
end tell
end run
APPLESCRIPT
    then
      echo "Warning: Finder could not reveal the latest Photo Booth file yet." >&2
    fi
  fi
fi

echo "$latest_path"

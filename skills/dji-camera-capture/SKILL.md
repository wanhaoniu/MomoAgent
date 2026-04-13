---
name: dji-camera-capture
description: Use when asked to take a photo, start video recording, stop video recording, inspect camera devices, or save media into session subfolders on this macOS machine. This skill works with OsmoPocket3 and ordinary macOS cameras such as USB or UVC webcams, using a native AVFoundation helper under skills/dji-camera-capture/workspace/captures.
---

# Mac Camera Capture

Use this skill to capture photos and videos from cameras that macOS can open normally, including:

- `OsmoPocket3`
- ordinary USB or UVC webcams such as `LRCP G-720P`
- other cameras that appear in macOS camera apps

This skill saves media on the Mac. It does not press a hardware shutter on the device itself and does not save onto the camera's SD card.

## Preconditions

- This machine must be macOS.
- `xcrun swiftc` must be available so the helper can compile on first use.
- `ffmpeg` should be installed so recorded `.mov` files can be remuxed to `.mp4` when recording stops.
- The Terminal or app running Codex must already have camera permission in macOS Privacy settings.

## Output Layout

All media is stored under:

```text
skills/dji-camera-capture/workspace/captures/<session>/
├── photos/
│   └── IMG_YYYYMMDD_HHMMSS.jpg
└── videos/
    ├── VID_YYYYMMDD_HHMMSS.mp4
    └── VID_YYYYMMDD_HHMMSS.native.log
```

## Everyday Commands

1. Inspect available cameras.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py list
```

2. Optional one-time setup when more than one camera is connected.

Pick the camera you want to use by default for later `photo` and `start-video` commands.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py select-camera --camera-name "LRCP G-720P"
```

3. Take one photo.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py photo --session demo-shot
```

4. Start video recording.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py start-video --session demo-video
```

5. Stop the current recording and finalize the `.mp4`.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py stop-video
```

## Selection Rules

- If `--camera-name` or `--video-index` is provided, use that camera.
- Otherwise, if a previously selected default camera is still connected, use it.
- Otherwise, prefer `OsmoPocket3` when it is available.
- Otherwise, if only one video camera is available, use that one.
- Otherwise, ask the user to run `list` and `select-camera` first.

## Optional Flags

- If the user explicitly needs audio:

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py start-video --session demo-video --with-audio
```

- If a one-off command should use a different camera without changing the saved default:

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py photo --camera-name "OsmoPocket3" --session demo-shot
```

## Notes

- The first real command may take a few seconds because the Swift helper is compiled into `workspace/runtime/`.
- `python3 -m py_compile skills/dji-camera-capture/scripts/dji_camera_capture.py` only checks Python syntax. It is expected to print nothing when successful.
- If capture fails, check macOS camera permissions first, then verify the device is still listed by the `list` command.

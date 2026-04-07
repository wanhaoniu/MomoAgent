---
name: dji-camera-capture
description: Use when asked to take a photo, start video recording, stop video recording, inspect camera devices, or save DJI OsmoPocket3 media into session subfolders on this macOS machine. This skill uses a native AVFoundation helper and stores media under skills/dji-camera-capture/workspace/captures.
---

# DJI Camera Capture

Use this skill to control local photo capture and video recording for the `OsmoPocket3` camera that appears as a macOS camera device.

This skill saves media on the Mac. It does not press the shutter on the DJI device itself and does not save onto the camera SD card.

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

While recording is in progress, the native helper writes a temporary `.mov` file first and the wrapper converts it to `.mp4` on `stop-video`.

## Workflow

1. List devices when you need to confirm indexes or whether `OsmoPocket3` is present.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py list
```

2. Take one photo and save it into a named session subfolder.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py photo \
  --camera-name OsmoPocket3 \
  --session demo-shot
```

3. Start video recording.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py start-video \
  --camera-name OsmoPocket3 \
  --session demo-video
```

If the user explicitly needs audio and the matching audio device is stable:

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py start-video \
  --camera-name OsmoPocket3 \
  --session demo-video \
  --with-audio
```

4. Inspect whether a recording is active.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py status
```

5. Stop the current recording and finalize the `.mp4`.

```bash
python3 skills/dji-camera-capture/scripts/dji_camera_capture.py stop-video
```

## Notes

- The first real command may take a few seconds because the Swift helper is compiled into `workspace/runtime/`.
- `python3 -m py_compile skills/dji-camera-capture/scripts/dji_camera_capture.py` only checks Python syntax. It is expected to print nothing when successful.
- Prefer `--camera-name OsmoPocket3` instead of hardcoded indexes because indexes can change after unplugging or rebooting.
- If capture fails, check macOS camera permissions first, then verify the device is still listed by the `list` command.

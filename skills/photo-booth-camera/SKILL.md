---
name: photo-booth-camera
description: Open Photo Booth on macOS and trigger a still photo capture, including iPhone Continuity Camera setups. Also use it when the user wants to take a photo and immediately pass the newly saved local image into `artsapi-image-video` for image-to-image or image-to-video generation, such as turning a fresh photo into a poster or animating it.
metadata:
  openclaw:
    emoji: "📸"
    requires:
      bins: ["bash", "python3"]
---

# Photo Booth Camera

Open Photo Booth and automate a single-photo capture by running the bundled scripts. For standard OpenClaw capture requests, the main entrypoint now prefers the older stable `Terminal` child-process path from `photo-booth-macos-use` so it can inherit `Terminal` accessibility permission; when that path is not applicable, it falls back to the local direct click flow and still validates success against `Photo Booth Library/Recents.plist`.

**Trigger:** When user says "拍照", "帮我拍张照", "拍个照片", or similar requests to take a photo using Photo Booth, run the capture flow automatically.

**Extended Trigger:** When user says "拍完后转成海报", "拍照后图生图", "拍完后让它动起来", "拍一张再做成视频", or similar requests, first capture the photo with Photo Booth, then immediately pass the saved local file into `artsapi-image-video`.

## Quick Start

1. Run `scripts/photo-booth-preflight.sh` before the first capture on a new Mac session.
2. Make sure Photo Booth is already using the desired camera. For iPhone capture, select the Continuity Camera source in Photo Booth first.
3. Run `scripts/photo-booth-take-photo.sh` to launch the app, trigger the shutter, and wait for a verified saved photo.
4. If the user wants post-processing after capture, run `scripts/photo-booth-capture-to-artsapi.py` instead of stitching the steps together manually.
5. Under OpenClaw, standard photo capture now prefers the `photo-booth-macos-use` Terminal-runner path first, because it can inherit `Terminal` accessibility permission and bypass `openclaw-gateway` click restrictions.
6. Keep the vendored `scripts/mouse_click_helper.sh` path available as the click-delivery fallback when direct host-side mouse events are flaky or blocked by permission.

## Use the Scripts

**When user asks to take a photo:** Run this command automatically. It now prefers the Terminal-runner backend for normal captures:
```bash
/Users/moce/.openclaw/skills/photo-booth-camera/scripts/photo-booth-take-photo.sh --before-shot-delay 2 --reveal
```

Run the preflight check:

```bash
scripts/photo-booth-preflight.sh
```

Take a photo immediately after Photo Booth opens:

```bash
scripts/photo-booth-take-photo.sh
```

Give the user time to pose, reveal the result, then close Photo Booth after the shot:

```bash
scripts/photo-booth-take-photo.sh --before-shot-delay 5 --reveal --quit-after
```

Use Apple's modifier behavior to disable the built-in countdown and flash:

```bash
scripts/photo-booth-take-photo.sh --no-countdown --no-flash
```

Verify automation without pressing the shutter:

```bash
scripts/photo-booth-take-photo.sh --dry-run
```

Open Photo Booth, then wait for a manual shutter press and return the new file path:

```bash
scripts/photo-booth-take-photo.sh --wait-only --reveal
```

Show the latest saved Photo Booth image path:

```bash
scripts/photo-booth-latest-photo.sh --reveal
```

Take a photo, then immediately send that fresh local file into ArtsAPI image-to-image:

```bash
python3 /Users/moce/.openclaw/skills/photo-booth-camera/scripts/photo-booth-capture-to-artsapi.py image \
  --prompt "把这张照片改成电影海报风格" \
  --photo-before-shot-delay 2 \
  --photo-reveal
```

Take a photo, then immediately send it into ArtsAPI image-to-video:

```bash
python3 /Users/moce/.openclaw/skills/photo-booth-camera/scripts/photo-booth-capture-to-artsapi.py video \
  --prompt "从这张照片开始，镜头缓慢推进，人物衣服轻微摆动" \
  --photo-before-shot-delay 2 \
  --duration 5 \
  --resolution 720p \
  --ratio 16:9
```

## Follow This Workflow

1. Run the preflight script if this is the first attempt or if GUI automation fails.
2. If the preflight script reports missing Accessibility permission, stop and have the user enable it in `System Settings > Privacy & Security > Accessibility`.
3. Open Photo Booth and confirm the live preview is already showing the correct camera.
4. Use `--before-shot-delay` whenever the user needs time to frame the shot.
5. Prefer the default `scripts/photo-booth-take-photo.sh` entrypoint instead of manually choosing a backend. It will try the Terminal child-process route first for normal captures, then fall back to the direct local click route if needed.
6. Treat `Photo Booth did not report a new saved photo` as a real failure signal. In that case, keep Photo Booth open, verify the live preview, and try again instead of trusting the button click.
7. Use `--wait-only` when the GUI automation is flaky but the user can still tap the shutter manually.
8. Use `scripts/photo-booth-latest-photo.sh` whenever the user needs the concrete saved-file path after a manual or automated shot.
9. If the user wants a transformation after capture, prefer `scripts/photo-booth-capture-to-artsapi.py` so the saved local path flows directly into `artsapi-image-video`.
10. For chained ArtsAPI runs, treat the just-captured local photo as the input image. Do not ask the user to re-locate or re-upload the file manually.
11. If the ArtsAPI key is missing, stop and point the user to `/Users/moce/.openclaw/skills/artsapi-image-video/config/artsapi.env`.
12. If the user only asks for a photo, do not invoke ArtsAPI.

## Chained ArtsAPI Workflow

1. Capture the photo with Photo Booth and verify a new file was saved.
2. Extract the saved local file path from Photo Booth output.
3. Call `/Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py` through `scripts/photo-booth-capture-to-artsapi.py`.
4. For image transformations, use the `image` subcommand.
5. For animation or video generation, use the `video` subcommand.
6. Relay both the captured file path and the ArtsAPI result summary back to the user.

## Dependency

- This skill chains into:
  `/Users/moce/.openclaw/skills/artsapi-image-video/SKILL.md`
- Stable Terminal-runner backend:
  `/Users/moce/.openclaw/skills/photo-booth-macos-use/SKILL.md`
- ArtsAPI key file:
  `/Users/moce/.openclaw/skills/artsapi-image-video/config/artsapi.env`
- Vendored helper for click delivery:
  `/Users/moce/.openclaw/skills/photo-booth-camera/scripts/mouse_click_helper.sh`

## Known Limits

- Rely on macOS Accessibility permission for GUI scripting and HID mouse events.
- Assume Photo Booth is already configured to the preferred camera source.
- Resolve saved-photo paths through `~/Pictures/Photo Booth Library/Recents.plist` and `~/Pictures/Photo Booth Library/Pictures/`.
- Target still-photo capture only. Do not use this skill for video recording or media export.
- The chained ArtsAPI step sends the captured image contents to ArtsAPI as part of the generation request.
- The chained flow depends on `artsapi-image-video` being installed and configured.
- `photo-booth-take-photo.sh` now prefers the Terminal child-process backend from `photo-booth-macos-use` for standard captures under OpenClaw, and falls back to helper-backed/local direct clicks when special options require the local flow.

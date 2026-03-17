# gesture_loc

Lightweight MediaPipe gesture-recognition service plus a configurable gesture-to-robot action runner.

## What it provides

- `GET /health`
- `GET /latest`
- `GET /status`
- Optional local action runner that maps stable gestures to `soarmmoce_sdk` actions

## Default gestures

The MediaPipe canned gesture recognizer can emit gestures such as:

- `Open_Palm`
- `Closed_Fist`
- `Pointing_Up`
- `Thumb_Up`
- `Thumb_Down`
- `Victory`
- `ILoveYou`

## Run the service

```bash
PYTHONNOUSERSITE=1 conda run -n gestureloc python Software/Master/gesture_loc/main.py \
  --config Software/Master/gesture_loc/configs/default.yaml
```

Query the latest result:

```bash
curl http://127.0.0.1:8012/latest | jq
```

## Run gesture actions

```bash
PYTHONNOUSERSITE=1 conda run -n localqwentts python Software/Master/gesture_loc/scripts/gesture_action_runner.py \
  --config Software/Master/gesture_loc/configs/actions.default.yaml
```

## Notes

- The recognizer model will auto-download on first run when `allow_auto_download: true`.
- Actions trigger from `stable_gesture_name`, not raw per-frame predictions.
- Edit `configs/actions.default.yaml` to remap gestures to robot actions.
- The shipped default action map avoids gripper-only actions so it can run on the current arm without extra hardware.

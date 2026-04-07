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

Create a dedicated conda env first:

```bash
cd /home/ubuntu/Code/MoceClaw/Software/Master/gesture_loc
source /home/ubuntu/miniconda3/etc/profile.d/conda.sh
conda env create -f environment.yml -n gesture_loc
conda activate gesture_loc
```

`gesture_loc` is better isolated from `face_loc`: both use Python 3.10, but hand-tracking adds MediaPipe and its own OpenCV wheel set.

```bash
PYTHONNOUSERSITE=1 conda run -n gesture_loc python Software/Master/gesture_loc/main.py \
  --config Software/Master/gesture_loc/configs/default.yaml
```

If you want the local OpenCV preview window, add `--visualizer`. The environment uses
GUI-enabled `opencv-contrib-python`, not the headless wheel. On macOS the preview is
displayed from a dedicated process so `cv2.imshow(...)` does not run inside the
tracking worker thread.

Query the latest result:

```bash
curl http://127.0.0.1:8012/latest | jq
```

## Run gesture actions

```bash
PYTHONNOUSERSITE=1 /home/ubuntu/anaconda3/bin/python Software/Master/gesture_loc/scripts/gesture_action_runner.py \
  --config Software/Master/gesture_loc/configs/actions.default.yaml
```

`gesture_action_runner.py` imports the local `soarmmoce_sdk`, so it should run in an SDK-ready Python with `draccus`, `kinpy`, `lerobot`, and `pyserial` available. The bundled `/home/ubuntu/anaconda3/bin/python` already has those installed on this machine.

## Notes

- The recognizer model will auto-download on first run when `allow_auto_download: true`.
- Actions trigger from `stable_gesture_name`, not raw per-frame predictions.
- Edit `configs/actions.default.yaml` to remap gestures to robot actions.
- The shipped default action map avoids gripper-only actions so it can run on the current arm without extra hardware.

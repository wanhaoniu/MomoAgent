# Smart Mirror Face Tracking

智能化妆镜的人脸检测与位置反馈项目。

当前默认运行链路针对 Mac CPU 做了简化：

- 默认检测后端：`opencv_yunet`
- 默认权重：`weights/face_detection_yunet_2023mar.onnx`
- 本地直接启动入口：`python main.py`
- OpenClaw 识别入口：仓库根目录 `SKILL.md`

项目仍保留 `insightface_onnx` 和 `insightface_faceanalysis` 两种可选后端，但默认不依赖它们。

## 目录

```text
.
├── SKILL.md
├── actions.schema.json
├── skill.manifest.json
├── main.py
├── configs/
├── logs/
├── runtime/
├── scripts/
├── src/
├── tests/
├── weights/
├── README.md
├── requirements.txt
└── pyproject.toml
```

## 环境安装

推荐使用项目内虚拟环境：

```bash
cd /Users/niuwanhao/Desktop/Intern/pp_skills/face_loc
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 直接启动

有窗口调试模式：

```bash
cd /Users/niuwanhao/Desktop/Intern/pp_skills/face_loc
source .venv/bin/activate
python main.py --config configs/default.yaml
```

无窗口模式：

```bash
cd /Users/niuwanhao/Desktop/Intern/pp_skills/face_loc
source .venv/bin/activate
python main.py --config configs/default.yaml --headless
```

USB 摄像头索引切换：

```bash
python main.py --config configs/default.yaml --source-type camera --camera-index 1
```

RTSP：

```bash
export RTSP_URL='rtsp://user:password@192.168.1.10:554/stream1'
python main.py --config configs/rtsp.yaml --headless
```

本地视频：

```bash
export VIDEO_FILE='/absolute/path/to/demo.mp4'
python main.py --config configs/video_file.yaml
```

## 默认配置说明

默认配置文件：[configs/default.yaml](/Users/niuwanhao/Desktop/Intern/pp_skills/face_loc/configs/default.yaml)

默认关键项：

- `source.type: camera`
- `detector.backend: opencv_yunet`
- `detector.model_path: ./weights/face_detection_yunet_2023mar.onnx`
- `detector.device: cpu`
- `selection.strategy: largest_face`
- `visualizer.enabled: true`

## 本地入口

根目录入口文件：[main.py](/Users/niuwanhao/Desktop/Intern/pp_skills/face_loc/main.py)

它会自动把 `src/` 加到 `PYTHONPATH`，所以不需要再手动写：

```bash
PYTHONPATH=src python -m face_tracking.main
```

直接执行：

```bash
python main.py
```

## API

服务启动后可访问：

- `GET /health`
- `GET /latest`
- `GET /status`
- `GET /frame.jpg`
- `WS /ws/stream`

预览接口详细说明见：

- [`FRAME_PREVIEW_API.md`](./FRAME_PREVIEW_API.md)

示例：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/latest | jq
curl http://127.0.0.1:8000/status | jq
curl "http://127.0.0.1:8000/frame.jpg?max_width=960&quality=70" -o frame.jpg
```

`/latest` 返回字段包括：

- `detected`
- `target_face.bbox`
- `target_face.center`
- `target_face.size`
- `target_face.area_ratio`
- `offset.dx/dy/ndx/ndy`
- `smoothed_offset.dx/dy/ndx/ndy`
- `distance_hint`
- `lateral_hint`
- `vertical_hint`
- `combined_hint`
- `fps`

## OpenClaw Skill

根目录 skill 文件：[SKILL.md](/Users/niuwanhao/Desktop/Intern/pp_skills/face_loc/SKILL.md)

支持动作：

1. `start_face_tracking`
2. `get_face_tracking_result`
3. `get_face_tracking_status`
4. `stop_face_tracking`

本地等价命令：

```bash
python scripts/face_tracking_skill.py start_face_tracking --config configs/default.yaml --source-type camera --camera-index 0 --headless
python scripts/face_tracking_skill.py get_face_tracking_result
python scripts/face_tracking_skill.py get_face_tracking_status
python scripts/face_tracking_skill.py stop_face_tracking
```

动作定义文件：

- [actions.schema.json](/Users/niuwanhao/Desktop/Intern/pp_skills/face_loc/actions.schema.json)
- [skill.manifest.json](/Users/niuwanhao/Desktop/Intern/pp_skills/face_loc/skill.manifest.json)

## 可选后端

如果后续要切换到 `insightface`：

```bash
source .venv/bin/activate
pip install 'insightface>=0.7.3' 'onnxruntime>=1.19.0'
```

然后把配置改成：

- `detector.backend: insightface_onnx`
- 或 `detector.backend: insightface_faceanalysis`

## 测试

```bash
cd /Users/niuwanhao/Desktop/Intern/pp_skills/face_loc
source .venv/bin/activate
pytest tests -q
```

## 当前状态

已完成：

- 根目录 `main.py` 本地启动入口
- 根目录 `SKILL.md` OpenClaw skill 入口
- 默认可用 YuNet 权重下载到 `weights/face_detection_yunet_2023mar.onnx`
- `.venv` 基础依赖安装完成
- 默认配置切换到 Mac CPU 友好模式

## 后续建议

1. 接机械臂模块时，把 `combined_hint` 扩展成速度或位姿增量。
2. 如果想提高检测精度，再切到 `insightface_onnx`。
3. 如果需要长期运行，增加服务守护和监控指标。

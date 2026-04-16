# Frame Preview API

`face_loc` 新增了一个独立的 JPEG 预览接口，给手机 App 或前端页面拉取摄像头画面使用。

这一路和 `WS /ws/stream` 是分开的：

- `WS /ws/stream` 继续负责结构化的人脸追踪结果
- `GET /frame.jpg` 负责返回当前最新一帧的 JPEG 预览图

这样做的目的是让视频预览不混进主控制链路，前端可以按自己的频率单独拉图。

## 1. 接口说明

- Method: `GET`
- Path: `/frame.jpg`
- Default base URL: `http://<controller-ip>:8000`

完整示例：

```text
http://<controller-ip>:8000/frame.jpg?max_width=960&quality=70
```

如果需要调试版带框画面，可以显式加：

```text
http://<controller-ip>:8000/frame.jpg?max_width=960&quality=70&overlay=1
```

## 2. 适用场景

- 手机端显示镜前摄像头当前画面
- Web 前端做低带宽预览
- 调试追踪状态时快速查看实时视图

这个接口返回的是“当前最新一帧”，不是视频推流协议，也不是 WebSocket 视频流。

## 3. Query 参数

### `max_width`

- 类型：`int`
- 默认值：`640`
- 实际生效范围：`64` 到 `1920`

含义：

- 如果原图宽度大于 `max_width`，服务端会按比例缩小后再编码成 JPEG
- 如果原图宽度本来就不大于 `max_width`，则不会放大

推荐值：

- 手机预览：`720` 到 `960`
- 低带宽场景：`480` 到 `640`
- 局域网调试：`960` 到 `1280`

### `quality`

- 类型：`int`
- 默认值：`72`
- 实际生效范围：`35` 到 `92`

含义：

- 控制 JPEG 压缩质量
- 值越高，画质越好，但带宽占用越大

推荐值：

- 手机 MVP 预览：`65` 到 `75`
- 更省流量：`50` 到 `60`
- 画质优先：`80` 到 `88`

### `overlay`

- 类型：`bool`
- 默认值：`false`

含义：

- `false`：返回纯摄像头画面，不叠加人脸框和提示信息
- `true`：返回调试版画面，包含 visualizer 叠加内容

推荐：

- 手机 App 正式预览：保持默认 `false`
- 本地调试识别效果：临时传 `overlay=1`

## 4. 成功响应

### Status Code

- `200 OK`

### Content-Type

```text
image/jpeg
```

### 响应头

#### `Cache-Control`

固定返回：

```text
no-store, no-cache, must-revalidate, max-age=0
```

表示这是实时预览图，不建议客户端缓存。

#### `X-Frame-Id`

- 类型：字符串形式的整数
- 含义：当前预览帧的递增编号

可用于：

- 判断客户端拿到的是否是新图
- 调试预览是否停住
- 做简单的“新帧到达”检测

#### `X-Frame-Overlay`

- `0`：当前返回的是纯画面
- `1`：当前返回的是带叠加层的调试画面

### Body

- JPEG 二进制内容

## 5. 错误响应

### `503 Service Unavailable`

出现条件：

- 服务已经启动，但当前还没有拿到可用画面

响应体：

```text
No frame available yet
```

常见原因：

- 摄像头刚启动
- 视频源正在重连
- 当前还没有处理出第一帧

### `500 Internal Server Error`

出现条件：

- 服务端拿到帧了，但 JPEG 编码失败

响应体：

```text
Failed to encode preview frame
```

## 6. 返回图像内容说明

这个接口返回的是服务端维护的“最新预览帧”。

行为上有两个特点：

- 服务端同时维护“原始画面”和“带叠加画面”
- 即使 `visualizer` 关闭，服务端也会持续维护最新预览帧

所以：

- `headless` 模式下也可以正常拉 `/frame.jpg`
- 默认返回纯画面
- 只有在 `overlay=1` 且 `visualizer.enabled=true` 时，才会看到人脸框和提示信息

## 7. 前端接入建议

### 轮询频率

MVP 阶段建议直接轮询，不必先上更重的视频协议。

推荐区间：

- `250ms` 到 `500ms` 一次

经验值：

- `350ms` 一次通常比较平衡
- 在局域网里足够看清当前镜前状态
- 带宽和延迟都会明显低于真正的视频推流

### 缓存规避

建议每次请求都带一个时间戳参数，避免中间层缓存：

```text
/frame.jpg?max_width=960&quality=70&t=1710000000000
```

### 带宽估算

实际带宽取决于画面复杂度、`max_width`、`quality` 和轮询频率。

一个典型 MVP 配置：

- `max_width=960`
- `quality=70`
- `350ms` 轮询一次

通常适合手机局域网预览，不会接近真正视频流的持续带宽占用。

## 8. 调试示例

### curl 保存一帧

```bash
curl "http://127.0.0.1:8000/frame.jpg?max_width=960&quality=70" -o frame.jpg
```

### curl 保存一帧带框调试图

```bash
curl "http://127.0.0.1:8000/frame.jpg?max_width=960&quality=70&overlay=1" -o frame_overlay.jpg
```

### 查看响应头

```bash
curl -I "http://127.0.0.1:8000/frame.jpg?max_width=960&quality=70"
```

### 浏览器直接访问

```text
http://127.0.0.1:8000/frame.jpg?max_width=960&quality=70
```

## 9. 相关接口

- `GET /health`
- `GET /status`
- `GET /latest`
- `WS /ws/stream`

推荐职责分工：

- `/latest`：拿结构化追踪结果
- `/frame.jpg`：拿预览图
- `/ws/stream`：持续订阅追踪数据

## 10. 当前 App 默认接法

当前 Android MVP 默认使用：

```text
http://<host>:8000/frame.jpg?max_width=960&quality=70
```

并额外附加一个时间戳参数：

```text
&t=<current-millis>
```

这样可以直接满足手机端最小预览需求。

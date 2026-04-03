---
name: soarmmoce-real-con
description: 使用本技能通过本地 Python 脚本或本地 SDK 控制真实 soarmMoce 机械臂；适用于自然语言动作控制、连续小步笛卡尔移动、回零与状态查询。2/3/5 号关节按减速比适配并运行在 mode 0 绝对位置配置，1/4 为单圈标定关节，可选夹爪单独处理。
metadata:
  openclaw:
    emoji: "🤖"
    requires:
      bins: ["python3"]
---

# soarmmoce-real-con

## 功能概览

- 本技能用于直接控制本机串口连接的真实 `soarmMoce` follower arm。
- 代码结构分成两层：
  - `scripts/soarmmoce_sdk.py`：SDK 风格控制逻辑
  - `scripts/soarmmoce_state.py` / `scripts/soarmmoce_move.py`：命令行入口
- 技能包内已自带运行所需的 `resources/urdf` 和 `resources/meshes`，默认不再依赖仓库根目录里的 SDK 资源。
- 当前 TCP 笛卡尔控制按 `5DOF position-only IK` 工作，只解末端位置 `x/y/z`，不再强行约束完整 6D 姿态。
- 对 `delta/xyz` 这类笛卡尔移动，默认锁住 `wrist_flex + wrist_roll`，避免 4/5 号在只解 `xyz` 时自己乱补姿态。
- 轨迹下发默认按时间频率连续插值，并使用平滑缓入缓出，避免单步跳变太大导致动作发卡。
- 2 号关节 `shoulder_lift`、3 号关节 `elbow_flex`、5 号关节 `wrist_roll` 底层统一运行在 `mode 0`；其中 2/3/5 通过 `Min/Max_Position_Limit=0` 加 `18(Phase)=28` 读取绝对位置值。
- 1/4 使用单圈标定，采用半圈归中后手动扫范围的方式记录 `homing_offset + range_min/max`。
- 夹爪如果存在，可以单独标定；不要把夹爪标定和 2/3/5 的绝对位置逻辑混在一起。

## 何时使用

- 用户说：`把机械臂抬高一点`、`往前一点`、`再来一点`、`回零`
- 用户要求状态查询：`当前机械臂在哪`
- 用户要执行多步动作，但不要求你生成正式项目代码

## 核心规则

1. 不要用 node 工具。
2. 脚本返回 JSON 仅供内部判断。
3. 默认最终回复只给用户自然语言。
4. 对普通空间动作，不要反问哪个关节、多少度。
5. 不要调用任何 gripper/open/close/set_gripper 相关命令。

## 推荐调用方式

### 1) 读取状态

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_state.py
```

### 1.1) 只读 IK 诊断

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_diag_ik.py --dx 0.02 --frame base
```

### 1.2) 标定

运行前先确认：
- `2/3/5` 不参与传统单圈标定，只会被写成固定的绝对位置配置
- 需要真正手动标定的是 `1/4`

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_calibrate.py
```

如果只想先生成 JSON，不回写寄存器：

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_calibrate.py --apply-registers false
```

### 2) 小步笛卡尔移动

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_move.py delta --dz 0.01 --frame base
```

需要排查多圈关节执行误差时，给运动命令加 `--trace`：

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_move.py delta --dy 0.05 --frame base --duration 2.0 --trace
```

### 3) 绝对 XYZ 移动

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_move.py xyz --x 0.22 --y 0.00 --z 0.18
```

### 4) 关节级低层修正

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_move.py joint --joint wrist_roll --delta-deg 5
```

### 5) 回零

多圈关节现在直接使用硬件 mode 0 的绝对位置值，并始终以标定 JSON 中记录的 `home_present_raw` 作为基准；上电后可直接执行：

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_move.py home
```

### 6) 人脸居中跟随

如果用户说开启人脸跟随模式，或者跟随我的脸等请运行：

```bash
/Users/moce/miniforge3/bin/conda run -n soarmmoce python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_face_follow.py --face-endpoint http://127.0.0.1:8000
```

当前默认行为：

- `shoulder_pan` 做水平居中
- `shoulder_lift` 做垂直居中
- 现在只做画面内 `x/y` 居中，不再做前后距离修正
- 同一帧不会重复下指令，默认会做轻量平滑，减少抽搐
- 丢失人脸时不报错，改为自动左右扫描寻找人脸
- 脚本会持续运行，直到你手动 `Ctrl+C` 停止

## SDK 直接调用

```python
from soarmmoce_sdk import SoArmMoceController

arm = SoArmMoceController()
arm.move_delta(dz=0.01, frame="base")
print(arm.get_state())
```

运行临时脚本时建议显式带上：

```bash
PYTHONPATH=~/.openclaw/skills/soarmmoce-real-con/scripts python3 /tmp/soarmmoce_sequence.py
```

## 可用 API

- `SoArmMoceController().read()`
- `SoArmMoceController().get_state()`
- `SoArmMoceController().move_delta(...)`
- `SoArmMoceController().move_to(...)`
- `SoArmMoceController().move_joint(...)`
- `SoArmMoceController().move_joints(...)`
- `SoArmMoceController().home()`
- `SoArmMoceController().stop()`

以下接口会直接报错，因为当前硬件没有夹爪舵机：

- `SoArmMoceController().set_gripper(...)`
- `SoArmMoceController().open_gripper()`
- `SoArmMoceController().close_gripper()`

## 环境变量

- `SOARMMOCE_PORT`：串口，默认 `/dev/ttyACM0`
- `SOARMMOCE_ROBOT_ID`：标定 ID，默认优先 `soarmmoce`，找不到再回退 `follower_moce`
- `SOARMMOCE_CALIB_DIR`：标定目录，默认使用 `skills/soarmmoce-real-con/calibration`
- `SOARMMOCE_URDF_PATH`：URDF 路径，默认使用 `skills/soarmmoce-real-con/resources/urdf/soarmoce_urdf.urdf`
- `SOARMMOCE_TARGET_FRAME`：末端 frame，默认 `wrist_roll`（按当前 5DOF 链截断）
- `SOARMMOCE_JOINT_SCALE_JSON`：覆盖关节减速比/方向，默认 `{"shoulder_pan":-1.0,"shoulder_lift":-5.3,"elbow_flex":5.6,"wrist_flex":1.0,"wrist_roll":1.0}`
- `SOARMMOCE_MODEL_OFFSETS_JSON`：覆盖 URDF 模型角度偏置，默认 `{"shoulder_pan":0.0,"shoulder_lift":0.0,"elbow_flex":0.0,"wrist_flex":0.0,"wrist_roll":0.0}`
- `SOARMMOCE_LINEAR_STEP_M`：笛卡尔插值步长，默认 `0.01`
- `SOARMMOCE_JOINT_STEP_DEG`：关节插值步长，默认 `5.0`
- `SOARMMOCE_CARTESIAN_UPDATE_HZ`：笛卡尔轨迹下发频率，默认 `20.0`
- `SOARMMOCE_JOINT_UPDATE_HZ`：关节轨迹下发频率，默认 `25.0`
- `SOARMMOCE_MAX_EE_POS_ERR_M`：笛卡尔动作最终位置误差容忍，默认 `0.02`
- `SOARMMOCE_IK_TARGET_TOL_M`：5DOF IK 收敛阈值，默认 `0.001`
- `SOARMMOCE_IK_MAX_ITERS`：5DOF IK 最大迭代次数，默认 `200`
- `SOARMMOCE_IK_DAMPING`：5DOF IK 阻尼系数，默认 `0.05`
- `SOARMMOCE_IK_STEP_SCALE`：5DOF IK 每轮步进缩放，默认 `0.8`
- `SOARMMOCE_IK_JOINT_STEP_DEG`：5DOF IK 单轮单关节最大步长，默认 `8.0`
- `SOARMMOCE_IK_SEED_BIAS`：5DOF IK 保持当前姿态的偏置强度，默认 `0.02`
- `SOARMMOCE_ARM_P_COEFFICIENT`：单圈关节 P 参数，默认 `16`
- `SOARMMOCE_ARM_D_COEFFICIENT`：单圈关节 D 参数，默认 `8`

## 执行策略

默认优先级：
1. `soarmmoce_state.py`
2. `soarmmoce_move.py delta`
3. `soarmmoce_move.py xyz`
4. `soarmmoce_move.py home`
5. SDK 临时脚本
6. `joint` / `joints` 仅作低层兜底

对 `delta` 相对移动，`frame="base"` 默认与正式 SDK/仿真一致，直接使用 URDF 原始基坐标。
如果你想用更直觉的用户坐标，再显式使用 `frame="user"`，它按 `x=前后`、`y=左右`、`z=上下` 解释。
`frame="urdf"` 是 `base` 的显式别名。
只有用户明确要求沿末端当前方向前进/后退时，才优先使用 `frame="tool"`。

串口相关脚本不要并行运行；同一时刻只保留一个 `state/move/diag` 进程，否则容易出现假性的缺电机 ID 或端口占用。

标定脚本同样不要和其它串口脚本并行运行。

## 参考文件

- `skills/soarmmoce-real-con/scripts/soarmmoce_sdk.py`
- `skills/soarmmoce-real-con/scripts/soarmmoce_calibrate.py`
- `skills/soarmmoce-real-con/scripts/soarmmoce_state.py`
- `skills/soarmmoce-real-con/scripts/soarmmoce_diag_ik.py`
- `skills/soarmmoce-real-con/scripts/soarmmoce_move.py`
- `skills/soarmmoce-real-con/resources/urdf/soarmoce_urdf.urdf`
- `skills/soarmmoce-real-con/resources/meshes`
- `skills/soarmmoce-real-con/agents/openai.yaml`

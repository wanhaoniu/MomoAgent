---
name: soarmmoce-real-con
description: 使用本技能通过本地 Python 脚本或本地 SDK 控制真实 soarmMoce 机械臂；适用于自然语言动作控制、连续小步笛卡尔移动、回零与状态查询。2/3 号关节按减速比 5.3/5.6 适配并运行在多圈模式，无夹爪舵机。
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
  - `scripts/soarm101_sdk.py`：SDK 风格控制逻辑
  - `scripts/soarm101_state.py` / `scripts/soarm101_move.py`：命令行入口
- 2 号关节 `shoulder_lift` 与 3 号关节 `elbow_flex` 已分别按减速比 `5.3 / 5.6` 做换算，底层运行在多圈模式。
- 当前机械臂没有安装 6 号夹爪舵机，不要调用夹爪脚本或夹爪 API。

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
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarm101_state.py
```

### 2) 小步笛卡尔移动

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarm101_move.py delta --dz 0.01 --frame base
```

### 3) 绝对 XYZ 移动

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarm101_move.py xyz --x 0.22 --y 0.00 --z 0.18
```

### 4) 关节级低层修正

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarm101_move.py joint --joint wrist_roll --delta-deg 5
```

### 5) 回零

```bash
python3 ~/.openclaw/skills/soarmmoce-real-con/scripts/soarm101_move.py home
```

## SDK 直接调用

```python
from soarm101_sdk import SoArm101Controller

arm = SoArm101Controller()
arm.move_delta(dz=0.01, frame="base")
print(arm.get_state())
```

运行临时脚本时建议显式带上：

```bash
PYTHONPATH=~/.openclaw/skills/soarmmoce-real-con/scripts python3 /tmp/soarmmoce_sequence.py
```

## 可用 API

- `SoArm101Controller().read()`
- `SoArm101Controller().get_state()`
- `SoArm101Controller().move_delta(...)`
- `SoArm101Controller().move_to(...)`
- `SoArm101Controller().move_joint(...)`
- `SoArm101Controller().move_joints(...)`
- `SoArm101Controller().home()`
- `SoArm101Controller().stop()`

以下接口会直接报错，因为当前硬件没有夹爪舵机：

- `SoArm101Controller().set_gripper(...)`
- `SoArm101Controller().open_gripper()`
- `SoArm101Controller().close_gripper()`

## 环境变量

- `SOARMMOCE_PORT`：串口，默认 `/dev/ttyACM0`
- `SOARMMOCE_ROBOT_ID`：标定 ID，默认 `follower_moce`
- `SOARMMOCE_CALIB_DIR`：标定目录
- `SOARMMOCE_URDF_PATH`：URDF 路径
- `SOARMMOCE_TARGET_FRAME`：末端 frame，默认 `gripper_frame_link`
- `SOARMMOCE_HOME_JOINTS_JSON`：覆盖 home 目标关节
- `SOARMMOCE_JOINT_SCALE_JSON`：覆盖关节减速比/方向，默认 `{"shoulder_pan":1.0,"shoulder_lift":5.3,"elbow_flex":5.6,"wrist_flex":1.0,"wrist_roll":1.0}`
- `SOARMMOCE_LINEAR_STEP_M`：笛卡尔插值步长，默认 `0.01`
- `SOARMMOCE_JOINT_STEP_DEG`：关节插值步长，默认 `5.0`
- `SOARMMOCE_MAX_EE_POS_ERR_M`：IK 位置误差容忍，默认 `0.03`
- `SOARMMOCE_ARM_P_COEFFICIENT`：单圈关节 P 参数，默认 `16`
- `SOARMMOCE_ARM_D_COEFFICIENT`：单圈关节 D 参数，默认 `8`

## 执行策略

默认优先级：
1. `soarm101_state.py`
2. `soarm101_move.py delta`
3. `soarm101_move.py xyz`
4. `soarm101_move.py home`
5. SDK 临时脚本
6. `joint` / `joints` 仅作低层兜底

对 `上/下/左/右` 默认使用 `frame="base"`。
只有用户明确要求沿末端当前方向前进/后退时，才优先使用 `frame="tool"`。

## 参考文件

- `skills/soarmmoce-real-con/scripts/soarm101_sdk.py`
- `skills/soarmmoce-real-con/scripts/soarm101_state.py`
- `skills/soarmmoce-real-con/scripts/soarm101_move.py`
- `skills/soarmmoce-real-con/agents/openai.yaml`

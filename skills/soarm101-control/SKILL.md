---
name: soarm101-control
description: 使用本技能通过本地 Python 脚本或本地 SDK 控制真实 soarm101 机械臂；适用于自然语言动作控制、连续小步笛卡尔移动、夹爪控制与状态查询。脚本返回 JSON 仅供内部判断，默认最终只向用户输出自然语言结果。
metadata:
  openclaw:
    emoji: "🤖"
    requires:
      bins: ["python3"]
---

# soarm101-control

## 功能概览

- 本技能用于**直接控制本机串口连接的真实 `soarm101` follower arm**。
- 代码结构分成两层：
  - `scripts/soarm101_sdk.py`：仅负责 SDK 风格控制逻辑
  - `scripts/soarm101_state.py` / `scripts/soarm101_move.py` / `scripts/soarm101_gripper.py`：命令行入口
- OpenClaw 可以直接调用这些脚本，也可以在复杂场景下写一个临时 Python 脚本导入 SDK。
- SDK 已移除软件侧的位移/关节变化硬限制，默认通过内部插值让运动更平滑。
- SDK 在动作完成后会自动把当前位置重新写回目标位，尽量减少到位后的持续抖动。

## 何时使用本技能

- 用户说：`把机械臂抬高一点`、`往前一点`、`再来一点`、`夹爪打开`、`回零`
- 用户要求状态查询：`当前机械臂在哪`
- 用户要执行多步动作，但不要求你生成正式项目代码

如果用户明确要“生成控制脚本/程序文件”，那更偏代码生成任务，不是本技能的主路径。

## 核心规则

1. **不要用 node 工具。**
2. **脚本返回 JSON 仅供内部判断。**
3. **默认最终回复只给用户自然语言。**
4. 对普通空间动作，**不要反问哪个关节、多少度**。
5. 一次用户请求允许**多次调用脚本**后再统一回答。

## 推荐调用方式

### 1) 读取状态

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_state.py
```

### 2) 小步笛卡尔移动

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_move.py delta --dz 0.01 --frame base
```

大位移也可以直接一次下达，SDK 会内部插值：

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_move.py delta --dz 0.20 --frame base --duration 4
```

### 3) 绝对 XYZ 移动

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_move.py xyz --x 0.22 --y 0.00 --z 0.18
```

说明：
- `delta` 的默认坐标系是 `base`
- `xyz` 也是 `base` 坐标系下的绝对目标位置
- 只有明确指定 `--frame tool` 时，`delta` 才按末端工具坐标系解释

### 4) 关节级低层修正

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_move.py joint --joint wrist_roll --delta-deg 5
```

### 5) 夹爪

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_gripper.py open
```

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_gripper.py close
```

```bash
python3 ~/.openclaw/skills/soarm101-control/scripts/soarm101_gripper.py set --open-ratio 0.4
```

## 脚本返回格式

所有命令行脚本统一返回：

成功：

```json
{"ok": true, "result": {...}, "error": null}
```

失败：

```json
{"ok": false, "result": null, "error": {"type": "...", "message": "..."}}
```

注意：
- 这个 JSON 是给 OpenClaw 内部读取的
- **默认不要原样转发给最终用户**

## SDK 直接调用

当动作较复杂、需要条件判断、或者需要多步连续控制时，OpenClaw 可以自己写一个临时 Python 脚本。

导入方式：

```python
from soarm101_sdk import SoArm101Controller
```

运行临时脚本时，建议显式带上 `PYTHONPATH`：

```bash
PYTHONPATH=~/.openclaw/skills/soarm101-control/scripts python3 /tmp/soarm101_sequence.py
```

最小示例：

```python
from soarm101_sdk import SoArm101Controller

arm = SoArm101Controller()
arm.move_delta(dz=0.01, frame="base")
arm.close_gripper()
print(arm.get_state())
```

SDK 常用方法：
- `SoArm101Controller().read()`
- `SoArm101Controller().get_state()`
- `SoArm101Controller().move_delta(...)`
- `SoArm101Controller().move_to(...)`
- `SoArm101Controller().move_joint(...)`
- `SoArm101Controller().move_joints(...)`
- `SoArm101Controller().open_gripper()`
- `SoArm101Controller().close_gripper()`
- `SoArm101Controller().set_gripper(...)`
- `SoArm101Controller().home()`
- `SoArm101Controller().stop()`

坐标系约定：
- `SoArm101Controller().move_delta(...)` 默认 `frame="base"`
- `SoArm101Controller().move_to(...)` 的 `x/y/z` 是 `base` 坐标系绝对位置
- 对用户说“上/下/左/右”时，默认按 `base` 理解
- 只有“沿当前工具方向前进/后退”这类语义才优先用 `tool`

默认平滑参数：
- `SOARM101_LINEAR_STEP_M`：笛卡尔插值步长，默认 `0.01`
- `SOARM101_JOINT_STEP_DEG`：关节插值步长，默认 `5.0`
- `SOARM101_MAX_EE_POS_ERR_M`：IK 位置误差容忍，默认 `0.03`
- `SOARM101_ARM_P_COEFFICIENT`：机械臂关节 P 参数，默认 `16`
- `SOARM101_ARM_D_COEFFICIENT`：机械臂关节 D 参数，默认 `8`

注意：这些是平滑参数，不是安全限幅。
其中 `SOARM101_MAX_EE_POS_ERR_M` 是 IK 求解容忍度；如果机械臂经常因为位置误差略大而失败，可以继续适当调大。
如果机械臂到位后还在轻微抖动，可以继续适当调低 `P/D`。

## 执行策略

默认优先级：
1. `soarm101_state.py`
2. `soarm101_move.py delta`
3. `soarm101_move.py xyz`
4. `soarm101_gripper.py`
5. `soarm101_move.py home`
6. SDK 临时脚本
7. `joint` / `joints` 仅作低层兜底

对于以下请求优先使用 `delta`：
- `抬高一点`
- `降低一点`
- `再来一点`
- `末端往前一点`
- `向上移动 20cm`

默认情况下：
- `delta` 按 `base` 坐标系解释
- `xyz` 也按 `base` 坐标系解释
- 如果用户没特别说明，不要默认切到 `tool`

只有用户明确给出关节名或角度时，才用 `joint` 或 `joints`。

## `home` 的定义

当前 `home` 的定义是：
- 回到当前配置里的 **saved home pose**
- 现在这组 `home` 已经设置为你提供的当前 state：
  - `shoulder_pan=-8.923076923076923`
  - `shoulder_lift=-9.31868131868132`
  - `elbow_flex=8.483516483516484`
  - `wrist_flex=-3.6043956043956045`
  - `wrist_roll=-0.17582417582417584`
  - `gripper=25.766470971950422`

注意：
- 现在的 `home` 不再是 joint-zero pose
- 它就是一组固定保存的目标关节值
- 当前默认值来自你这次提供的 state

后续如果你还想改这组 home：
- 可以直接改 `scripts/soarm101_sdk.py` 里的默认值
- 或通过环境变量 `SOARM101_HOME_JOINTS_JSON` 覆盖

## 多次调用规则

可以连续多次调用脚本，但不再是必须。
典型场景：
- 用户说 `再来一点`
- 需要先读状态再决定下一步
- 需要分阶段完成动作

对于较大位移，优先先尝试一次命令，让 SDK 内部完成插值平滑；
只有在你想做更复杂的阶段控制时，再主动拆成多次调用。

## 用户回复规则

脚本返回给你的是内部结构化结果；你对用户说的是自然语言。

好的回复：
- `已向上抬高约 1 厘米。`
- `夹爪已打开。`
- `当前末端大约位于 x=0.03, y=-0.01, z=0.26 米。`

默认不要：
- 粘贴整个 JSON
- 输出完整 `state`
- 输出内部脚本调用细节

只有用户明确要求原始返回、调试输出、完整状态时，才展示 JSON。

## 安全规则

- `上/下/左/右` 默认用 `frame="base"`
- 工具相对前后运动才优先用 `frame="tool"`
- 若出现 `IKError`、`ValidationError`、`HardwareError`，如实汇报

## 参考文件

- `skills/soarm101-control/scripts/soarm101_sdk.py`
- `skills/soarm101-control/scripts/soarm101_state.py`
- `skills/soarm101-control/scripts/soarm101_move.py`
- `skills/soarm101-control/scripts/soarm101_gripper.py`
- `skills/soarm101-control/agents/openai.yaml`

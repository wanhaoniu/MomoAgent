# PhyAgentOS MomoAgent 插件

这个目录是一个 PhyAgentOS 外部插件草案，用于把 MomoAgent 机械臂以 **直接 SDK 控制** 的方式接入 PhyAgentOS。

它不要求启动 `momo_robot_service`，也不通过 HTTP API 控制机械臂。运行链路是：

```text
PhyAgentOS HAL Watchdog -> MomoAgentDriver -> runtime/momo_bridge.py -> soarmmoce_sdk -> 串口机械臂
```

## 目录结构

```text
integrations/phyagentos_momo_plugin/
├── PhyAgentOS_plugin.toml
├── pyproject.toml
├── phyagentos_momo_plugin/
│   ├── driver.py
│   └── profiles/momoagent.md
├── runtime/
│   ├── momo_bridge.py
│   ├── README.md
│   └── requirements.txt
└── tests/
```

## 推荐复制方式

如果这个插件保持在 MomoAgent 仓库内，不需要额外复制 SDK。

如果要做成独立插件仓库，至少复制：

```text
integrations/phyagentos_momo_plugin/  -> 新插件仓库根目录
sdk/                                  -> 新插件仓库 runtime/third_party/MomoAgent/sdk/
```

不要复制：

- `.env` / `env`
- API keys
- `__pycache__`
- `.pytest_cache`
- runtime session/log 文件
- 其他机器人机器上的 calibration/config，除非你确认硬件一致

## 安装依赖

建议使用 MomoAgent README 中的 Python 3.12 conda 环境。最小 direct SDK 依赖：

```bash
conda create -n momoagent -c conda-forge python=3.12 pip pyyaml pybullet -y
conda activate momoagent
python -m pip install -U pip
python -m pip install -e ./sdk lerobot ftservo-python-sdk feetech-servo-sdk numpy scipy kinpy
```

如果插件已被复制成独立仓库，并且 SDK 位于 `runtime/third_party/MomoAgent/sdk/`：

```bash
python -m pip install -e ./runtime/third_party/MomoAgent/sdk
python -m pip install -r runtime/requirements.txt
```

## 本地调试

在插件根目录：

```bash
python runtime/momo_bridge.py preflight --pretty
python runtime/momo_bridge.py state --pretty
python runtime/momo_bridge.py joint_delta --params-json '{"joint":"shoulder_pan","delta_deg":3}' --pretty
```

## 部署到 PhyAgentOS

根据 PhyAgentOS 插件文档，插件仓库根目录必须有 `PhyAgentOS_plugin.toml`。本目录已经具备这个结构。

如果 PhyAgentOS 主仓库的部署脚本支持本地插件路径，可以在 PhyAgentOS 主仓库中执行类似命令：

```bash
python scripts/deploy_rekep_real_plugin.py \
  --repo-url ../MomoAgent/integrations/phyagentos_momo_plugin \
  --no-install-deps
```

然后启动：

```bash
python hal/hal_watchdog.py --driver momoagent --workspace ~/.PhyAgentOS/workspace
```

如果你的 PhyAgentOS 版本不提供通用插件部署脚本，可以参考它的插件开发文档中 `hal.plugins.register_plugin()` 的方式，把这个插件的 `PhyAgentOS_plugin.toml` 注册到本地 registry。

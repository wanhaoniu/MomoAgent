# MomoAgent 🤖
> 🏆 **荣获清华大学黑客松二等奖！**（[查看证书](images/prize.pdf)）
>
> 基于 SOARM101 的二次开发增强版机械臂：更高负载、更大工作空间、同等控制方式与精度体验  

<p align="center">
  <img src="images/moceai.jpg" alt="MoceAI" height="60">
  <span style="font-size: 24px; margin: 0 15px;">&times;</span>
  <img src="images/feetech.png" alt="Feetech" height="60">
  <br>
  <em>MoceAI 与 Feetech 联合开发</em>
</p>

> **开源计划：2026 年 3 月**（代码/硬件资料将在开源日统一放出）

> **主页：** https://arm.moce.ai/

[English](README.md) | [中文](README_ZH.md)

![MomoAgent Model Overview](images/1.JPG)
<!-- TODO: 替换为你的“模型总览”图片路径，例如 docs/media/overview.jpg -->

---

## 1. 项目简介
**MomoAgent** 是我们在 **SOARM101** 基础上进行的二次开发版本：在保持 **相同 5 自由度（DOF）架构** 与 **Python + ROS 控制方式** 不变的前提下，通过对关键关节引入**金属减速模组强化**，显著提升了承载能力与结构刚度，同时扩大了工作空间覆盖范围。

### 研发初衷
我们最初是想给自己的 "mystery robot" 做一个 **affordable portable robotic arm**（经济实惠的便携式机械臂），但发现市场上没有可以满足的产品。因此，**MoceAI** 在 SO-ARM 舵机供应商 **飞特 (Feetech)** 的支持下，联合发布了这款开源机械臂。

本项目面向：
- 创客与开源硬件开发者（快速二次开发、功能扩展）
- 教育/实验室教学（ROS/运动学/控制/视觉课程配套）
- 轻量级应用与原型验证（抓取、摆放、交互展示等）

---

## 2. 外观与结构展示（图片位预留）
### 2.1 模型总览
![Model Overview](images/4.JPG)
<!-- TODO: 模型总览图 -->

### 2.2 SOARM101 vs MomoAgent 对比图
![SOARM101 vs MomoAgent Comparison](images/3.JPG)
<!-- TODO: 对比图（建议包含：负载/工作空间/结构强化点） -->

### 2.3 核心部位特写（关键关节金属减速模组）
![Key Module Close-up](images/2.JPG)
<!-- TODO: 核心部位特写（建议标注：关键关节、减速模组、安装位） -->

---

## 3. 升级亮点（相对 SOARM101）
- **负载能力跃升**：通过关键关节金属减速模组强化，实现负载能力显著提升（实际实验获得）。
- **工作空间扩大**：基于公开 URDF 仿真评估，工作空间面积提升接近 30%。
- **结构刚度与稳定性增强**：强化结构带来更强抗扭与抗变形能力，提升系统稳定性。
- **精度与控制习惯保持一致**：重复定位精度保持 1 mm，控制方式保持 Python + ROS，不改变上手成本。
- **生态更完整**：兼容 **LeRobot** 上游生态，并扩展 **Moce 自有生态支持**。

---

## 4. 核心指标对比（SOARM101 vs MomoAgent）
> 以下数据整理自项目对比材料：负载为实验结果，工作空间相关为 URDF 仿真结果。

| 核心指标 | SOARM101 | MomoAgent | 变化 |
|---|---:|---:|---:|
| 额定最大负载 (kg) | 0.3 | 1.5 | **3×** 提升 |
| 极限负载 (kg) | – | 2.0 | 更高承载冗余 |
| 重复定位精度 (mm) | 1.0 | 1.0 | 保持一致 |
| 最大水平工作半径 Rmax (mm) | 380.6 | 433.1 | +13.8% |
| 最大三维可达距离 Dmax (mm) | 447.2 | 516.2 | +15.4% |
| Z 轴最大高度 (mm) | 428.7 | 502.9 | +17.3% |
| XY 工作空间面积 (m²) | 0.3255 | 0.4226 | +29.8% |
| 结构材料 | 标准 3D 打印结构 | 强化 3D 打印 + 金属减速模组 | 刚度更强 |
| 关键关节设计 | 常规驱动结构 | 双关节金属减速强化设计 | 扭矩放大 |
| 自由度 (DOF) | 5 | 5 | 架构一致 |
| 末端支持 | 通用末端接口 | 模块化定制末端接口 | 扩展更强 |
| 控制方式 | Python + ROS | Python + ROS | 一致 |
| 生态支持 | LeRobot | LeRobot 兼容 + Moce 生态 | 更完整 |
| 模块化维护 | 标准结构维护 | 关键关节可升级替换 | 可维护性增强 |

---

## 5. 仓库内容（开源后将补全）
> **提示：本仓库将在 2026 年 3 月开源日补齐以下内容。**

预计包含：
- `hardware/`：BOM、结构件清单、加工/打印建议、装配说明
- `urdf/`：URDF、网格模型、惯量/关节参数
- `ros/`：ROS 包（launch、控制、示例）
- `sdk/`：Python 控制接口、示例脚本、API 文档
- `docs/`：校准流程、常见问题、开发指南
- `examples/`：轨迹跟随、示教记录、抓取 demo（可选）

## 5.1 新手讲解资料

如果你需要给 0 基础同学介绍这个仓库、讲清机械臂控制链路，或者带大家做第一次上手演示，建议先看：

- [docs/0基础机械臂快速入门.md](docs/0基础机械臂快速入门.md)

---

## 6. 快速开始
### 6.1 环境要求
- 推荐 Ubuntu 20.04/22.04（主从串口与摄像头链路主要面向 Linux）
- 推荐 Python 3.10（SDK 最低支持 Python 3.8）
- 实机模式需要 1 套 Leader + 1 套 Follower，并准备可用串口（如 `/dev/ttyACM0`）
- 网络端口：`6666/TCP`（控制）与 `6000/UDP`（摄像头推流，可选）

### 6.2 安装依赖
在仓库根目录执行：
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ./sdk

# 主从控制运行依赖
pip install draccus opencv-python PyQt5 lerobot

# 可选（仿真 / 3D 视图）
pip install pybullet vtk
```

### 6.3 启动主从遥操作（实机）
1. 在从臂侧设备启动服务端：
```bash
cd Software/Slave
python3 main.py
```
2. 在主控 PC 启动客户端：
```bash
cd Software/Master
python3 main.py --ip <从臂IP> --port 6666 --leader-port /dev/ttyACM0 --leader-id black_arm_leader
```
3. 内置命令包括：
`savepos`、`goto`、`record`、`play`、`home`、`quit`

注意：
- 若摄像头目标 IP 或设备路径不同，请修改 `Software/Slave/main.py` 中的 `TARGET_PC_IP` 和 `CAM1_PATH`。
- 标定文件位于 `Software/Master/calibration/...` 与 `Software/Slave/calibration/...`。
- 若仅控制机械臂不看视频，可在主控端加 `--no-cam`。

### 6.4 启动图形界面（可选）
```bash
cd Software/Master
python3 gui.py
```
在 Settings 页面填写 IP/端口后点击 **Connect**。
如有问题欢迎提交 issue。

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-page HMI main window."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
import base64
from collections import deque
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PyQt5.QtCore import QEvent, QSize, QTimer, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from hmi.camera_window import CameraWindow
from hmi.pages import QuickMovePage, SettingsPage
from hmi.speech_window import SpeechInputWindow
from hmi.theme import apply_soft_effects, get_stylesheet
from hmi.widgets import GlobalStatusBar

try:
    from hmi.vtk_robot_view import VtkRobotView

    VTK_VIEW_AVAILABLE = True
    _VTK_VIEW_IMPORT_ERROR = None
except Exception as _vtk_e:
    VtkRobotView = None
    VTK_VIEW_AVAILABLE = False
    _VTK_VIEW_IMPORT_ERROR = _vtk_e

try:
    import pybullet as _pb

    PYBULLET_AVAILABLE = True
    _PYBULLET_IMPORT_ERROR = None
except Exception as _e:
    _pb = None
    PYBULLET_AVAILABLE = False
    _PYBULLET_IMPORT_ERROR = _e

try:
    from v4l2_camera_reader import V4L2CameraReader

    V4L2_CAMERA_AVAILABLE = True
    _V4L2_CAMERA_IMPORT_ERROR = None
except Exception as _v4l2_e:
    V4L2CameraReader = None
    V4L2_CAMERA_AVAILABLE = False
    _V4L2_CAMERA_IMPORT_ERROR = _v4l2_e

PICTURE_DIR = Path(__file__).resolve().parents[1] / "Picture"
REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO_PATH = PICTURE_DIR / "logo.png"
MOCEAI_LIGHT_PATH = PICTURE_DIR / "moceai.png"
MOCEAI_DARK_PATH = PICTURE_DIR / "moceai_Drak.jpg"
SPEECH_ICON_PATHS = (
    REPO_ROOT / "images" / "speech.png",
    REPO_ROOT / "images" / "speech.pngg",
    PICTURE_DIR / "speech.png",
)

HEADER_ICON_FILES = {
    "home": (
        "home_24dp_000000_FILL0_wght400_GRAD0_opsz24.svg",
        "home_24dp_FFFFFF_FILL0_wght400_GRAD0_opsz24.svg",
    ),
    "job": (
        "Job_24dp_000000_FILL0_wght400_GRAD0_opsz24.svg",
        "Job_24dp_FFFFFF_FILL0_wght400_GRAD0_opsz24.svg",
    ),
    "settings": (
        "settings_24dp_000000_FILL0_wght400_GRAD0_opsz24.svg",
        "settings_24dp_FFFFFF_FILL0_wght400_GRAD0_opsz24.svg",
    ),
}

SIM_RENDER_SUPERSAMPLE = 1.4

SDK_SRC = REPO_ROOT / "sdk" / "src"
DEFAULT_SDK_REAL_CONFIG_PATH = SDK_SRC / "soarmmoce_sdk" / "resources" / "configs" / "soarm_moce_serial.yaml"
if SDK_SRC.exists():
    _sdk_src_str = str(SDK_SRC)
    _normalized_sdk_src = os.path.normpath(_sdk_src_str)
    sys.path[:] = [
        path
        for path in sys.path
        if os.path.normpath(path or os.curdir) != _normalized_sdk_src
    ]
    sys.path.insert(0, _sdk_src_str)

if TYPE_CHECKING:
    from soarmmoce_sdk import Robot as SDKRobot
else:
    SDKRobot = Any

try:
    from soarmmoce_sdk import resolve_config as resolve_sdk_config
    from soarmmoce_sdk.runtime_compat import CompatibleRuntimeRobot as RuntimeRobot

    SDK_AVAILABLE = True
    _SDK_IMPORT_ERROR = None
except Exception as _sdk_e:
    RuntimeRobot = None
    resolve_sdk_config = None
    SDK_AVAILABLE = False
    _SDK_IMPORT_ERROR = _sdk_e


class VideoThread(QThread):
    """Video polling thread."""

    frame_ready = pyqtSignal(np.ndarray, float, float)  # frame, latency, fps

    def __init__(self, video_client):
        super().__init__()
        self.video_client = video_client
        self.running = True

    def run(self):
        while self.running:
            if self.video_client:
                frame, latency, fps = self.video_client.get_latest()
                if frame is not None:
                    self.frame_ready.emit(frame, latency, fps)
            time.sleep(0.033)

    def stop(self):
        self.running = False
        self.wait()


class ArmControlGUI(QMainWindow):
    """Refactored HMI window with page navigation and camera tool window."""

    log_signal = pyqtSignal(str, str)
    sdk_command_state_ready = pyqtSignal(object, str)
    sdk_command_failed = pyqtSignal(str, str)
    sdk_sync_state_ready = pyqtSignal(object)
    sdk_connection_finished = pyqtSignal(bool, str)

    PAGE_QUICK_MOVE = 0
    PAGE_SETTINGS = 1
    PAGE_HOME = PAGE_QUICK_MOVE

    def __init__(self):
        super().__init__()

        self.video_client = None
        self.video_thread: Optional[VideoThread] = None
        self.camera_window: Optional[CameraWindow] = None
        self.speech_window: Optional[SpeechInputWindow] = None
        self.connected = False
        self.connecting = False

        self.current_lang = "zh"
        self.current_theme = "light"
        self._translations = self._build_translations()
        self._lang_syncing = False

        self.mode_text = "Manual"
        self.robot_state_text = "Normal"
        self.speed_percent = 50
        self.control_owner_text = "Local"
        self.last_rtt_text = "--"

        self._sim_ready = False
        self._sim_updating = False
        self._sim_backend = "none"
        self._sim_pb_client = None
        self._sim_pb_robot_id = None
        self._sim_pb_joint_indices = []
        self._sim_pb_ee_link_index = None
        self._sim_pb_renderer = None
        self._sim_pb_renderer_fallback_done = False
        self.sim_vtk_view = None
        self.sim_robot = None
        self.sim_joint_names = []
        self.sim_q = np.zeros(0, dtype=float)
        self._sim_limits = []
        self._sim_joint_offsets = np.zeros(0, dtype=float)
        self._sim_model_limits = []
        self._sim_fk = None
        self._sim_matrix_to_rpy = None
        self._sim_solve_ik = None
        self._sdk_robot: Optional[SDKRobot] = None
        self._sdk_lock = threading.RLock()
        env_sdk_config = str(os.getenv("SOARMMOCE_CONFIG", "")).strip()
        self._sdk_config_path = env_sdk_config or (str(DEFAULT_SDK_REAL_CONFIG_PATH) if DEFAULT_SDK_REAL_CONFIG_PATH.exists() else None)
        self._sdk_runtime_mode = "disconnected"
        self._sdk_runtime_transport = ""
        self._sdk_runtime_config_path = self._sdk_config_path or ""
        self._sdk_last_connect_error = ""
        self._sdk_gui_sync_enabled = str(os.getenv("SOARMMOCE_GUI_SYNC", "1")).strip().lower() not in ("0", "false", "off", "no")
        try:
            self._sdk_gui_sync_interval_sec = max(
                0.02,
                float(str(os.getenv("SOARMMOCE_GUI_SYNC_INTERVAL", "0.04")).strip()),
            )
        except Exception:
            self._sdk_gui_sync_interval_sec = 0.04
        self._sdk_last_gui_sync_ts = 0.0
        self._sdk_last_gui_sync_err_ts = 0.0
        self._sdk_visual_prefer_twin_until_ts = 0.0
        self._sdk_visual_twin_tol_rad = math.radians(0.8)
        self._sdk_visual_twin_slack_sec = 2.5
        self._sdk_cmd_queue = deque()
        self._sdk_cmd_cond = threading.Condition()
        self._sdk_cmd_running = True
        self._sdk_cmd_thread: Optional[threading.Thread] = None
        self._sdk_sync_running = True
        self._sdk_sync_thread: Optional[threading.Thread] = None
        self._sim_motion_active = False
        self._sim_motion_start_q = np.zeros(0, dtype=float)
        self._sim_motion_target_q = np.zeros(0, dtype=float)
        self._sim_motion_start_ts = 0.0
        self._sim_motion_end_ts = 0.0
        self._sim_motion_timer = QTimer(self)
        self._sim_motion_timer.setInterval(16)
        self._sim_motion_timer.timeout.connect(self._on_sim_motion_tick)
        self._jog_hold_timer = QTimer(self)
        self._jog_hold_timer.setInterval(100)
        self._jog_hold_timer.timeout.connect(self._on_quick_jog_hold_tick)
        self._jog_hold_key = None

        self.last_frame = None
        self.last_frame_fps = 0.0
        self.last_frame_latency = 0.0
        self._virtual_cam_tick_ts = time.time()

        self.init_ui()
        self.setup_timers()
        self.log_signal.connect(self._append_log)
        self.sdk_command_state_ready.connect(self._on_sdk_command_state_ready)
        self.sdk_command_failed.connect(self._on_sdk_command_failed)
        self.sdk_sync_state_ready.connect(self._on_sdk_sync_state_ready)
        self.sdk_connection_finished.connect(self._on_sdk_connection_finished)
        self._start_sdk_command_worker()
        self._start_sdk_sync_worker()

        self._apply_language()
        self._sync_motion_defaults_from_settings()
        self._update_connection_widgets()
        self._update_global_status_bar()

    def _build_translations(self):
        return {
            "zh": {
                "window_title": "SoarmMoce Control",
                "nav_home": "Home",
                "nav_quick": "Quick Move",
                "nav_settings": "Settings",
                "btn_back_home": "⌂",
                "btn_connecting": "连接中...",
                "btn_camera": "📷 相机",
                "btn_camera_close": "📷 关闭相机",
                "btn_speech": "🎤 语音",
                "btn_speech_close": "🎤 关闭语音",
                "btn_log": "🧾 日志",
                "lang_label": "语言",
                "camera_window_title": "独立摄像头窗口",
                "speech_window_title": "语音输入",
                "status_ready": "就绪",
                "status_connecting": "连接中...",
                "status_connected": "已连接",
                "status_disconnected": "已断开",
                "status_simulation": "仿真模式",
                "state_connecting": "Connecting",
                "state_connected": "Connected",
                "state_disconnected": "Disconnected",
                "state_simulation": "Simulation",
                "state_normal": "Normal",
                "state_warning": "Warning",
                "state_fault": "Fault",
                "state_estop": "E-Stop",
                "mode_manual": "Manual",
                "mode_auto": "Auto",
                "owner_local": "Local",
                "global_connection": "连接",
                "global_robot": "机器人",
                "global_mode": "模式",
                "global_rtt": "RTT",
                "global_speed": "速度",
                "global_owner": "控制权",
                "home_card_connection": "连接",
                "home_card_robot": "机器人",
                "home_card_network": "网络",
                "home_card_motion": "运动",
                "home_tile_quick_move": "快速移动",
                "home_tile_job": "作业",
                "home_tile_settings": "设置",
                "home_tile_program": "程序\n敬请期待",
                "quick_left_title": "笛卡尔 Jog",
                "quick_center_title": "3D 视图",
                "quick_right_title": "关节控制",
                "quick_coord": "坐标系",
                "quick_speed": "速度",
                "quick_tcp": "末端位姿",
                "quick_origin": "回原点",
                "quick_zero": "回 Home",
                "quick_free": "自由移动",
                "job_recordings": "录制",
                "job_positions": "点位",
                "job_logs": "日志",
                "settings_hardware": "硬件",
                "settings_robot": "Robot Model / URDF",
                "settings_motion": "Motion",
                "settings_ui": "UI",
                "settings_jog_style": "Jog 风格",
                "settings_jog_minimal": "极简线条（推荐）",
                "settings_jog_soft": "柔和键帽",
                "label_ctl_port": "控制端口:",
                "label_camera_source": "相机来源:",
                "label_cam_port": "摄像头端口:",
                "label_camera_device": "本地摄像头:",
                "label_camera_rotation": "画面旋转:",
                "camera_source_virtual": "虚拟预览",
                "camera_source_v4l2": "本地 V4L2 相机",
                "placeholder_pos_name": "位置名称",
                "placeholder_rec_name": "录制名称",
                "btn_save_pos": "💾 保存当前位置",
                "btn_goto": "🎯 跳转",
                "btn_delete": "🗑️ 删除",
                "btn_refresh": "🔄 刷新",
                "btn_record_start": "⏺️ 开始录制",
                "btn_record_stop": "⏹️ 停止录制",
                "label_play_times": "播放次数:",
                "btn_play": "▶️ 播放",
                "btn_connect": "🔗 连接",
                "btn_disconnect": "❌ 断开",
                "btn_home": "🏠 Home",
                "sim_loading": "3D 视图加载中...",
                "sim_not_initialized": "仿真模型未初始化",
                "sim_urdf_not_found": "URDF 读取失败：未找到可用 URDF 文件",
                "sim_urdf_loaded": "URDF 模型已加载",
                "sim_view_unavailable": "3D 视图不可用：{error}",
                "sim_fallback_enabled": "已自动切换到 FK 线框回退模式",
                "sim_pybullet_init_failed": "PyBullet 初始化失败：{error}",
                "sim_slider_group": "关节角 (rad)",
                "sim_reset_zero": "重置为 Home",
                "camera_disconnected": "未连接",
                "fps_default": "FPS: --",
                "latency_default": "延迟: -- ms",
                "latency_value": "延迟: {value:.1f} ms",
                "rtt_min": "最小",
                "rtt_max": "最大",
                "rtt_avg": "平均",
                "msg_tip": "提示",
                "msg_confirm": "确认",
                "msg_input_pos_name": "请输入位置名称",
                "msg_input_rec_name": "请输入录制名称",
                "msg_jump_title": "跳转",
                "msg_jump_prompt": "跳转到 '{name}' 的时间 (秒):",
                "msg_confirm_delete_pos": "确定要删除位置 '{name}' 吗?",
                "msg_confirm_delete_rec": "确定要删除录制 '{name}' 吗?",
                "msg_confirm_home": "确定要回 Home 位吗?",
                "msg_confirm_exit_disconnect": "当前已连接，确定直接断开并退出吗?",
                "log_connecting": "正在建立串口连接...",
                "log_connect_success": "连接成功!",
                "log_connect_failed": "连接失败: {error}",
                "log_disconnecting": "正在断开...",
                "log_disconnected": "已断开",
                "log_pos_saved": "位置 '{name}' 已保存",
                "log_goto_start": "正在跳转到 '{name}'...",
                "log_goto_done": "跳转到 '{name}' 完成",
                "log_goto_failed": "跳转失败",
                "log_pos_deleted": "位置 '{name}' 已删除",
                "log_record_start": "开始录制 '{name}'",
                "log_record_saved": "录制已保存",
                "log_play_start": "正在播放 '{name}' x{times}...",
                "log_play_done": "播放完成",
                "log_play_failed": "播放失败",
                "log_record_deleted": "录制 '{name}' 已删除",
                "log_home_start": "正在回 Home 位...",
                "log_home_done": "回 Home 位完成",
                "log_home_failed": "回 Home 位失败",
                "rec_item_fmt": "{name} ({frames} 帧, {duration:.1f}s)",
            },
            "en": {
                "window_title": "SoarmMoce Control",
                "nav_home": "Home",
                "nav_quick": "Quick Move",
                "nav_settings": "Settings",
                "btn_back_home": "⌂",
                "btn_connecting": "Connecting...",
                "btn_camera": "📷 Camera",
                "btn_camera_close": "📷 Close Camera",
                "btn_speech": "🎤 Voice",
                "btn_speech_close": "🎤 Close Voice",
                "btn_log": "🧾 Logs",
                "lang_label": "Language",
                "camera_window_title": "Camera Window",
                "speech_window_title": "Voice Input",
                "status_ready": "Ready",
                "status_connecting": "Connecting...",
                "status_connected": "Connected",
                "status_disconnected": "Disconnected",
                "status_simulation": "Simulation",
                "state_connecting": "Connecting",
                "state_connected": "Connected",
                "state_disconnected": "Disconnected",
                "state_simulation": "Simulation",
                "state_normal": "Normal",
                "state_warning": "Warning",
                "state_fault": "Fault",
                "state_estop": "E-Stop",
                "mode_manual": "Manual",
                "mode_auto": "Auto",
                "owner_local": "Local",
                "global_connection": "Connection",
                "global_robot": "Robot",
                "global_mode": "Mode",
                "global_rtt": "RTT",
                "global_speed": "Speed",
                "global_owner": "Owner",
                "home_card_connection": "Connection",
                "home_card_robot": "Robot",
                "home_card_network": "Network",
                "home_card_motion": "Motion",
                "home_tile_quick_move": "Quick Move",
                "home_tile_job": "Job",
                "home_tile_settings": "Settings",
                "home_tile_program": "Program\nComing soon",
                "quick_left_title": "Cartesian Jog",
                "quick_center_title": "3D View",
                "quick_right_title": "Joint Control",
                "quick_coord": "Coordinate",
                "quick_speed": "Speed",
                "quick_tcp": "TCP",
                "quick_origin": "Origin",
                "quick_zero": "Home",
                "quick_free": "Free Move",
                "job_recordings": "Recordings",
                "job_positions": "Positions",
                "job_logs": "Logs",
                "settings_hardware": "Hardware",
                "settings_robot": "Robot Model / URDF",
                "settings_motion": "Motion",
                "settings_ui": "UI",
                "settings_jog_style": "Jog Style",
                "settings_jog_minimal": "Minimal Line (Recommended)",
                "settings_jog_soft": "Soft Keycap",
                "label_ctl_port": "Control Port:",
                "label_camera_source": "Camera Source:",
                "label_cam_port": "Camera Port:",
                "label_camera_device": "Local Camera:",
                "label_camera_rotation": "Camera Rotation:",
                "camera_source_virtual": "Virtual Preview",
                "camera_source_v4l2": "Local V4L2 Camera",
                "placeholder_pos_name": "Position name",
                "placeholder_rec_name": "Recording name",
                "btn_save_pos": "💾 Save Current Pose",
                "btn_goto": "🎯 Go To",
                "btn_delete": "🗑️ Delete",
                "btn_refresh": "🔄 Refresh",
                "btn_record_start": "⏺️ Start Recording",
                "btn_record_stop": "⏹️ Stop Recording",
                "label_play_times": "Play count:",
                "btn_play": "▶️ Play",
                "btn_connect": "🔗 Connect",
                "btn_disconnect": "❌ Disconnect",
                "btn_home": "🏠 Home",
                "sim_loading": "Loading 3D view...",
                "sim_not_initialized": "Simulation model not initialized",
                "sim_urdf_not_found": "URDF load failed: no usable URDF found",
                "sim_urdf_loaded": "URDF model loaded",
                "sim_view_unavailable": "3D view unavailable: {error}",
                "sim_fallback_enabled": "Automatically switched to FK wireframe fallback",
                "sim_pybullet_init_failed": "PyBullet init failed: {error}",
                "sim_slider_group": "Joint Angles (rad)",
                "sim_reset_zero": "Reset Home",
                "camera_disconnected": "Disconnected",
                "fps_default": "FPS: --",
                "latency_default": "Latency: -- ms",
                "latency_value": "Latency: {value:.1f} ms",
                "rtt_min": "Min",
                "rtt_max": "Max",
                "rtt_avg": "Avg",
                "msg_tip": "Notice",
                "msg_confirm": "Confirm",
                "msg_input_pos_name": "Please enter a position name",
                "msg_input_rec_name": "Please enter a recording name",
                "msg_jump_title": "Go To",
                "msg_jump_prompt": "Move to '{name}' in (seconds):",
                "msg_confirm_delete_pos": "Delete position '{name}'?",
                "msg_confirm_delete_rec": "Delete recording '{name}'?",
                "msg_confirm_home": "Return to home position?",
                "msg_confirm_exit_disconnect": "Robot is connected. Disconnect and exit now?",
                "log_connecting": "Opening serial runtime...",
                "log_connect_success": "Connected!",
                "log_connect_failed": "Connect failed: {error}",
                "log_disconnecting": "Disconnecting...",
                "log_disconnected": "Disconnected",
                "log_pos_saved": "Position '{name}' saved",
                "log_goto_start": "Moving to '{name}'...",
                "log_goto_done": "Move to '{name}' complete",
                "log_goto_failed": "Move failed",
                "log_pos_deleted": "Position '{name}' deleted",
                "log_record_start": "Started recording '{name}'",
                "log_record_saved": "Recording saved",
                "log_play_start": "Playing '{name}' x{times}...",
                "log_play_done": "Playback complete",
                "log_play_failed": "Playback failed",
                "log_record_deleted": "Recording '{name}' deleted",
                "log_home_start": "Returning home...",
                "log_home_done": "Home complete",
                "log_home_failed": "Home failed",
                "rec_item_fmt": "{name} ({frames} frames, {duration:.1f}s)",
            },
        }

    def _tr(self, key: str, **kwargs) -> str:
        lang_map = self._translations.get(self.current_lang, {})
        fallback = self._translations.get("zh", {})
        text = lang_map.get(key, fallback.get(key, key))
        if kwargs:
            try:
                return text.format(**kwargs)
            except Exception:
                return text
        return text

    def init_ui(self):
        self.setWindowTitle(self._tr("window_title"))
        self.setMinimumSize(1360, 860)
        self.setStyleSheet(get_stylesheet(self.current_theme))

        if LOGO_PATH.exists():
            icon = QPixmap(str(LOGO_PATH))
            if not icon.isNull():
                self.setWindowIcon(QIcon(icon.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 0)
        root.setSpacing(8)

        header = self._build_header()
        root.addWidget(header)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, stretch=1)

        self.quick_page = QuickMovePage()
        self.settings_page = SettingsPage()

        self.stack.addWidget(self.quick_page)
        self.stack.addWidget(self.settings_page)

        self.global_status = GlobalStatusBar()
        root.addWidget(self.global_status)

        self.log_dock = QDockWidget("Logs", self)
        self.log_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.log_dock_text = QLabel()
        self.log_dock_text.setText("")
        self.log_dock_text.setWordWrap(True)
        self.log_dock_text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        dock_widget = QWidget()
        dock_layout = QVBoxLayout(dock_widget)
        self.log_dock_view = self._make_log_text_widget()
        dock_layout.addWidget(self.log_dock_view)
        self.log_dock.setWidget(dock_widget)
        self.addDockWidget(Qt.RightDockWidgetArea, self.log_dock)
        self.log_dock.hide()
        self.log_dock.visibilityChanged.connect(self._on_log_dock_visibility_changed)

        sim_widget = self._build_sim_widget()
        self.quick_page.set_sim_widget(sim_widget)

        self._bind_signals()
        self.on_jog_style_changed()
        self._apply_theme(self.current_theme)
        self.statusBar().showMessage(self._tr("status_ready"))
        self._set_page(self.PAGE_QUICK_MOVE)

    def _build_header(self) -> QWidget:
        header = QFrame()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self.back_home_btn = QPushButton(self._tr("btn_back_home"))
        self.back_home_btn.setObjectName("primaryBtn")
        self.back_home_btn.setFixedWidth(44)
        self.back_home_btn.setCheckable(True)
        self.back_home_btn.clicked.connect(lambda: self._set_page(self.PAGE_QUICK_MOVE))
        layout.addWidget(self.back_home_btn)

        self.top_settings_btn = QPushButton(self._tr("nav_settings"))
        self.top_settings_btn.setObjectName("primaryBtn")
        self.top_settings_btn.setFixedWidth(44)
        self.top_settings_btn.setCheckable(True)
        self.top_settings_btn.clicked.connect(lambda: self._set_page(self.PAGE_SETTINGS))
        layout.addWidget(self.top_settings_btn)
        self.nav_buttons = [self.back_home_btn, self.top_settings_btn]

        layout.addStretch()

        self.connect_toggle_btn = QPushButton(self._tr("btn_connect"))
        self.connect_toggle_btn.setObjectName("primaryBtn")
        layout.addWidget(self.connect_toggle_btn)

        self.camera_btn = QPushButton(self._tr("btn_camera"))
        self.camera_btn.setObjectName("primaryBtn")
        self.camera_btn.clicked.connect(self.on_toggle_camera_window)
        layout.addWidget(self.camera_btn)

        self.speech_btn = QPushButton(self._tr("btn_speech"))
        self.speech_btn.setObjectName("primaryBtn")
        self.speech_btn.clicked.connect(self.on_toggle_speech_window)
        layout.addWidget(self.speech_btn)

        self.log_btn = QPushButton(self._tr("btn_log"))
        self.log_btn.setObjectName("primaryBtn")
        self.log_btn.setCheckable(True)
        self.log_btn.clicked.connect(self.on_toggle_log_panel)
        layout.addWidget(self.log_btn)

        self.lang_label = QLabel(self._tr("lang_label"))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("中文", "zh")
        self.lang_combo.addItem("English", "en")
        self.lang_combo.currentIndexChanged.connect(self.on_language_changed)
        layout.addWidget(self.lang_label)
        layout.addWidget(self.lang_combo)

        self.brand_label = QLabel()
        layout.addWidget(self.brand_label)

        self._apply_header_icons()
        self._update_header_brand()

        return header

    def _picture_path_by_theme(self, light_name: str, dark_name: str) -> Optional[Path]:
        primary = dark_name if self.current_theme == "dark" else light_name
        fallback = light_name if self.current_theme == "dark" else dark_name
        for name in (primary, fallback):
            path = PICTURE_DIR / name
            if path.exists():
                return path
        return None

    def _load_themed_icon(self, key: str) -> QIcon:
        pair = HEADER_ICON_FILES.get(key)
        if pair is None:
            return QIcon()
        if key in ("home", "job", "settings"):
            # Keep compact top icons white in both light/dark themes.
            white_path = PICTURE_DIR / pair[1]
            path = white_path if white_path.exists() else self._picture_path_by_theme(pair[0], pair[1])
        else:
            path = self._picture_path_by_theme(pair[0], pair[1])
        if path is None:
            return QIcon()
        return QIcon(str(path))

    def _set_button_icon(self, button: QPushButton, key: str, size: int = 18) -> bool:
        icon = self._load_themed_icon(key)
        if icon.isNull():
            button.setIcon(QIcon())
            return False
        button.setIcon(icon)
        button.setIconSize(QSize(size, size))
        return True

    def _apply_header_icons(self):
        self._back_home_has_icon = self._set_button_icon(self.back_home_btn, "home", size=18)
        self._top_settings_has_icon = self._set_button_icon(self.top_settings_btn, "settings", size=18)
        self.connect_toggle_btn.setIcon(QIcon())
        self.camera_btn.setIcon(QIcon())
        self.speech_btn.setIcon(QIcon())
        self.log_btn.setIcon(QIcon())

    def _update_header_brand(self):
        if not hasattr(self, "brand_label"):
            return
        candidates = (
            (MOCEAI_DARK_PATH, MOCEAI_LIGHT_PATH)
            if self.current_theme == "dark"
            else (MOCEAI_LIGHT_PATH, MOCEAI_DARK_PATH)
        )
        pixmap = None
        for path in candidates:
            if not path.exists():
                continue
            px = QPixmap(str(path))
            if px.isNull():
                continue
            pixmap = px
            break
        if pixmap is None:
            self.brand_label.clear()
            self.brand_label.hide()
            return
        self.brand_label.setPixmap(pixmap.scaledToHeight(44, Qt.SmoothTransformation))
        self.brand_label.show()

    def _plain_header_text(self, key: str) -> str:
        text = self._tr(key)
        text = (
            text.replace("📷 ", "")
            .replace("🎤 ", "")
            .replace("🧾 ", "")
            .replace("🔗 ", "")
            .replace("❌ ", "")
            .replace("⌂", "")
            .strip()
        )
        return text if text else self._tr(key)

    def _set_camera_btn_text(self, opened: bool):
        key = "btn_camera_close" if opened else "btn_camera"
        self.camera_btn.setText(self._plain_header_text(key))

    def _set_connect_btn_text(self):
        if self.connecting:
            key = "btn_connecting"
        elif self.connected or self._sdk_runtime_mode == "simulation":
            key = "btn_disconnect"
        else:
            key = "btn_connect"
        self.connect_toggle_btn.setText(self._plain_header_text(key))
        self.connect_toggle_btn.setToolTip(self._tr(key))
        self.connect_toggle_btn.setEnabled(not self.connecting)

    def _set_speech_btn_text(self, opened: bool):
        key = "btn_speech_close" if opened else "btn_speech"
        self.speech_btn.setText(self._plain_header_text(key))

    def _make_log_text_widget(self):
        from PyQt5.QtWidgets import QTextEdit

        log_widget = QTextEdit()
        log_widget.setReadOnly(True)
        return log_widget

    def _bind_signals(self):
        self.quick_page.speed_changed.connect(self.on_speed_changed)
        self.quick_page.home_clicked.connect(self._on_quick_zero)

        self.connect_toggle_btn.clicked.connect(self.on_toggle_connection)
        self.settings_page.default_speed_spin.valueChanged.connect(self.on_speed_changed)
        self.settings_page.default_step_dist_spin.valueChanged.connect(self.on_default_step_distance_changed)
        self.settings_page.default_step_angle_spin.valueChanged.connect(self.on_default_step_angle_changed)
        self.settings_page.ui_lang_combo.currentIndexChanged.connect(self.on_settings_language_changed)
        self.settings_page.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        self.settings_page.jog_style_combo.currentIndexChanged.connect(self.on_jog_style_changed)
        self.settings_page.camera_source_combo.currentIndexChanged.connect(self.on_camera_source_changed)
        self.settings_page.camera_rotation_combo.currentIndexChanged.connect(self.on_camera_source_changed)
        self.settings_page.camera_device_input.editingFinished.connect(self.on_camera_source_changed)
        self.settings_page.apply_view_btn.clicked.connect(self._apply_vtk_visual_settings)
        self.settings_page.aa_mode_combo.currentIndexChanged.connect(self._apply_vtk_visual_settings)
        self.settings_page.material_preset_combo.currentIndexChanged.connect(self._apply_vtk_visual_settings)
        self.settings_page.background_theme_combo.currentIndexChanged.connect(self._apply_vtk_visual_settings)
        self.settings_page.camera_preset_combo.currentIndexChanged.connect(self._apply_vtk_camera_preset)

        for idx, (_, minus_btn, _, plus_btn) in enumerate(self.quick_page.joint_rows):
            minus_btn.clicked.connect(partial(self._on_quick_joint_step, idx, -1.0))
            plus_btn.clicked.connect(partial(self._on_quick_joint_step, idx, 1.0))

        for key, btn in self.quick_page.jog_buttons.items():
            btn.clicked.connect(partial(self._on_quick_jog_clicked, key))
            btn.pressed.connect(partial(self._on_quick_jog_pressed, key))
            btn.released.connect(self._on_quick_jog_released)

        self.quick_page.goto_origin_btn.clicked.connect(self._on_quick_origin)
        self.quick_page.free_move_btn.clicked.connect(self._on_quick_free_move)

    def setup_timers(self):
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(500)

        self.virtual_cam_timer = QTimer()
        self.virtual_cam_timer.timeout.connect(self._on_virtual_cam_tick)
        self.virtual_cam_timer.start(33)

    def _set_page(self, index: int):
        index = int(index)
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)

    def _on_log_dock_visibility_changed(self, visible: bool):
        self.log_btn.blockSignals(True)
        self.log_btn.setChecked(bool(visible))
        self.log_btn.blockSignals(False)

    def on_toggle_log_panel(self):
        if self.log_dock.isVisible():
            self.log_dock.hide()
        else:
            self.log_dock.show()

    def on_language_changed(self):
        if self._lang_syncing:
            return
        lang = self.lang_combo.currentData()
        if lang in ("zh", "en") and lang != self.current_lang:
            self.current_lang = lang
            self._apply_language()

    def on_settings_language_changed(self):
        if self._lang_syncing:
            return
        lang = self.settings_page.ui_lang_combo.currentData()
        if lang in ("zh", "en") and lang != self.current_lang:
            self.current_lang = lang
            self._apply_language()

    def on_theme_changed(self):
        theme = self.settings_page.theme_combo.currentData()
        self._apply_theme(theme if theme else "light")

    def on_jog_style_changed(self):
        style = self.settings_page.jog_style_combo.currentData()
        if hasattr(self.quick_page, "set_jog_visual_style"):
            self.quick_page.set_jog_visual_style(style if style else "line")

    def on_default_step_distance_changed(self, value: float):
        self.quick_page.step_dist_spin.blockSignals(True)
        self.quick_page.step_dist_spin.setValue(float(value))
        self.quick_page.step_dist_spin.blockSignals(False)

    def on_default_step_angle_changed(self, value: float):
        self.quick_page.step_angle_spin.blockSignals(True)
        self.quick_page.step_angle_spin.setValue(float(value))
        self.quick_page.step_angle_spin.blockSignals(False)

    def _sync_motion_defaults_from_settings(self):
        self.on_speed_changed(int(self.settings_page.default_speed_spin.value()))
        self.on_default_step_distance_changed(float(self.settings_page.default_step_dist_spin.value()))
        self.on_default_step_angle_changed(float(self.settings_page.default_step_angle_spin.value()))

    def _apply_language(self):
        self.setWindowTitle(self._tr("window_title"))

        if getattr(self, "_back_home_has_icon", False):
            self.back_home_btn.setText("")
        else:
            self.back_home_btn.setText(self._tr("nav_quick"))
        self.back_home_btn.setToolTip(self._tr("nav_quick"))
        if getattr(self, "_top_settings_has_icon", False):
            self.top_settings_btn.setText("")
        else:
            self.top_settings_btn.setText(self._tr("nav_settings"))
        self.top_settings_btn.setToolTip(self._tr("nav_settings"))
        self._set_connect_btn_text()
        self._set_camera_btn_text(self._is_camera_window_visible())
        self._set_speech_btn_text(self._is_speech_window_visible())
        self.log_btn.setText(self._plain_header_text("btn_log"))
        self.lang_label.setText(self._tr("lang_label"))

        self.quick_page.set_texts(self._tr)
        self.settings_page.set_texts(self._tr)

        self.mode_text = self._tr("mode_manual")
        self.control_owner_text = self._tr("owner_local")

        if self.camera_window is not None:
            self.camera_window.set_window_title(self._tr("camera_window_title"))
        if self.speech_window is not None:
            self.speech_window.set_window_title(self._tr("speech_window_title"))

        self._sync_language_combos()
        self._update_global_status_bar()
        self.statusBar().showMessage(self._status_message_for_runtime())

    def _sync_language_combos(self):
        lang_idx = 0 if self.current_lang == "zh" else 1
        self._lang_syncing = True
        self.lang_combo.setCurrentIndex(lang_idx)
        self.settings_page.ui_lang_combo.setCurrentIndex(lang_idx)
        self._lang_syncing = False

    def _sync_theme_combo(self):
        desired = str(self.current_theme).strip().lower()
        idx = self.settings_page.theme_combo.findData(desired)
        if idx < 0:
            idx = self.settings_page.theme_combo.findData("light")
        self.settings_page.theme_combo.blockSignals(True)
        self.settings_page.theme_combo.setCurrentIndex(max(0, idx))
        self.settings_page.theme_combo.blockSignals(False)

    def _apply_theme(self, theme: str):
        theme_norm = str(theme).strip().lower()
        if theme_norm not in ("light", "dark"):
            theme_norm = "light"
        self.current_theme = theme_norm
        self._apply_header_icons()
        self._update_header_brand()
        stylesheet = get_stylesheet(theme_norm)
        self.setStyleSheet(stylesheet)
        apply_soft_effects(self, theme_norm)
        if hasattr(self.quick_page, "set_theme"):
            self.quick_page.set_theme(theme_norm)
        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            if hasattr(self.sim_vtk_view, "set_ui_theme"):
                self.sim_vtk_view.set_ui_theme(theme_norm)
        if self.camera_window is not None:
            self.camera_window.setStyleSheet(stylesheet)
            apply_soft_effects(self.camera_window, theme_norm)
        if self.speech_window is not None:
            self.speech_window.set_theme(theme_norm)
        self._sync_theme_combo()

        if theme_norm == "dark" and self.settings_page.background_theme_combo.currentData() != "dark":
            idx = self.settings_page.background_theme_combo.findData("dark")
            if idx >= 0:
                self.settings_page.background_theme_combo.setCurrentIndex(idx)
        elif theme_norm == "light" and self.settings_page.background_theme_combo.currentData() == "dark":
            idx = self.settings_page.background_theme_combo.findData("studio")
            if idx >= 0:
                self.settings_page.background_theme_combo.setCurrentIndex(idx)

        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            self._apply_vtk_visual_settings()

    def on_speed_changed(self, value: int):
        self.speed_percent = int(value)
        self.quick_page.speed_slider.blockSignals(True)
        self.quick_page.speed_slider.setValue(self.speed_percent)
        self.quick_page.speed_slider.blockSignals(False)
        self.settings_page.default_speed_spin.blockSignals(True)
        self.settings_page.default_speed_spin.setValue(self.speed_percent)
        self.settings_page.default_speed_spin.blockSignals(False)
        self.quick_page.speed_value.setText(f"{self.speed_percent}%")
        self._update_global_status_bar()

    def _start_sdk_command_worker(self):
        if self._sdk_cmd_thread is not None and self._sdk_cmd_thread.is_alive():
            return
        self._sdk_cmd_running = True
        self._sdk_cmd_thread = threading.Thread(
            target=self._sdk_command_loop,
            name="soarmmoce-sdk-command-worker",
            daemon=True,
        )
        self._sdk_cmd_thread.start()

    def _stop_sdk_command_worker(self):
        thread = self._sdk_cmd_thread
        if thread is None:
            return
        with self._sdk_cmd_cond:
            self._sdk_cmd_running = False
            self._sdk_cmd_queue.clear()
            self._sdk_cmd_cond.notify_all()
        thread.join(timeout=2.0)
        self._sdk_cmd_thread = None

    def _start_sdk_sync_worker(self):
        if self._sdk_sync_thread is not None and self._sdk_sync_thread.is_alive():
            return
        self._sdk_sync_running = True
        self._sdk_sync_thread = threading.Thread(
            target=self._sdk_sync_loop,
            name="soarmmoce-sdk-sync-worker",
            daemon=True,
        )
        self._sdk_sync_thread.start()

    def _stop_sdk_sync_worker(self):
        thread = self._sdk_sync_thread
        if thread is None:
            return
        self._sdk_sync_running = False
        thread.join(timeout=2.0)
        self._sdk_sync_thread = None

    def _drop_sdk_commands_locked(self, kinds: Tuple[str, ...]):
        if not kinds:
            return
        self._sdk_cmd_queue = deque(
            cmd for cmd in self._sdk_cmd_queue if str(cmd.get("kind", "")).strip() not in kinds
        )

    def _enqueue_sdk_command(self, command: Dict[str, Any], drop_kinds: Tuple[str, ...] = ()):
        with self._sdk_cmd_cond:
            if not self._sdk_cmd_running:
                return
            self._drop_sdk_commands_locked(tuple(str(kind) for kind in drop_kinds))
            self._sdk_cmd_queue.append(dict(command))
            self._sdk_cmd_cond.notify()

    def _clear_pending_sdk_commands(self, *kinds: str):
        with self._sdk_cmd_cond:
            if kinds:
                self._drop_sdk_commands_locked(tuple(str(kind) for kind in kinds))
            else:
                self._sdk_cmd_queue.clear()

    def _sdk_command_loop(self):
        while True:
            with self._sdk_cmd_cond:
                while self._sdk_cmd_running and not self._sdk_cmd_queue:
                    self._sdk_cmd_cond.wait()
                if not self._sdk_cmd_running and not self._sdk_cmd_queue:
                    return
                command = dict(self._sdk_cmd_queue.popleft())

            source = str(command.get("source", "sdk")).strip() or "sdk"
            try:
                state = self._execute_sdk_command(command)
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                self.sdk_command_failed.emit(source, message)
                continue

            self.sdk_command_state_ready.emit({"state": state, "command": command}, source)

    def _execute_sdk_command(self, command: Dict[str, Any]) -> object:
        kind = str(command.get("kind", "")).strip()
        if kind == "joint_step":
            return self._execute_sdk_joint_step(command)
        if kind == "cartesian_jog":
            return self._execute_sdk_cartesian_jog(command)
        if kind == "home":
            return self._execute_sdk_home(command)
        if kind == "stop":
            return self._execute_sdk_stop()
        raise ValueError(f"Unknown SDK command: {kind}")

    def _execute_sdk_joint_step(self, command: Dict[str, Any]) -> object:
        idx = int(command["joint_index"])
        delta = float(command["delta"])
        duration = float(command["duration"])
        if idx < 0:
            raise ValueError("Joint index must be non-negative")

        with self._sdk_lock:
            robot = self._sdk_get_robot()
            state_before = robot.get_state()
            q_target = self._extract_joint_q_from_sdk_state(state_before, prefer_twin=True)
            if q_target is None:
                q_target = np.asarray(state_before.joint_state.q, dtype=float).copy()
            else:
                q_target = np.asarray(q_target, dtype=float).copy()
            if idx >= q_target.shape[0]:
                if idx == len(self.quick_page.joint_rows) - 1 and bool(getattr(state_before.gripper_state, "available", False)):
                    current_ratio = getattr(state_before.gripper_state, "open_ratio", None)
                    if current_ratio is None:
                        current_ratio = 0.5
                    ratio_step = 0.05 if float(delta) >= 0.0 else -0.05
                    robot.set_gripper(
                        open_ratio=float(np.clip(float(current_ratio) + ratio_step, 0.0, 1.0)),
                        wait=False,
                    )
                    return robot.get_state()
                raise ValueError(f"Joint index {idx} out of range")
            lo, hi = robot.robot_model.joint_limits[idx]
            q_target[idx] = float(np.clip(q_target[idx] + delta, float(lo), float(hi)))
            robot.move_joints(q_target, duration=duration, wait=True)
            return robot.get_state()

    def _execute_sdk_cartesian_jog(self, command: Dict[str, Any]) -> object:
        key_norm = str(command["key"]).strip().upper()
        step_mm = float(command.get("step_mm", 1.0))
        step_rad = float(command.get("step_rad", math.radians(1.0)))
        duration = float(command.get("duration", 0.2))
        frame = "tool" if bool(command.get("use_tool", False)) else "base"
        delta_kwargs = {
            "dx": 0.0,
            "dy": 0.0,
            "dz": 0.0,
            "drx": 0.0,
            "dry": 0.0,
            "drz": 0.0,
        }
        key_map = {
            "+X": ("dx", 1.0, step_mm / 1000.0),
            "-X": ("dx", -1.0, step_mm / 1000.0),
            "+Y": ("dy", 1.0, step_mm / 1000.0),
            "-Y": ("dy", -1.0, step_mm / 1000.0),
            "+Z": ("dz", 1.0, step_mm / 1000.0),
            "-Z": ("dz", -1.0, step_mm / 1000.0),
            "+RX": ("drx", 1.0, step_rad),
            "-RX": ("drx", -1.0, step_rad),
            "+RY": ("dry", 1.0, step_rad),
            "-RY": ("dry", -1.0, step_rad),
            "+RZ": ("drz", 1.0, step_rad),
            "-RZ": ("drz", -1.0, step_rad),
        }
        if key_norm not in key_map:
            raise RuntimeError(f"Unsupported Cartesian jog key: {key_norm}")

        field_name, sign, magnitude = key_map[key_norm]
        delta_kwargs[field_name] = float(sign) * float(magnitude)
        wait_motion = bool(command.get("wait_motion", False))

        with self._sdk_lock:
            robot = self._sdk_get_robot()
            robot.move_delta(
                frame=frame,
                duration=duration,
                wait=wait_motion,
                **delta_kwargs,
            )
            return robot.get_state()

    def _execute_sdk_home(self, command: Dict[str, Any]) -> object:
        duration = float(command["duration"])
        with self._sdk_lock:
            robot = self._sdk_get_robot()
            robot.home(duration=duration, wait=True)
            return robot.get_state()

    def _execute_sdk_stop(self) -> object:
        with self._sdk_lock:
            robot = self._sdk_get_robot()
            robot.stop()
            return robot.get_state()

    @staticmethod
    def _sdk_command_label(source: str) -> str:
        src = str(source or "").strip()
        if src.startswith("home:"):
            return f"SDK {src.split(':', 1)[1]}"
        if src.startswith("joint:"):
            return "Quick Joint"
        if src.startswith("jog:"):
            return "Quick Jog"
        if src == "stop":
            return "SDK stop"
        return f"SDK {src}" if src else "SDK"

    def _on_sdk_command_state_ready(self, payload: object, source: str):
        if isinstance(payload, dict):
            state = payload.get("state")
            command = payload.get("command")
        else:
            state = payload
            command = None
        self._handle_sim_motion_from_command(state, command, source)
        self._sdk_last_gui_sync_ts = time.time()
        src = str(source or "").strip()
        if src.startswith("home:"):
            self.statusBar().showMessage("Robot moved to home")
            self.log(f"[{self._sdk_command_label(src)}] moved to home", "success")
        elif src == "stop":
            self.statusBar().showMessage("Robot stopped")

    def _on_sdk_command_failed(self, source: str, message: str):
        label = self._sdk_command_label(source)
        msg = str(message or "").strip() or "Command failed"
        self.statusBar().showMessage(f"{label} failed: {msg}")
        self.log(f"[{label}] {msg}", "warning")

    def _on_sdk_sync_state_ready(self, state: object):
        if self._sim_motion_active:
            # Let the local preview animation finish; otherwise periodic state sync
            # snaps the 3D view back toward the lagging actual joints mid-motion.
            self._sdk_last_gui_sync_ts = time.time()
            self._update_connection_widgets()
            self._update_quick_pose_from_sim()
            return
        self._sync_sim_from_sdk_state(state)
        self._sdk_last_gui_sync_ts = time.time()
        self._update_connection_widgets()
        self._update_quick_pose_from_sim()

    def _on_quick_joint_step(self, idx: int, direction: float):
        if idx < 0:
            return
        step_deg = max(0.1, float(self.quick_page.step_angle_spin.value()))
        delta = math.radians(step_deg) * float(direction)
        speed_scale = max(0.1, float(self.speed_percent) / 100.0)
        duration = float(np.clip(0.20 / speed_scale, 0.08, 0.60))
        self._enqueue_sdk_command(
            {
                "kind": "joint_step",
                "source": "joint:step",
                "joint_index": int(idx),
                "delta": float(delta),
                "duration": duration,
            },
            drop_kinds=("stop",),
        )

    def _quick_jog_mode_is_continuous(self) -> bool:
        combo = getattr(self.quick_page, "step_mode_combo", None)
        if combo is None:
            return False
        if int(combo.currentIndex()) == 1:
            return True
        return str(combo.currentText()).strip().lower() in {"continuous", "连续"}

    def _on_quick_jog_pressed(self, key: str):
        if not self._quick_jog_mode_is_continuous():
            return
        self._jog_hold_key = str(key)
        self._apply_quick_cartesian_jog(self._jog_hold_key)
        self._jog_hold_timer.start()

    def _on_quick_jog_released(self, enqueue_stop: bool = True):
        was_holding = bool(self._jog_hold_key) or self._jog_hold_timer.isActive()
        self._jog_hold_key = None
        if self._jog_hold_timer.isActive():
            self._jog_hold_timer.stop()
        if not was_holding and enqueue_stop:
            return
        self._clear_pending_sdk_commands("cartesian_jog")
        if enqueue_stop and was_holding:
            self._enqueue_sdk_command({"kind": "stop", "source": "stop"}, drop_kinds=("stop",))

    def _on_quick_jog_clicked(self, key: str):
        if self._quick_jog_mode_is_continuous():
            return
        self._apply_quick_cartesian_jog(str(key))

    def _on_quick_jog_hold_tick(self):
        if not self._jog_hold_key:
            self._jog_hold_timer.stop()
            return
        self._apply_quick_cartesian_jog(self._jog_hold_key)

    @staticmethod
    def _normalize_jog_key(key: str) -> str:
        key_norm = str(key).strip().upper().replace(" ", "")
        alias = {
            "+U": "+RX",
            "-U": "-RX",
            "+V": "+RY",
            "-V": "-RY",
            "+W": "+RZ",
            "-W": "-RZ",
        }
        return alias.get(key_norm, key_norm)

    def _solve_pybullet_ik(
        self,
        client_id: int,
        robot_id: int,
        joint_indices,
        limits,
        ee_link_idx: int,
        target_xyz: np.ndarray,
        target_rpy: np.ndarray,
    ) -> Optional[np.ndarray]:
        if not PYBULLET_AVAILABLE:
            return None
        if ee_link_idx is None:
            return None
        if not joint_indices:
            return None

        lower = [float(lo) for lo, _ in limits]
        upper = [float(hi) for _, hi in limits]
        ranges = [max(1e-4, float(hi - lo)) for lo, hi in limits]
        rest_user = np.array(
            [float(self.sim_q[i]) if i < len(self.sim_q) else 0.0 for i in range(len(joint_indices))],
            dtype=float,
        )
        rest = self._sim_user_to_model_q(rest_user).tolist()
        target_quat = _pb.getQuaternionFromEuler([float(target_rpy[0]), float(target_rpy[1]), float(target_rpy[2])])

        try:
            q_full = _pb.calculateInverseKinematics(
                robot_id,
                int(ee_link_idx),
                targetPosition=[float(target_xyz[0]), float(target_xyz[1]), float(target_xyz[2])],
                targetOrientation=target_quat,
                lowerLimits=lower,
                upperLimits=upper,
                jointRanges=ranges,
                restPoses=rest,
                maxNumIterations=160,
                residualThreshold=1e-5,
                physicsClientId=client_id,
            )
        except Exception:
            return None

        q_vals = list(q_full) if q_full is not None else []
        q_out = np.array(rest, dtype=float)
        for i, ji in enumerate(joint_indices):
            if ji < len(q_vals):
                raw = float(q_vals[ji])
            elif i < len(q_vals):
                raw = float(q_vals[i])
            else:
                raw = float(q_out[i])
            lo, hi = limits[i]
            q_out[i] = float(np.clip(raw, float(lo), float(hi)))
        q_user = self._sim_model_to_user_q(q_out)
        for i, (lo, hi) in enumerate(self._sim_limits[: q_user.shape[0]]):
            q_user[i] = float(np.clip(q_user[i], float(lo), float(hi)))
        return q_user

    def _solve_sim_ik(self, target_xyz: np.ndarray, target_rpy: np.ndarray) -> Optional[np.ndarray]:
        if self._sim_backend == "pybullet":
            if self._sim_pb_client is None or self._sim_pb_robot_id is None:
                return None
            ee_idx = self._sim_pb_ee_link_index
            if ee_idx is None:
                ee_idx = self._detect_pybullet_ee_link(self._sim_pb_client, self._sim_pb_robot_id)
                self._sim_pb_ee_link_index = ee_idx
            return self._solve_pybullet_ik(
                self._sim_pb_client,
                self._sim_pb_robot_id,
                self._sim_pb_joint_indices,
                self._sim_model_limits or self._sim_limits,
                ee_idx,
                target_xyz,
                target_rpy,
            )

        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            client_id = getattr(self.sim_vtk_view, "pb_client", None)
            robot_id = getattr(self.sim_vtk_view, "pb_robot_id", None)
            joint_indices = list(getattr(self.sim_vtk_view, "joint_indices", []))
            limits = list(getattr(self.sim_vtk_view, "joint_limits", []))
            ee_idx = getattr(self.sim_vtk_view, "_ee_link_index", None)
            if client_id is None or robot_id is None or not joint_indices or not limits:
                return None
            if ee_idx is None:
                ee_idx = self._detect_pybullet_ee_link(client_id, robot_id)
                setattr(self.sim_vtk_view, "_ee_link_index", ee_idx)
            return self._solve_pybullet_ik(
                int(client_id),
                int(robot_id),
                joint_indices,
                self._sim_model_limits or limits,
                ee_idx,
                target_xyz,
                target_rpy,
            )

        if self._sim_backend == "kinematics":
            if self.sim_robot is None or self._sim_solve_ik is None:
                return None
            try:
                sol = self._sim_solve_ik(
                    self.sim_robot,
                    np.asarray(target_xyz, dtype=float),
                    np.asarray(target_rpy, dtype=float),
                    q0=np.asarray(self.sim_q, dtype=float),
                    max_iters=180,
                    pos_tol=2e-3,
                    rot_tol=2e-2,
                )
            except Exception:
                return None

            q_out = np.asarray(sol.q, dtype=float).copy()
            if q_out.shape[0] != len(self._sim_limits):
                return None
            for i, (lo, hi) in enumerate(self._sim_limits):
                q_out[i] = float(np.clip(q_out[i], float(lo), float(hi)))
            if not sol.success and (float(sol.pos_err) > 0.03 or float(sol.rot_err) > 0.30):
                return None
            return q_out

        return None

    def _apply_quick_cartesian_jog(self, key: str):
        key_norm = self._normalize_jog_key(key)
        trans_map = {
            "+X": np.array([1.0, 0.0, 0.0], dtype=float),
            "-X": np.array([-1.0, 0.0, 0.0], dtype=float),
            "+Y": np.array([0.0, 1.0, 0.0], dtype=float),
            "-Y": np.array([0.0, -1.0, 0.0], dtype=float),
            "+Z": np.array([0.0, 0.0, 1.0], dtype=float),
            "-Z": np.array([0.0, 0.0, -1.0], dtype=float),
        }
        rot_map = {
            "+RX": np.array([1.0, 0.0, 0.0], dtype=float),
            "-RX": np.array([-1.0, 0.0, 0.0], dtype=float),
            "+RY": np.array([0.0, 1.0, 0.0], dtype=float),
            "-RY": np.array([0.0, -1.0, 0.0], dtype=float),
            "+RZ": np.array([0.0, 0.0, 1.0], dtype=float),
            "-RZ": np.array([0.0, 0.0, -1.0], dtype=float),
        }
        if key_norm not in trans_map and key_norm not in rot_map:
            return

        step_mm = max(0.1, float(self.quick_page.step_dist_spin.value()))
        step_rad = math.radians(max(0.1, float(self.quick_page.step_angle_spin.value())))
        coord_mode = str(self.quick_page.coord_combo.currentText()).strip().lower()
        use_tool = coord_mode.startswith("tool")
        speed_scale = max(0.1, float(self.speed_percent) / 100.0)
        is_continuous = self._quick_jog_mode_is_continuous()
        if is_continuous:
            duration = float(np.clip(0.15 / speed_scale, 0.08, 0.5))
        else:
            duration = float(np.clip(0.20 / speed_scale, 0.08, 0.60))

        self._enqueue_sdk_command(
            {
                "kind": "cartesian_jog",
                "source": f"jog:{key_norm}",
                "key": key_norm,
                "step_mm": float(step_mm),
                "step_rad": float(step_rad),
                "use_tool": bool(use_tool),
                "duration": duration,
                "wait_motion": not is_continuous,
            },
            drop_kinds=("cartesian_jog", "stop") if is_continuous else ("stop",),
        )

    def _sdk_home_and_sync(self, duration: Optional[float] = None, source: str = "home") -> bool:
        self._on_quick_jog_released(enqueue_stop=False)
        try:
            robot = self._sdk_get_robot()
            speed_scale = max(0.1, float(self.speed_percent) / 100.0)
            duration_val = float(duration) if duration is not None else float(np.clip(1.8 / speed_scale, 0.5, 5.0))
            robot.home(duration=duration_val, wait=True)
            state_after = robot.get_state()
            self._sync_sim_from_sdk_state(state_after)
            self._sdk_last_gui_sync_ts = time.time()
            self.statusBar().showMessage("Robot moved to home")
            self.log(f"[SDK {source}] moved to home", "success")
            return True
        except Exception as exc:
            self.statusBar().showMessage(f"Home failed: {exc}")
            self.log(f"[SDK {source}] {exc}", "warning")
            return False

    def _enqueue_sdk_home(self, duration: Optional[float] = None, source: str = "home"):
        self._on_quick_jog_released(enqueue_stop=False)
        self._clear_pending_sdk_commands()
        speed_scale = max(0.1, float(self.speed_percent) / 100.0)
        duration_val = float(duration) if duration is not None else float(np.clip(1.8 / speed_scale, 0.5, 5.0))
        self._enqueue_sdk_command(
            {
                "kind": "home",
                "source": f"home:{source}",
                "duration": duration_val,
            }
        )

    def _on_quick_origin(self):
        self._enqueue_sdk_home(source="origin")

    def _on_quick_zero(self):
        self._enqueue_sdk_home(source="zero")

    def _on_quick_free_move(self):
        combo = getattr(self.quick_page, "step_mode_combo", None)
        if combo is None:
            return
        next_idx = 0 if int(combo.currentIndex()) == 1 else 1
        combo.setCurrentIndex(next_idx)
        if next_idx == 1:
            self.statusBar().showMessage("Jog mode: Continuous")
        else:
            self.statusBar().showMessage("Jog mode: Step")

    def _sync_quick_joint_panel(self):
        for idx, (name_label, _minus_btn, value_label, _plus_btn) in enumerate(self.quick_page.joint_rows):
            if idx < len(self.sim_joint_names):
                name_label.setText(f"J{idx + 1}")
                name_label.setToolTip(self.sim_joint_names[idx])
                value_label.setText(f"{self.sim_q[idx]:.3f}")
            else:
                name_label.setText(f"J{idx + 1}")
                name_label.setToolTip("")
                value_label.setText("--")

    def _apply_vtk_visual_settings(self):
        if self._sim_backend != "vtk" or self.sim_vtk_view is None:
            return

        aa_data = self.settings_page.aa_mode_combo.currentData()
        if isinstance(aa_data, tuple) and len(aa_data) == 2:
            aa_mode, aa_samples = aa_data
        else:
            aa_mode, aa_samples = "fxaa", 0

        material = self.settings_page.material_preset_combo.currentData() or "soft"
        background = self.settings_page.background_theme_combo.currentData() or "studio"

        if hasattr(self.sim_vtk_view, "set_ui_theme"):
            self.sim_vtk_view.set_ui_theme(self.current_theme)
        self.sim_vtk_view.set_antialiasing(mode=str(aa_mode), samples=int(aa_samples), announce=False)
        self.sim_vtk_view.set_background(str(background))
        self.sim_vtk_view.set_material_preset(str(material))

    def _apply_vtk_camera_preset(self):
        if self._sim_backend != "vtk" or self.sim_vtk_view is None:
            return
        preset = self.settings_page.camera_preset_combo.currentData() or "iso"
        self.sim_vtk_view.set_camera_preset(str(preset))

    def _resolved_sdk_config(self):
        if not SDK_AVAILABLE or resolve_sdk_config is None:
            return None
        cfg_path = self._sdk_runtime_config_path or self._sdk_config_path
        try:
            return resolve_sdk_config(cfg_path or None)
        except Exception:
            return None

    def _resolved_serial_port_text(self) -> str:
        cfg = self._resolved_sdk_config()
        if cfg is None:
            return "--"
        port = str(getattr(cfg, "port", "") or "").strip()
        return port or "--"

    def _runtime_summary_text(self) -> str:
        status = self._status_message_for_runtime()
        if (not self.connecting) and (not self.connected) and self._sdk_last_connect_error:
            return f"{status} | {self._sdk_last_connect_error}"
        return status

    def _refresh_settings_runtime_summary(self):
        self.settings_page.set_runtime_summary(
            status_text=self._runtime_summary_text(),
            config_path=self._sdk_runtime_config_path or self._sdk_config_path or "",
            serial_port=self._resolved_serial_port_text(),
        )

    def _update_connection_widgets(self):
        runtime_connected = bool(self.connected or self._sdk_runtime_mode == "simulation")
        self.settings_page.set_connected(runtime_connected)
        self.quick_page.set_motion_enabled(runtime_connected)
        self.quick_page.set_cartesian_enabled(runtime_connected)
        self._set_connect_btn_text()
        self._refresh_settings_runtime_summary()

    def _state_connection_text(self) -> str:
        if self.connecting:
            return self._tr("state_connecting")
        if self.connected:
            return self._tr("state_connected")
        if self._sdk_runtime_mode == "simulation":
            return self._tr("state_simulation")
        return self._tr("state_disconnected")

    def _state_robot_text(self) -> str:
        lowered = self.robot_state_text.lower()
        if lowered == "warning":
            return self._tr("state_warning")
        if lowered == "fault":
            return self._tr("state_fault")
        if lowered in ("e-stop", "estop"):
            return self._tr("state_estop")
        return self._tr("state_normal")

    def _update_global_status_bar(self):
        conn = self._state_connection_text()
        robot = self._state_robot_text()

        self.global_status.set_connection(f"{self._tr('global_connection')}: {conn}")
        self.global_status.set_robot(f"{self._tr('global_robot')}: {robot}")
        self.global_status.set_mode(f"{self._tr('global_mode')}: {self.mode_text}")
        self.global_status.set_rtt(f"{self._tr('global_rtt')}: {self.last_rtt_text}")
        self.global_status.set_speed(f"{self._tr('global_speed')}: {self.speed_percent}%")
        self.global_status.set_owner(f"{self._tr('global_owner')}: {self.control_owner_text}")

        if robot == self._tr("state_fault"):
            self.quick_page.set_status_light("fault")
        elif robot == self._tr("state_warning"):
            self.quick_page.set_status_light("warning")
        else:
            self.quick_page.set_status_light("normal")

    def _status_message_for_runtime(self) -> str:
        if self.connecting:
            return self._tr("status_connecting")
        if self.connected:
            return self._tr("status_connected")
        if self._sdk_runtime_mode == "simulation":
            return self._tr("status_simulation")
        return self._tr("status_ready")

    def _bootstrap_sdk_connection(self):
        # Keep the hook for future lightweight preflight work, but do not
        # auto-connect the real arm on GUI startup. Hardware connection is now
        # lazy: it happens on the first explicit control action or manual connect.
        return

    # ==================== Camera window ====================

    def _build_placeholder_pixmap(self, target_label: QLabel) -> Optional[QPixmap]:
        if not LOGO_PATH.exists():
            return None
        pixmap = QPixmap(str(LOGO_PATH))
        if pixmap.isNull():
            return None
        if pixmap.width() > 1000 or pixmap.height() > 1000:
            pixmap = pixmap.scaled(600, 600, Qt.KeepAspectRatio, Qt.FastTransformation)
        return pixmap.scaled(
            max(300, target_label.width()),
            max(300, target_label.height()),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

    def _using_virtual_vtk_camera(self) -> bool:
        return self._sim_backend == "vtk" and self.sim_vtk_view is not None

    def _fetch_virtual_vtk_frame(self) -> Optional[np.ndarray]:
        if not self._using_virtual_vtk_camera():
            return None

        width = 960
        height = 720
        if self.camera_window is not None:
            width = max(320, int(self.camera_window.camera_label.width()))
            height = max(240, int(self.camera_window.camera_label.height()))
        try:
            frame = self.sim_vtk_view.render_eye_in_hand_frame(width=width, height=height)
        except Exception:
            return None
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        return frame

    def _display_camera_frame(self, frame: np.ndarray, fps: float, latency: float):
        self.last_frame = frame.copy()
        self.last_frame_fps = float(fps)
        self.last_frame_latency = float(latency)

        if self._is_camera_window_visible():
            self.camera_window.set_frame(
                frame,
                f"FPS: {int(max(0.0, fps))}",
                self._tr("latency_value", value=float(latency)),
                f"RTT: {self.last_rtt_text}",
                False,
            )

    def _on_virtual_cam_tick(self):
        if not self._is_camera_window_visible():
            return
        if not self._using_virtual_vtk_camera():
            return

        frame = self._fetch_virtual_vtk_frame()
        if frame is None:
            return

        now = time.time()
        dt = max(1e-3, now - float(self._virtual_cam_tick_ts))
        self._virtual_cam_tick_ts = now
        fps = 1.0 / dt
        self._display_camera_frame(frame, fps=fps, latency=0.0)

    def _ensure_camera_window(self):
        if self.camera_window is None:
            self.camera_window = CameraWindow(
                self._tr("camera_window_title"),
                self._tr("fps_default"),
                self._tr("latency_default"),
            )
            self.camera_window.setStyleSheet(get_stylesheet(self.current_theme))
            apply_soft_effects(self.camera_window, self.current_theme)
            self.camera_window.closed.connect(self._on_camera_window_closed)
            pixmap = self._build_placeholder_pixmap(self.camera_window.camera_label)
            self.camera_window.set_placeholder(pixmap, self._tr("camera_disconnected"))
        return self.camera_window

    def _on_camera_window_closed(self):
        if self._camera_source_mode() == "v4l2":
            self._stop_camera_stream()
        self._set_camera_btn_text(False)

    def _is_camera_window_visible(self) -> bool:
        return self.camera_window is not None and self.camera_window.isVisible()

    def on_toggle_camera_window(self):
        cam = self._ensure_camera_window()
        if cam.isVisible():
            cam.close()
            self._set_camera_btn_text(False)
            return

        cam.show()
        cam.raise_()
        cam.activateWindow()
        self._set_camera_btn_text(True)

        if self._camera_source_mode() == "v4l2":
            self._start_camera_stream()
            return

        if self._using_virtual_vtk_camera():
            frame = self._fetch_virtual_vtk_frame()
            if frame is not None:
                self._virtual_cam_tick_ts = time.time()
                self._display_camera_frame(frame, fps=30.0, latency=0.0)
                return

        if self.last_frame is not None:
            self._display_camera_frame(
                self.last_frame,
                fps=float(self.last_frame_fps),
                latency=float(self.last_frame_latency),
            )
            return

        pixmap = self._build_placeholder_pixmap(cam.camera_label)
        cam.set_placeholder(pixmap, self._tr("camera_disconnected"))

    # ==================== Speech window ====================

    def _resolve_speech_icon_path(self) -> Optional[Path]:
        for path in SPEECH_ICON_PATHS:
            if path.exists():
                return path
        return None

    def _ensure_speech_window(self):
        if self.speech_window is None:
            self.speech_window = SpeechInputWindow(
                self._tr("speech_window_title"),
                self._resolve_speech_icon_path(),
            )
            self.speech_window.set_theme(self.current_theme)
            self.speech_window.closed.connect(self._on_speech_window_closed)
            if hasattr(self.speech_window, "transcript_ready"):
                self.speech_window.transcript_ready.connect(self._on_speech_transcript_ready)
            if hasattr(self.speech_window, "transcript_failed"):
                self.speech_window.transcript_failed.connect(self._on_speech_transcript_failed)
            if hasattr(self.speech_window, "agent_reply_ready"):
                self.speech_window.agent_reply_ready.connect(self._on_speech_agent_reply_ready)
            if hasattr(self.speech_window, "agent_failed"):
                self.speech_window.agent_failed.connect(self._on_speech_agent_failed)
            if hasattr(self.speech_window, "agent_session_changed"):
                self.speech_window.agent_session_changed.connect(self._on_speech_agent_session_changed)
            if hasattr(self.speech_window, "tts_failed"):
                self.speech_window.tts_failed.connect(self._on_speech_tts_failed)
        return self.speech_window

    def _on_speech_window_closed(self):
        speech = self.speech_window
        self.speech_window = None
        self._set_speech_btn_text(False)
        if speech is not None:
            try:
                speech.deleteLater()
            except Exception:
                pass

    def _on_speech_transcript_ready(self, text: str):
        text = str(text or "").strip()
        if text:
            self.log(f"[Speech] {text}", "info")
            self.statusBar().showMessage(f"Speech: {text[:80]}")

    def _on_speech_transcript_failed(self, message: str):
        msg = str(message or "").strip()
        if not msg:
            msg = "Speech recognition failed"
        self.log(f"[Speech] {msg}", "error")
        self.statusBar().showMessage(msg)

    def _on_speech_agent_reply_ready(self, text: str):
        msg = str(text or "").strip()
        if not msg:
            return
        self.log(f"[OpenClaw] {msg}", "success")
        self.statusBar().showMessage(f"OpenClaw: {msg[:80]}")

    def _on_speech_agent_failed(self, message: str):
        msg = str(message or "").strip() or "OpenClaw invocation failed"
        self.log(f"[OpenClaw] {msg}", "error")
        self.statusBar().showMessage(msg)

    def _on_speech_agent_session_changed(self, session_id: str):
        sid = str(session_id or "").strip()
        if sid:
            self.log(f"[OpenClaw] Session: {sid}", "info")

    def _on_speech_tts_failed(self, message: str):
        msg = str(message or "").strip() or "TTS playback failed"
        self.log(f"[Speech:TTS] {msg}", "error")
        self.statusBar().showMessage(msg)

    @staticmethod
    def _tool_to_float(value, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _tool_to_int(value, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _tool_to_optional_float(value, default: Optional[float] = None) -> Optional[float]:
        if value is None:
            return default
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _sdk_find_joint_index(joint_names: List[str], joint_name: str) -> Optional[int]:
        key = str(joint_name or "").strip().lower()
        if not key:
            return None
        for i, name in enumerate(joint_names):
            if str(name).strip().lower() == key:
                return i
        for i, name in enumerate(joint_names):
            if key in str(name).strip().lower():
                return i
        return None

    def _sdk_transport_name(self, robot: SDKRobot) -> str:
        transport_name = getattr(robot, "transport_name", None)
        if isinstance(transport_name, str) and transport_name.strip():
            return transport_name
        transport = getattr(robot, "_transport", None)
        if transport is None:
            return "uninitialized"
        return type(transport).__name__

    def _sdk_error_result(self, exc: Exception) -> Dict[str, object]:
        msg = str(exc).strip() or exc.__class__.__name__
        return {"ok": False, "error": msg, "error_type": exc.__class__.__name__, "backend": "sdk"}

    def _sdk_set_runtime_state(
        self,
        mode: str,
        *,
        robot: Optional[SDKRobot] = None,
        config_path: Optional[str] = None,
        error_text: str = "",
    ):
        mode_norm = str(mode or "").strip().lower()
        if mode_norm not in ("connected", "simulation", "disconnected"):
            mode_norm = "disconnected"
        self._sdk_runtime_mode = mode_norm
        self._sdk_last_connect_error = str(error_text or "").strip()
        if config_path is not None:
            self._sdk_runtime_config_path = str(config_path or "").strip()
        if robot is not None:
            self._sdk_runtime_transport = self._sdk_transport_name(robot)
        elif mode_norm == "disconnected":
            self._sdk_runtime_transport = ""

    def _sdk_build_robot(self, config_path: Optional[str]) -> SDKRobot:
        return RuntimeRobot.from_config(config_path) if config_path else RuntimeRobot()

    def _sdk_connect_robot(self) -> SDKRobot:
        primary_cfg = self._sdk_config_path
        with self._sdk_lock:
            try:
                robot = self._sdk_build_robot(primary_cfg)
                robot.connect()
                transport_name = self._sdk_transport_name(robot).lower()
                mode = "simulation" if "mock" in transport_name else "connected"
                self._sdk_set_runtime_state(mode, robot=robot, config_path=primary_cfg)
                self._sdk_robot = robot
                return robot
            except Exception as exc:
                self._sdk_set_runtime_state("disconnected", config_path=primary_cfg, error_text=str(exc))
                self._sdk_robot = None
                raise

    def _sdk_get_robot(self) -> SDKRobot:
        if not SDK_AVAILABLE:
            raise RuntimeError(f"soarmmoce_sdk import failed: {_SDK_IMPORT_ERROR}")
        if RuntimeRobot is None:
            raise RuntimeError("soarmmoce_sdk runtime class is unavailable")

        with self._sdk_lock:
            if not self.connected and self._sdk_runtime_mode != "simulation":
                raise RuntimeError("Real arm is not connected. Click Connect first.")
            if self._sdk_robot is None:
                raise RuntimeError("Real arm session is not initialized. Click Connect first.")

            if not self._sdk_robot.connected:
                try:
                    self._sdk_robot.connect()
                except Exception as exc:
                    self._sdk_set_runtime_state(
                        "disconnected",
                        config_path=self._sdk_runtime_config_path,
                        error_text=str(exc),
                    )
                    self._sdk_robot = None
                    self.connected = False
                    raise RuntimeError(f"Real arm session was lost: {exc}. Click Connect again.") from exc
                transport_name = self._sdk_transport_name(self._sdk_robot).lower()
                mode = "simulation" if "mock" in transport_name else "connected"
                self._sdk_set_runtime_state(mode, robot=self._sdk_robot, config_path=self._sdk_runtime_config_path)

            return self._sdk_robot

    def _sdk_disconnect_robot(self):
        with self._sdk_lock:
            if self._sdk_robot is None:
                self._sdk_set_runtime_state("disconnected")
                return
            try:
                self._sdk_robot.disconnect()
            except Exception:
                pass
            finally:
                self._sdk_robot = None
                self._sdk_set_runtime_state("disconnected")

    def _resolve_sim_home_q(self) -> Optional[np.ndarray]:
        if not SDK_AVAILABLE or RuntimeRobot is None or not self.sim_joint_names:
            return None
        try:
            robot = RuntimeRobot.from_config(self._sdk_config_path) if self._sdk_config_path else RuntimeRobot()
            q_sdk = np.asarray(robot._resolve_home_q(), dtype=float).reshape(-1)  # type: ignore[attr-defined]
            sdk_joint_names = list(getattr(robot.robot_model, "joint_names", []) or [])
            if q_sdk.shape[0] == 0 or not sdk_joint_names:
                return None
            robot_cfg = robot.config.get("robot", {}) if isinstance(robot.config, dict) else {}
            alias_cfg = robot_cfg.get("joint_name_aliases", {}) if isinstance(robot_cfg, dict) else {}
            q_sim = np.zeros(len(self.sim_joint_names), dtype=float)
            for sim_idx, sim_name in enumerate(self.sim_joint_names):
                sdk_idx = self._sdk_find_joint_index(sdk_joint_names, sim_name)
                if sdk_idx is None and isinstance(alias_cfg, dict):
                    sim_key = str(sim_name).strip().lower()
                    for candidate_idx, sdk_name in enumerate(sdk_joint_names):
                        alias_name = alias_cfg.get(sdk_name, sdk_name)
                        alias_key = str(alias_name).strip().lower()
                        if sim_key == alias_key or sim_key in alias_key or alias_key in sim_key:
                            sdk_idx = candidate_idx
                            break
                if sdk_idx is not None and sdk_idx < q_sdk.shape[0]:
                    q_sim[sim_idx] = float(q_sdk[sdk_idx])
                elif sim_idx < len(self.sim_q):
                    q_sim[sim_idx] = float(self.sim_q[sim_idx])
            for idx, (lo, hi) in enumerate(self._sim_limits[: q_sim.shape[0]]):
                q_sim[idx] = float(np.clip(q_sim[idx], float(lo), float(hi)))
            return q_sim
        except Exception:
            return None

    def _resolve_sim_joint_offsets(self, joint_names: List[str]) -> np.ndarray:
        # The corrected URDF is now the single source of truth for both sim and hardware.
        # GUI-side simulation offsets only create hidden discrepancies, especially on wrist_flex.
        return np.zeros(len(joint_names), dtype=float)

    def _sim_limits_from_model_limits(self, model_limits: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        out: List[Tuple[float, float]] = []
        for idx, (lo, hi) in enumerate(model_limits):
            off = float(self._sim_joint_offsets[idx]) if idx < len(self._sim_joint_offsets) else 0.0
            out.append((float(lo - off), float(hi - off)))
        return out

    def _sim_user_to_model_q(self, q_src: np.ndarray) -> np.ndarray:
        q_user = np.asarray(q_src, dtype=float).reshape(-1)
        q_model = q_user.copy()
        n = min(q_model.shape[0], len(self._sim_joint_offsets))
        if n > 0:
            q_model[:n] = q_model[:n] + self._sim_joint_offsets[:n]
        return q_model

    def _sim_model_to_user_q(self, q_src: np.ndarray) -> np.ndarray:
        q_model = np.asarray(q_src, dtype=float).reshape(-1)
        q_user = q_model.copy()
        n = min(q_user.shape[0], len(self._sim_joint_offsets))
        if n > 0:
            q_user[:n] = q_user[:n] - self._sim_joint_offsets[:n]
        return q_user

    @staticmethod
    def _smooth_fraction(fraction: float) -> float:
        t = min(1.0, max(0.0, float(fraction)))
        return t * t * (3.0 - 2.0 * t)

    def _extract_joint_q_from_sdk_state(self, state: object, *, prefer_twin: bool = False) -> Optional[np.ndarray]:
        candidates = []
        if prefer_twin:
            candidates.append(getattr(state, "twin", None))
        candidates.append(state)
        if not prefer_twin:
            candidates.append(getattr(state, "twin", None))
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                q = np.asarray(getattr(getattr(candidate, "joint_state"), "q"), dtype=float).reshape(-1)
            except Exception:
                continue
            if q.shape[0] > 0:
                return q
        return None

    def _extract_tcp_pose_from_sdk_state(
        self, state: object, *, prefer_twin: bool = False
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        candidates = []
        if prefer_twin:
            candidates.append(getattr(state, "twin", None))
        candidates.append(state)
        if not prefer_twin:
            candidates.append(getattr(state, "twin", None))
        for candidate in candidates:
            if candidate is None:
                continue
            pose = getattr(candidate, "tcp_pose", None)
            if pose is None:
                continue
            try:
                xyz = np.asarray(getattr(pose, "xyz"), dtype=float).reshape(3)
                rpy = np.asarray(getattr(pose, "rpy"), dtype=float).reshape(3)
            except Exception:
                continue
            return xyz, rpy
        return None

    def _apply_sim_joint_q(self, q_src: np.ndarray, *, update_plot: bool = True) -> bool:
        if not self._sim_ready:
            return False
        q = np.asarray(q_src, dtype=float).reshape(-1)
        if q.shape[0] == 0 or len(self.sim_q) == 0:
            return False
        q_dst = np.asarray(self.sim_q, dtype=float).copy()
        n = min(q_dst.shape[0], q.shape[0])
        q_dst[:n] = q[:n]
        for i in range(min(n, len(self._sim_limits))):
            lo, hi = self._sim_limits[i]
            q_dst[i] = float(np.clip(q_dst[i], float(lo), float(hi)))
        self.sim_q = q_dst
        if update_plot:
            self._update_sim_plot()
        return True

    def _cancel_sim_motion(self):
        self._sim_motion_active = False
        self._sim_motion_start_q = np.zeros(0, dtype=float)
        self._sim_motion_target_q = np.zeros(0, dtype=float)
        self._sim_motion_start_ts = 0.0
        self._sim_motion_end_ts = 0.0
        self._sim_motion_timer.stop()

    def _schedule_sim_motion(self, target_q: np.ndarray, duration: float):
        q_target_raw = np.asarray(target_q, dtype=float).reshape(-1)
        if not self._apply_sim_joint_q(np.asarray(self.sim_q, dtype=float), update_plot=False):
            return
        if q_target_raw.shape[0] == 0:
            return
        q_start = np.asarray(self.sim_q, dtype=float).copy()
        q_target = q_start.copy()
        n = min(q_target.shape[0], q_target_raw.shape[0])
        q_target[:n] = q_target_raw[:n]
        for i in range(min(n, len(self._sim_limits))):
            lo, hi = self._sim_limits[i]
            q_target[i] = float(np.clip(q_target[i], float(lo), float(hi)))
        duration = max(0.0, float(duration))
        if duration <= 1e-4:
            self._cancel_sim_motion()
            self._apply_sim_joint_q(q_target, update_plot=True)
            return
        self._sim_motion_start_q = q_start
        self._sim_motion_target_q = q_target.copy()
        self._sim_motion_start_ts = time.time()
        self._sim_motion_end_ts = self._sim_motion_start_ts + duration
        self._sim_motion_active = True
        if not self._sim_motion_timer.isActive():
            self._sim_motion_timer.start()

    def _on_sim_motion_tick(self):
        if not self._sim_motion_active:
            self._sim_motion_timer.stop()
            return
        now = time.time()
        if now >= self._sim_motion_end_ts or self._sim_motion_end_ts <= self._sim_motion_start_ts:
            target = self._sim_motion_target_q.copy()
            self._cancel_sim_motion()
            self._apply_sim_joint_q(target, update_plot=True)
            return
        span = max(1e-9, self._sim_motion_end_ts - self._sim_motion_start_ts)
        alpha = self._smooth_fraction((now - self._sim_motion_start_ts) / span)
        q_now = self._sim_motion_start_q + (self._sim_motion_target_q - self._sim_motion_start_q) * alpha
        self._apply_sim_joint_q(q_now, update_plot=True)

    def _handle_sim_motion_from_command(self, state: object, command: object, source: str):
        duration = 0.0
        if isinstance(command, dict):
            try:
                duration = max(0.0, float(command.get("duration", 0.0) or 0.0))
            except Exception:
                duration = 0.0

        q_target = self._extract_joint_q_from_sdk_state(state, prefer_twin=True)
        q_actual = self._extract_joint_q_from_sdk_state(state, prefer_twin=False)
        if (
            q_target is not None
            and q_actual is not None
            and duration > 1e-4
            and np.linalg.norm(np.asarray(q_target, dtype=float) - np.asarray(q_actual, dtype=float)) > 1e-5
        ):
            self._sdk_visual_prefer_twin_until_ts = time.time() + max(
                float(duration) + float(self._sdk_visual_twin_slack_sec),
                1.0,
            )
            self._schedule_sim_motion(np.asarray(q_target, dtype=float), duration)
            return

        self._sdk_visual_prefer_twin_until_ts = 0.0
        self._cancel_sim_motion()
        self._sync_sim_from_sdk_state(state)

    def _resolve_display_joint_q_from_sdk_state(self, state: object) -> Optional[np.ndarray]:
        q_actual = self._extract_joint_q_from_sdk_state(state, prefer_twin=False)
        q_twin = self._extract_joint_q_from_sdk_state(state, prefer_twin=True)
        if q_actual is None:
            return q_twin
        if q_twin is None:
            return q_actual
        q_actual_arr = np.asarray(q_actual, dtype=float)
        q_twin_arr = np.asarray(q_twin, dtype=float)
        diff = np.abs(q_twin_arr - q_actual_arr)
        if float(np.max(diff)) <= float(self._sdk_visual_twin_tol_rad):
            return q_actual
        if time.time() < float(self._sdk_visual_prefer_twin_until_ts):
            return q_twin_arr
        return q_actual

    def _sync_sim_from_sdk_state(self, state: object):
        q_src = self._resolve_display_joint_q_from_sdk_state(state)
        if q_src is None:
            return
        self._apply_sim_joint_q(q_src, update_plot=True)

    def _sync_gui_from_sdk(self, force: bool = False) -> bool:
        if self.connecting or (not self._sim_ready) or (not self._sdk_gui_sync_enabled):
            return False

        now = time.time()
        if (not force) and (now - float(self._sdk_last_gui_sync_ts) < float(self._sdk_gui_sync_interval_sec)):
            return False

        if not self._sdk_lock.acquire(blocking=False):
            return False
        try:
            robot = self._sdk_robot
            if robot is None or not getattr(robot, "connected", False):
                return False
            state = robot.get_state()
        except Exception as exc:
            if now - float(self._sdk_last_gui_sync_err_ts) >= 5.0:
                self._sdk_last_gui_sync_err_ts = now
                self.log(f"[SDK sync] {exc}", "warning")
            return False
        finally:
            self._sdk_lock.release()

        self._sync_sim_from_sdk_state(state)
        self._sdk_last_gui_sync_ts = now
        return True

    def _sdk_sync_loop(self):
        idle_sleep = 0.05
        while self._sdk_sync_running:
            if self.connecting or (not self._sdk_gui_sync_enabled) or (not self._sim_ready) or self._sim_motion_active:
                time.sleep(idle_sleep)
                continue
            if not self._sdk_lock.acquire(blocking=False):
                time.sleep(min(idle_sleep, self._sdk_gui_sync_interval_sec))
                continue
            state = None
            try:
                robot = self._sdk_robot
                if robot is None or not getattr(robot, "connected", False):
                    continue
                state = robot.get_state()
            except Exception as exc:
                now = time.time()
                if now - float(self._sdk_last_gui_sync_err_ts) >= 5.0:
                    self._sdk_last_gui_sync_err_ts = now
                    self.log_signal.emit(f"[SDK sync] {exc}", "warning")
            finally:
                self._sdk_lock.release()
            if state is None:
                time.sleep(self._sdk_gui_sync_interval_sec)
                continue
            self.sdk_sync_state_ready.emit(state)
            time.sleep(self._sdk_gui_sync_interval_sec)

    def _tool_find_joint_index(self, joint_name: str) -> Optional[int]:
        key = str(joint_name or "").strip().lower()
        if not key:
            return None
        for i, name in enumerate(self.sim_joint_names):
            if str(name).strip().lower() == key:
                return i
        for i, name in enumerate(self.sim_joint_names):
            if key in str(name).strip().lower():
                return i
        return None

    @staticmethod
    def _tool_estimate_red_object_score(frame_bgr: Optional[np.ndarray]) -> Dict[str, float]:
        if frame_bgr is None or not isinstance(frame_bgr, np.ndarray) or frame_bgr.size == 0:
            return {"score": 0.0, "red_ratio": 0.0, "area_ratio": 0.0}
        try:
            hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        except Exception:
            return {"score": 0.0, "red_ratio": 0.0, "area_ratio": 0.0}

        lower1 = np.array([0, 90, 50], dtype=np.uint8)
        upper1 = np.array([10, 255, 255], dtype=np.uint8)
        lower2 = np.array([160, 90, 50], dtype=np.uint8)
        upper2 = np.array([179, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower1, upper1) | cv2.inRange(hsv, lower2, upper2)
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        total = float(mask.shape[0] * mask.shape[1]) if mask.ndim == 2 else float(mask.size)
        if total <= 0.0:
            return {"score": 0.0, "red_ratio": 0.0, "area_ratio": 0.0}
        red_pixels = float(np.count_nonzero(mask))
        red_ratio = red_pixels / total

        contours_info = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = contours_info[-2] if len(contours_info) == 3 else contours_info[0]
        area_ratio = 0.0
        if contours:
            area_ratio = max(float(cv2.contourArea(c)) / total for c in contours)

        score = max(red_ratio * 12.0, area_ratio * 24.0)
        score = float(max(0.0, min(1.0, score)))
        return {"score": score, "red_ratio": float(red_ratio), "area_ratio": float(area_ratio)}

    def _tool_move_robot_arm_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        x = self._tool_to_float(payload.get("x", 0.0), 0.0)
        y = self._tool_to_float(payload.get("y", 0.0), 0.0)
        z = self._tool_to_float(payload.get("z", 0.0), 0.0)
        frame = str(payload.get("frame", "base") or "base").strip().lower()
        duration = max(0.2, min(20.0, self._tool_to_float(payload.get("duration", 2.0), 2.0)))
        wait = bool(payload.get("wait", True))
        timeout = self._tool_to_optional_float(payload.get("timeout"), None)
        if timeout is not None and timeout < 0.0:
            return False, {"ok": False, "error": "timeout must be >= 0"}

        if frame not in ("base", "tool"):
            return False, {"ok": False, "error": "frame must be 'base' or 'tool'"}

        try:
            robot = self._sdk_get_robot()
            if frame == "tool":
                robot.move_delta(
                    dx=float(x),
                    dy=float(y),
                    dz=float(z),
                    frame="tool",
                    duration=float(duration),
                    wait=bool(wait),
                    timeout=timeout,
                )
            else:
                robot.move_pose(
                    xyz=[float(x), float(y), float(z)],
                    rpy=None,
                    duration=float(duration),
                    wait=bool(wait),
                    timeout=timeout,
                )
            state = robot.get_state()
            self._sync_sim_from_sdk_state(state)
        except Exception as exc:
            result = self._sdk_error_result(exc)
            return False, result

        backend = f"sdk-{self._sdk_transport_name(robot)}"
        result = {
            "ok": True,
            "message": "cartesian move completed",
            "backend": backend,
            "frame": frame,
            "requested_xyz_m": [float(x), float(y), float(z)],
            "actual_xyz_m": [float(v) for v in np.asarray(state.tcp_pose.xyz, dtype=float).reshape(3).tolist()],
            "actual_rpy_rad": [float(v) for v in np.asarray(state.tcp_pose.rpy, dtype=float).reshape(3).tolist()],
            "duration": float(duration),
            "wait": bool(wait),
            "timeout": timeout,
        }
        return True, result

    def _tool_get_robot_state_main_thread(self) -> Tuple[bool, Dict[str, object]]:
        try:
            robot = self._sdk_get_robot()
            state = robot.get_state()
            self._sync_sim_from_sdk_state(state)
        except Exception as exc:
            result = self._sdk_error_result(exc)
            return False, result

        names = list(getattr(robot.robot_model, "joint_names", []) or [])
        joints: Dict[str, float] = {}
        q = np.asarray(state.joint_state.q, dtype=float).reshape(-1)
        for i, value in enumerate(q.tolist()):
            name = str(names[i]) if i < len(names) else f"joint_{i + 1}"
            joints[name] = float(value)

        gripper_state = state.gripper_state
        permissions = state.permissions
        result = {
            "ok": True,
            "backend": f"sdk-{self._sdk_transport_name(robot)}",
            "simulation_ready": bool(self._sim_ready),
            "connected": bool(state.connected),
            "timestamp": float(state.timestamp) if state.timestamp is not None else None,
            "joints_rad": joints,
            "ee_xyz_m": [float(v) for v in np.asarray(state.tcp_pose.xyz, dtype=float).tolist()],
            "ee_rpy_rad": [float(v) for v in np.asarray(state.tcp_pose.rpy, dtype=float).tolist()],
            "gripper": {
                "available": bool(getattr(gripper_state, "available", False)),
                "open_ratio": getattr(gripper_state, "open_ratio", None),
                "moving": getattr(gripper_state, "moving", None),
            },
            "permissions": {
                "allow_motion": bool(getattr(permissions, "allow_motion", True)),
                "allow_gripper": bool(getattr(permissions, "allow_gripper", True)),
                "allow_home": bool(getattr(permissions, "allow_home", True)),
                "allow_stop": bool(getattr(permissions, "allow_stop", True)),
            },
        }
        return True, result

    def _tool_get_camera_frame_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        source = str(payload.get("source", "eye_in_hand") or "eye_in_hand").strip().lower()
        width = max(160, min(1920, self._tool_to_int(payload.get("width", 960), 960)))
        height = max(120, min(1080, self._tool_to_int(payload.get("height", 720), 720)))
        fmt = str(payload.get("format", "jpg") or "jpg").strip().lower()
        mode = str(payload.get("return_mode", "path") or "path").strip().lower()
        if fmt not in ("jpg", "png"):
            return False, {"ok": False, "error": "format must be 'jpg' or 'png'"}
        if mode not in ("path", "base64"):
            return False, {"ok": False, "error": "return_mode must be 'path' or 'base64'"}

        frame = None
        if source == "eye_in_hand" and self._using_virtual_vtk_camera():
            try:
                frame = self.sim_vtk_view.render_eye_in_hand_frame(width=width, height=height)
            except Exception:
                frame = None
        if frame is None:
            if self.last_frame is not None:
                frame = self.last_frame.copy()
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return False, {"ok": False, "error": "no camera frame available"}

        ext = ".jpg" if fmt == "jpg" else ".png"
        ok_enc, encoded = cv2.imencode(ext, frame)
        if not ok_enc:
            return False, {"ok": False, "error": "failed to encode frame"}
        blob = bytes(encoded.tobytes())

        result: Dict[str, object] = {
            "ok": True,
            "backend": f"main-thread-{self._sim_backend}",
            "source": source,
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "format": fmt,
            "timestamp": float(time.time()),
        }
        if mode == "path":
            out_dir = Path("/tmp/mocearm_openclaw_frames")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"frame_{int(time.time() * 1000)}{ext}"
            out_path.write_bytes(blob)
            result["path"] = str(out_path.resolve())
        else:
            result["base64"] = base64.b64encode(blob).decode("ascii")
        return True, result

    def _tool_stop_robot_main_thread(self) -> Tuple[bool, Dict[str, object]]:
        try:
            self._on_quick_jog_released(enqueue_stop=False)
        except Exception:
            pass
        try:
            robot = self._sdk_get_robot()
            robot.stop()
            backend = f"sdk-{self._sdk_transport_name(robot)}"
            return True, {"ok": True, "message": "stopped", "backend": backend}
        except Exception as exc:
            result = self._sdk_error_result(exc)
            return False, result

    def _tool_set_gripper_main_thread(self, target: float) -> bool:
        idx = self._tool_find_joint_index("gripper")
        if idx is None or idx >= len(self._sim_limits):
            return False
        lo, hi = self._sim_limits[idx]
        span = float(hi) - float(lo)
        if abs(span) < 1e-8:
            return False
        ratio = float(np.clip((float(target) - float(lo)) / span, 0.0, 1.0))
        ok, _ = self._tool_set_gripper_tool_main_thread({"open_ratio": ratio, "wait": True})
        return bool(ok)

    def _tool_set_gripper_tool_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        ratio = max(0.0, min(1.0, self._tool_to_float(payload.get("open_ratio", 1.0), 1.0)))
        wait = bool(payload.get("wait", True))
        timeout = self._tool_to_optional_float(payload.get("timeout"), None)
        if timeout is not None and timeout < 0.0:
            return False, {"ok": False, "error": "timeout must be >= 0"}

        try:
            robot = self._sdk_get_robot()
            robot.set_gripper(open_ratio=float(ratio), wait=bool(wait), timeout=timeout)
            state = robot.get_state()
            self._sync_sim_from_sdk_state(state)
            backend = f"sdk-{self._sdk_transport_name(robot)}"
        except Exception as exc:
            result = self._sdk_error_result(exc)
            return False, result

        g = state.gripper_state
        ratio_now = getattr(g, "open_ratio", None)
        idx = self._tool_find_joint_index("gripper")
        q_now = float(np.asarray(self.sim_q, dtype=float)[idx]) if idx is not None and len(self.sim_q) > idx else None
        joint_name = str(self.sim_joint_names[idx]) if idx is not None and idx < len(self.sim_joint_names) else "gripper"
        result = {
            "ok": True,
            "message": "gripper command completed",
            "backend": backend,
            "joint_name": joint_name,
            "wait": bool(wait),
            "timeout": timeout,
            "open_ratio_target": float(ratio),
            "open_ratio_actual": float(ratio_now) if ratio_now is not None else None,
            "joint_position_rad": q_now,
            "gripper_available": bool(getattr(g, "available", False)),
        }
        return True, result

    def _tool_rotate_joint_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        joint_name = str(payload.get("joint_name", "") or "").strip()
        if not joint_name:
            return False, {"ok": False, "error": "joint_name is required"}

        has_target = "target_deg" in payload and payload.get("target_deg") is not None
        delta_deg = self._tool_to_float(payload.get("delta_deg", 0.0), 0.0)
        target_deg = self._tool_to_optional_float(payload.get("target_deg"), None)
        wait = bool(payload.get("wait", True))
        timeout = self._tool_to_optional_float(payload.get("timeout"), None)
        duration = max(0.2, min(20.0, self._tool_to_float(payload.get("duration", 1.0), 1.0)))
        if timeout is not None and timeout < 0.0:
            return False, {"ok": False, "error": "timeout must be >= 0"}

        try:
            robot = self._sdk_get_robot()
            state_before = robot.get_state()
            joint_names = list(getattr(robot.robot_model, "joint_names", []) or [])
            idx = self._sdk_find_joint_index(joint_names, joint_name)
            if idx is None:
                return False, {"ok": False, "error": f"joint not found: {joint_name}"}
            q_before = float(np.asarray(state_before.joint_state.q, dtype=float)[idx])
            lo, hi = robot.robot_model.joint_limits[idx]

            if has_target:
                requested_target_deg = float(target_deg if target_deg is not None else math.degrees(q_before))
                requested_delta_deg = float(requested_target_deg - math.degrees(q_before))
                q_new = robot.rotate_joint(
                    joint=joint_names[idx],
                    target_deg=requested_target_deg,
                    duration=duration,
                    wait=wait,
                    timeout=timeout,
                )
            else:
                requested_delta_deg = float(delta_deg)
                requested_target_deg = float(math.degrees(q_before) + requested_delta_deg)
                q_new = robot.rotate_joint(
                    joint=joint_names[idx],
                    delta_deg=requested_delta_deg,
                    duration=duration,
                    wait=wait,
                    timeout=timeout,
                )

            state_after = robot.get_state()
            self._sync_sim_from_sdk_state(state_after)
        except Exception as exc:
            result = self._sdk_error_result(exc)
            return False, result

        q_after = float(np.asarray(q_new, dtype=float)[idx])
        actual_target_deg = float(math.degrees(q_after))
        clipped = bool(abs(actual_target_deg - requested_target_deg) > 1e-8)
        backend = f"sdk-{self._sdk_transport_name(robot)}"

        result = {
            "ok": True,
            "message": "joint rotated",
            "backend": backend,
            "joint_name": str(joint_names[idx]),
            "wait": bool(wait),
            "timeout": timeout,
            "requested_delta_deg": float(requested_delta_deg),
            "requested_target_deg": float(requested_target_deg),
            "actual_target_deg": float(actual_target_deg),
            "clipped": clipped,
            "joint_limits_deg": [float(math.degrees(lo)), float(math.degrees(hi))],
        }
        return True, result

    def _tool_scan_for_object_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        object_name = str(payload.get("object_name", "") or "").strip()
        if not object_name:
            return False, {"ok": False, "error": "object_name is required"}

        joint_name = str(payload.get("joint_name", "wrist_roll") or "wrist_roll").strip()
        try:
            robot = self._sdk_get_robot()
            state0 = robot.get_state()
        except Exception as exc:
            result = self._sdk_error_result(exc)
            return False, result

        joint_names = list(getattr(robot.robot_model, "joint_names", []) or [])
        idx = self._sdk_find_joint_index(joint_names, joint_name)
        if idx is None:
            idx = self._sdk_find_joint_index(joint_names, "wrist_roll")
        if idx is None:
            return False, {"ok": False, "error": "scan joint not found (expected wrist_roll)"}

        source = str(payload.get("source", "eye_in_hand") or "eye_in_hand").strip().lower()
        if source not in ("eye_in_hand", "scene"):
            source = "eye_in_hand"
        width = max(160, min(1920, self._tool_to_int(payload.get("width", 960), 960)))
        height = max(120, min(1080, self._tool_to_int(payload.get("height", 720), 720)))
        sweep_range_deg = max(10.0, min(240.0, self._tool_to_float(payload.get("sweep_range_deg", 120.0), 120.0)))
        step_deg = max(2.0, min(60.0, self._tool_to_float(payload.get("step_deg", 15.0), 15.0)))
        return_to_start = bool(payload.get("return_to_start", False))

        q_start = float(np.asarray(state0.joint_state.q, dtype=float)[idx])
        start_deg = float(math.degrees(q_start))
        half = sweep_range_deg / 2.0
        n_pts = int(max(3, min(25, round(sweep_range_deg / step_deg) + 1)))
        offsets = np.linspace(-half, half, num=n_pts).tolist()

        name_lower = object_name.lower()
        red_target = ("red" in name_lower) or ("apple" in name_lower) or ("红" in object_name) or ("苹果" in object_name)
        samples: List[Dict[str, object]] = []
        best_score = -1.0
        best_angle_deg = start_deg
        best_frame_path = ""

        for off in offsets:
            target_deg = float(start_deg + float(off))
            ok_rot, rot_result = self._tool_rotate_joint_main_thread(
                {"joint_name": str(joint_names[idx]), "target_deg": target_deg, "wait": True}
            )
            if not ok_rot:
                samples.append({"angle_deg": target_deg, "ok": False, "score": 0.0})
                continue

            ok_frame, frame_result = self._tool_get_camera_frame_main_thread(
                {
                    "source": source,
                    "width": width,
                    "height": height,
                    "format": "jpg",
                    "return_mode": "path",
                }
            )
            frame_path = str(frame_result.get("path", "")) if ok_frame else ""
            frame_img = cv2.imread(frame_path) if frame_path else None
            det = self._tool_estimate_red_object_score(frame_img) if red_target else {"score": 0.0, "red_ratio": 0.0, "area_ratio": 0.0}
            score = float(det.get("score", 0.0))

            if score > best_score:
                best_score = score
                best_angle_deg = float(rot_result.get("actual_target_deg", target_deg))
                best_frame_path = frame_path
            samples.append(
                {
                    "angle_deg": float(rot_result.get("actual_target_deg", target_deg)),
                    "ok": bool(ok_frame),
                    "score": score,
                    "red_ratio": float(det.get("red_ratio", 0.0)),
                    "area_ratio": float(det.get("area_ratio", 0.0)),
                }
            )

        found = bool(best_score >= 0.12 and red_target)
        if return_to_start:
            self._tool_rotate_joint_main_thread(
                {"joint_name": str(joint_names[idx]), "target_deg": start_deg, "wait": True}
            )
        elif found:
            self._tool_rotate_joint_main_thread(
                {"joint_name": str(joint_names[idx]), "target_deg": best_angle_deg, "wait": True}
            )

        result = {
            "ok": True,
            "backend": f"sdk-{self._sdk_transport_name(robot)}",
            "message": "scan completed",
            "object_name": object_name,
            "detector": "red_hsv" if red_target else "unsupported_object_name",
            "scan_joint": str(joint_names[idx]),
            "source": source,
            "found": found,
            "confidence": float(max(0.0, best_score)),
            "start_angle_deg": float(start_deg),
            "best_angle_deg": float(best_angle_deg),
            "sweep_range_deg": float(sweep_range_deg),
            "step_deg": float(step_deg),
            "best_frame_path": best_frame_path,
            "samples": samples,
        }
        if not red_target:
            result["found"] = False
            result["message"] = "scan completed (no detector for object_name; only red object detector enabled)"
        return True, result

    def _tool_run_skill_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        raw_name = str(payload.get("name", "") or "").strip().lower()
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}
        if not raw_name:
            return False, {"ok": False, "error": "run_skill requires non-empty name"}

        if raw_name in ("dance_short", "dance", "wave"):
            return (
                False,
                {
                    "ok": False,
                    "skill": raw_name,
                    "error": (
                        "This skill depends on Cartesian motion, which is not available in the current SDK build. "
                        "Use rotate_joint / set_gripper, or add an IK-backed Cartesian layer first."
                    ),
                },
            )

        if raw_name in ("grasp_apple", "grasp_apple_mock", "pick_apple"):
            return (
                False,
                {
                    "ok": False,
                    "skill": raw_name,
                    "error": (
                        "This skill depends on Cartesian motion, which is not available in the current SDK build. "
                        "Keep using joint-level tools for now, or add an IK-backed Cartesian layer first."
                    ),
                },
            )

        return False, {"ok": False, "error": f"unsupported skill: {raw_name}"}

    def _is_speech_window_visible(self) -> bool:
        return self.speech_window is not None and self.speech_window.isVisible()

    def on_toggle_speech_window(self):
        speech = self._ensure_speech_window()
        if speech.isVisible():
            speech.close()
            self._set_speech_btn_text(False)
            return

        main_geo = self.frameGeometry()
        pos_x = max(0, main_geo.right() - speech.width() - 24)
        pos_y = max(0, main_geo.top() + 88)
        speech.move(pos_x, pos_y)
        speech.show()
        speech.raise_()
        speech.activateWindow()
        self._set_speech_btn_text(True)

    # ==================== Logging ====================

    def log(self, message: str, level: str = "info"):
        self.log_signal.emit(message, level)

    def _append_log(self, message: str, level: str):
        timestamp = time.strftime("%H:%M:%S")
        if self.current_theme == "dark":
            colors = {
                "info": "#D7E1EE",
                "success": "#34D399",
                "warning": "#FBBF24",
                "error": "#F87171",
            }
            time_color = "#7E93AB"
            default_color = "#D7E1EE"
        else:
            colors = {
                "info": "#1E293B",
                "success": "#059669",
                "warning": "#D97706",
                "error": "#DC2626",
            }
            time_color = "#94A3B8"
            default_color = "#1E293B"
        color = colors.get(level, default_color)
        line = (
            f'<span style="color: {time_color}">[{timestamp}]</span> '
            f'<span style="color: {color}">{message}</span>'
        )
        self.log_dock_view.append(line)
        self.log_dock_view.verticalScrollBar().setValue(self.log_dock_view.verticalScrollBar().maximum())

    # ==================== Connection ====================

    def on_toggle_connection(self):
        if self.connecting:
            return
        if self.connected or self._sdk_runtime_mode == "simulation" or self._sdk_robot is not None:
            self.on_disconnect()
            return
        self.on_connect()

    def on_connect(self):
        if self.connected or self.connecting:
            return

        self.connecting = True
        self._sdk_last_connect_error = ""
        self._update_connection_widgets()
        self._update_global_status_bar()
        self.statusBar().showMessage(self._tr("status_connecting"))
        self.log(self._tr("log_connecting"))

        def connect_task():
            try:
                self._sdk_connect_robot()
                self.sdk_connection_finished.emit(True, "")
            except Exception as exc:
                self._sdk_disconnect_robot()
                self.sdk_connection_finished.emit(False, str(exc))

        threading.Thread(target=connect_task, daemon=True).start()

    def _on_sdk_connection_finished(self, success: bool, message: str):
        self.connecting = False
        self.connected = bool(success and self._sdk_runtime_mode == "connected")
        self._sdk_last_gui_sync_ts = 0.0
        if success:
            self.log(self._tr("log_connect_success"), "success")
            self._on_connected()
            return
        self.log(self._tr("log_connect_failed", error=message or "unknown error"), "error")
        self._on_connect_failed()

    def _on_connected(self):
        self._update_connection_widgets()
        self.statusBar().showMessage(self._status_message_for_runtime())
        self._update_global_status_bar()

    def _on_connect_failed(self):
        self._update_connection_widgets()
        self.statusBar().showMessage(self._status_message_for_runtime())
        self._update_global_status_bar()

    def on_disconnect(self):
        if not self.connected and not self.connecting and self._sdk_robot is None:
            return

        self._on_quick_jog_released(enqueue_stop=False)
        self._clear_pending_sdk_commands()
        self.log(self._tr("log_disconnecting"))

        self._sdk_disconnect_robot()

        self.connected = False
        self.connecting = False
        self.last_rtt_text = "--"

        if self.camera_window is not None:
            if self._using_virtual_vtk_camera():
                frame = self._fetch_virtual_vtk_frame()
                if frame is not None:
                    self._virtual_cam_tick_ts = time.time()
                    self._display_camera_frame(frame, fps=30.0, latency=0.0)
                else:
                    pixmap = self._build_placeholder_pixmap(self.camera_window.camera_label)
                    self.camera_window.set_placeholder(pixmap, self._tr("camera_disconnected"))
            elif self._camera_source_mode() == "virtual":
                pixmap = self._build_placeholder_pixmap(self.camera_window.camera_label)
                self.camera_window.set_placeholder(pixmap, self._tr("camera_disconnected"))

        self.log(self._tr("log_disconnected"), "warning")
        self.statusBar().showMessage(self._status_message_for_runtime())
        self._update_connection_widgets()
        self._update_global_status_bar()

    def on_home(self):
        self._enqueue_sdk_home(source="legacy-home")

    # ==================== Camera frames ====================

    def update_camera(self, frame: np.ndarray, latency: float, fps: float):
        if self._using_virtual_vtk_camera():
            return
        self._display_camera_frame(frame, fps=float(fps), latency=float(latency))

    def _camera_source_mode(self) -> str:
        if not hasattr(self.settings_page, "camera_source_combo"):
            return "virtual"
        value = str(self.settings_page.camera_source_combo.currentData() or "virtual").strip().lower()
        return value if value in ("virtual", "v4l2") else "virtual"

    def _camera_rotation_deg(self) -> int:
        if not hasattr(self.settings_page, "camera_rotation_combo"):
            return 0
        try:
            return int(self.settings_page.camera_rotation_combo.currentData() or 0)
        except Exception:
            return 0

    def _camera_device_value(self) -> str:
        if not hasattr(self.settings_page, "camera_device_input"):
            return ""
        return str(self.settings_page.camera_device_input.text() or "").strip()

    def on_camera_source_changed(self):
        if not self._is_camera_window_visible():
            return
        if self._camera_source_mode() == "v4l2":
            self._start_camera_stream()
            return
        self._stop_camera_stream()
        if self._using_virtual_vtk_camera():
            frame = self._fetch_virtual_vtk_frame()
            if frame is not None:
                self._display_camera_frame(frame, fps=30.0, latency=0.0)
                return
        if self.last_frame is not None:
            self._display_camera_frame(
                self.last_frame,
                fps=float(self.last_frame_fps),
                latency=float(self.last_frame_latency),
            )
            return
        pixmap = self._build_placeholder_pixmap(self.camera_window.camera_label)
        self.camera_window.set_placeholder(pixmap, self._tr("camera_disconnected"))

    def _create_camera_client(self):
        mode = self._camera_source_mode()
        if mode == "v4l2":
            if not V4L2_CAMERA_AVAILABLE or V4L2CameraReader is None:
                raise RuntimeError(f"Local V4L2 camera is unavailable: {_V4L2_CAMERA_IMPORT_ERROR}")
            raw_device = self._camera_device_value()
            device_path = raw_device if raw_device.startswith("/dev/video") else None
            name_hint = None if device_path else (raw_device or None)
            return V4L2CameraReader(
                device=device_path,
                name_hint=name_hint,
                rotation_deg=self._camera_rotation_deg(),
                width=1280,
                height=720,
                fps=30,
            )
        raise RuntimeError("Virtual preview does not need a physical camera client")

    def _start_camera_stream(self):
        if self._camera_source_mode() != "v4l2":
            return
        self._stop_camera_stream()
        try:
            self.video_client = self._create_camera_client()
            self.video_client.start()
            self.video_thread = VideoThread(self.video_client)
            self.video_thread.frame_ready.connect(self.update_camera)
            self.video_thread.start()
        except Exception as exc:
            self.video_client = None
            self.video_thread = None
            self.log(f"[Camera] {exc}", "warning")

    def _stop_camera_stream(self):
        if self.video_thread:
            self.video_thread.stop()
            self.video_thread = None

        if self.video_client:
            try:
                self.video_client.stop()
            except Exception:
                pass
            self.video_client = None

    # ==================== Status update ====================

    def _set_quick_pose_labels(self, xyz: np.ndarray, rpy: np.ndarray):
        vals = [float(xyz[0]), float(xyz[1]), float(xyz[2]), float(rpy[0]), float(rpy[1]), float(rpy[2])]
        for key, value in zip(("X", "Y", "Z", "Rx", "Ry", "Rz"), vals):
            self.quick_page.pose_labels[key].setText(f"{value:.3f}")

    def _detect_pybullet_ee_link(self, client_id: int, robot_id: int):
        if not PYBULLET_AVAILABLE:
            return None
        try:
            num_joints = _pb.getNumJoints(robot_id, physicsClientId=client_id)
            if num_joints <= 0:
                return None

            wrist_roll_idx = None
            parents = set()
            for j in range(num_joints):
                info = _pb.getJointInfo(robot_id, j, physicsClientId=client_id)
                joint_name = info[1].decode("utf-8")
                if joint_name == "wrist_roll":
                    wrist_roll_idx = j
                parent_idx = int(info[16])
                if parent_idx >= 0:
                    parents.add(parent_idx)

            if wrist_roll_idx is not None:
                return wrist_roll_idx

            leaves = sorted(set(range(num_joints)) - parents)
            if leaves:
                return leaves[-1]
            return num_joints - 1
        except Exception:
            return None

    def _get_sim_pose_xyzrpy(self) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        if not self._sim_ready:
            return None

        try:
            if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
                pose = self.sim_vtk_view.get_end_effector_pose_base()
                if pose is not None:
                    return np.asarray(pose[0], dtype=float), np.asarray(pose[1], dtype=float)
                return None

            if self._sim_backend == "pybullet":
                if self._sim_pb_client is None or self._sim_pb_robot_id is None:
                    return None
                if self._sim_pb_ee_link_index is None:
                    self._sim_pb_ee_link_index = self._detect_pybullet_ee_link(
                        self._sim_pb_client, self._sim_pb_robot_id
                    )
                if self._sim_pb_ee_link_index is None:
                    return None
                state = _pb.getLinkState(
                    self._sim_pb_robot_id,
                    int(self._sim_pb_ee_link_index),
                    computeForwardKinematics=True,
                    physicsClientId=self._sim_pb_client,
                )
                xyz = np.asarray(state[4], dtype=float)
                rpy = np.asarray(_pb.getEulerFromQuaternion(state[5]), dtype=float)
                return xyz, rpy

            if self._sim_backend == "kinematics":
                if self.sim_robot is None or self._sim_fk is None or self._sim_matrix_to_rpy is None:
                    return None
                T = self._sim_fk(self.sim_robot, np.asarray(self.sim_q, dtype=float))
                xyz = np.asarray(T[:3, 3], dtype=float)
                rpy = np.asarray(self._sim_matrix_to_rpy(T[:3, :3]), dtype=float)
                return xyz, rpy
        except Exception:
            return None

        return None

    def _update_quick_pose_from_sim(self):
        pose = self._get_sim_pose_xyzrpy()
        if pose is None:
            return
        xyz, rpy = pose
        self._set_quick_pose_labels(xyz, rpy)

    def update_status(self):
        self.last_rtt_text = "--"
        if self._sim_ready:
            self._update_quick_pose_from_sim()

        # The real-arm session is now explicitly controlled by Connect/Disconnect.
        # Refresh enable/disable state from the latest session state on every tick.
        self._update_connection_widgets()
        self._update_global_status_bar()

    # ==================== Simulation / 3D ====================

    def _select_sim_urdf_path(self) -> Optional[Path]:
        repo_root = Path(__file__).resolve().parents[3]
        candidates = [
            repo_root / "sdk" / "src" / "soarmmoce_sdk" / "resources" / "urdf" / "soarmoce_urdf.urdf",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _can_use_vtk_view(self) -> bool:
        if not VTK_VIEW_AVAILABLE:
            return False
        qpa = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
        if qpa in {"offscreen", "minimal", "minimalegl"}:
            return False
        if sys.platform.startswith("linux"):
            has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            if not has_display:
                return False
        return True

    def _init_sim_pybullet(self, urdf_path: Path):
        if not PYBULLET_AVAILABLE:
            raise RuntimeError(f"PyBullet unavailable: {_PYBULLET_IMPORT_ERROR}")

        self._cleanup_sim_backend()
        client_id = _pb.connect(_pb.DIRECT)
        try:
            _pb.resetSimulation(physicsClientId=client_id)
            robot_id = _pb.loadURDF(
                str(urdf_path),
                basePosition=[0, 0, 0],
                baseOrientation=[0, 0, 0, 1],
                useFixedBase=True,
                physicsClientId=client_id,
            )

            joint_indices = []
            joint_names = []
            limits = []
            q_init = []

            for ji in range(_pb.getNumJoints(robot_id, physicsClientId=client_id)):
                info = _pb.getJointInfo(robot_id, ji, physicsClientId=client_id)
                jtype = info[2]
                if jtype not in (_pb.JOINT_REVOLUTE, _pb.JOINT_PRISMATIC):
                    continue
                name = info[1].decode("utf-8")
                lo, hi = float(info[8]), float(info[9])
                if (not math.isfinite(lo)) or (not math.isfinite(hi)) or lo >= hi:
                    lo, hi = -math.pi, math.pi
                q0 = float(np.clip(0.0, lo, hi))
                _pb.resetJointState(robot_id, ji, q0, physicsClientId=client_id)
                joint_indices.append(ji)
                joint_names.append(name)
                limits.append((lo, hi))
                q_init.append(q0)

            if not joint_indices:
                raise RuntimeError("No movable joint found in URDF")

            self._sim_pb_client = client_id
            self._sim_pb_robot_id = robot_id
            self._sim_pb_joint_indices = joint_indices
            self._sim_pb_ee_link_index = self._detect_pybullet_ee_link(client_id, robot_id)
            self._sim_pb_camera = {
                "target": [0.0, 0.0, 0.30],
                "distance": 1.4,
                "yaw": 45.0,
                "pitch": -30.0,
                "fov": 50.0,
                "near": 0.01,
                "far": 5.0,
                "light_direction": [1.0, -0.6, 1.3],
                "light_color": [1.0, 0.98, 0.95],
                "light_distance": 2.2,
                "light_ambient": 0.55,
                "light_diffuse": 0.45,
                "light_specular": 0.25,
                "shadow": 1,
                "supersample": SIM_RENDER_SUPERSAMPLE,
            }
            self._sim_pb_renderer = _pb.ER_BULLET_HARDWARE_OPENGL
            self._sim_pb_renderer_fallback_done = False
            self._sim_backend = "pybullet"
            self.sim_joint_names = joint_names
            self._sim_joint_offsets = self._resolve_sim_joint_offsets(self.sim_joint_names)
            self._sim_model_limits = list(limits)
            self._sim_limits = self._sim_limits_from_model_limits(self._sim_model_limits)
            self.sim_q = np.zeros(len(self.sim_joint_names), dtype=float)
            home_q = self._resolve_sim_home_q()
            if home_q is not None:
                self.sim_q = np.asarray(home_q, dtype=float)
            model_q = self._sim_user_to_model_q(self.sim_q)
            for idx, joint_idx in enumerate(joint_indices[: model_q.shape[0]]):
                _pb.resetJointState(
                    robot_id,
                    joint_idx,
                    float(model_q[idx]),
                    physicsClientId=client_id,
                )
            self.sim_info_label.setText(self._tr("sim_urdf_loaded"))
        except Exception:
            try:
                _pb.disconnect(client_id)
            except Exception:
                pass
            raise

    def _build_sim_widget(self) -> QWidget:
        sim_tab = QWidget()
        sim_layout = QVBoxLayout(sim_tab)
        sim_layout.setContentsMargins(8, 8, 8, 8)

        self.sim_vtk_view = None
        self.sim_canvas = None
        self.sim_ax = None
        self.sim_view_label = QLabel(self._tr("sim_loading"))
        self.sim_view_label.setObjectName("cameraLabel")
        self.sim_view_label.setAlignment(Qt.AlignCenter)
        self.sim_view_label.setMinimumSize(320, 240)
        self.sim_view_label.installEventFilter(self)
        sim_layout.addWidget(self.sim_view_label, stretch=3)

        self.sim_info_label = QLabel(self._tr("sim_not_initialized"))
        self.sim_info_label.setWordWrap(True)
        sim_layout.addWidget(self.sim_info_label)

        self.sim_slider_group = QFrame()
        slider_layout = QGridLayout(self.sim_slider_group)
        sim_layout.addWidget(self.sim_slider_group, stretch=2)

        self.sim_sliders = []
        self.sim_spins = []
        self._sim_limits = []

        urdf_path = self._select_sim_urdf_path()
        if urdf_path is None:
            self.sim_info_label.setText(self._tr("sim_urdf_not_found"))
            return sim_tab

        vtk_err = None
        if self._can_use_vtk_view():
            try:
                self.sim_vtk_view = VtkRobotView(urdf_path)
                sim_layout.insertWidget(0, self.sim_vtk_view, stretch=3)
                self.sim_view_label.hide()
                self.sim_slider_group.hide()

                self._sim_backend = "vtk"
                self.sim_joint_names = list(self.sim_vtk_view.joint_names)
                self._sim_joint_offsets = self._resolve_sim_joint_offsets(self.sim_joint_names)
                self._sim_model_limits = list(self.sim_vtk_view.joint_limits)
                self._sim_limits = self._sim_limits_from_model_limits(self._sim_model_limits)
                self.sim_q = np.zeros(len(self.sim_joint_names), dtype=float)
                home_q = self._resolve_sim_home_q()
                if home_q is not None:
                    self.sim_q = np.asarray(home_q, dtype=float)
                self._sim_ready = True

                self.sim_info_label.clear()
                self.sim_info_label.hide()
                self._sync_quick_joint_panel()
                self.settings_page.urdf_path_label.setText(str(urdf_path))
                self.settings_page.urdf_refresh_btn.clicked.connect(
                    lambda: self.settings_page.urdf_path_label.setText(str(self._select_sim_urdf_path()))
                )
                self.settings_page.reset_view_btn.clicked.connect(self._on_sim_reset_view)
                self._apply_vtk_visual_settings()
                self._apply_vtk_camera_preset()
                if not self.connected:
                    self._update_quick_pose_from_sim()
                return sim_tab
            except Exception as exc:
                vtk_err = exc
        elif VTK_VIEW_AVAILABLE:
            vtk_err = RuntimeError("VTK disabled in headless/offscreen environment")

        pybullet_err = None
        if PYBULLET_AVAILABLE:
            try:
                self._init_sim_pybullet(urdf_path)
            except Exception as exc:
                pybullet_err = exc
                self._sim_backend = "none"
        else:
            pybullet_err = _PYBULLET_IMPORT_ERROR

        if self._sim_backend == "pybullet" and vtk_err is not None:
            self.sim_info_label.setText(f"{self._tr('sim_urdf_loaded')} (PyBullet)\nVTK unavailable: {vtk_err}")

        if self._sim_backend != "pybullet":
            try:
                from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
                from matplotlib.figure import Figure
                from ik_solver import RobotModel, fk, matrix_to_rpy, solve_ik, transform_from_xyz_rpy, transform_rot, transform_trans
                self.sim_view_label.hide()
                self.sim_canvas = FigureCanvas(Figure(figsize=(4, 3), tight_layout=True))
                self.sim_ax = self.sim_canvas.figure.add_subplot(111, projection="3d")
                self.sim_ax.view_init(elev=25, azim=45)
                sim_layout.insertWidget(0, self.sim_canvas, stretch=3)
            except Exception as exc:
                message = self._tr("sim_view_unavailable", error=exc)
                if pybullet_err is not None:
                    message += "\n" + self._tr("sim_pybullet_init_failed", error=pybullet_err)
                self.sim_info_label.setText(message)
                return sim_tab

            self._sim_tf_origin = transform_from_xyz_rpy
            self._sim_tf_rot = transform_rot
            self._sim_tf_trans = transform_trans
            self._sim_fk = fk
            self._sim_matrix_to_rpy = matrix_to_rpy
            self._sim_solve_ik = solve_ik
            self.sim_robot = RobotModel(urdf_path)
            self._sim_backend = "kinematics"
            self.sim_joint_names = []
            self._sim_limits = []
            for joint in self.sim_robot.active_joints:
                lo, hi = float(joint.limit_lower), float(joint.limit_upper)
                if (not math.isfinite(lo)) or (not math.isfinite(hi)) or lo >= hi:
                    lo, hi = -math.pi, math.pi
                self.sim_joint_names.append(joint.name)
                self._sim_limits.append((lo, hi))
            self._sim_joint_offsets = self._resolve_sim_joint_offsets(self.sim_joint_names)
            self._sim_model_limits = list(self._sim_limits)
            self.sim_robot.urdf_joint_limits = list(self._sim_model_limits)
            self.sim_robot.joint_offsets = np.asarray(self._sim_joint_offsets, dtype=float).copy()
            self._sim_limits = self._sim_limits_from_model_limits(self._sim_model_limits)
            self.sim_robot.joint_limits = list(self._sim_limits)
            self.sim_q = np.zeros(len(self.sim_joint_names), dtype=float)
            home_q = self._resolve_sim_home_q()
            if home_q is not None:
                self.sim_q = np.asarray(home_q, dtype=float)
            message = self._tr("sim_fallback_enabled")
            if pybullet_err is not None:
                message += "\n" + self._tr("sim_pybullet_init_failed", error=pybullet_err)
            if vtk_err is not None:
                message += f"\nVTK unavailable: {vtk_err}"
            self.sim_info_label.setText(message)

        for idx, name in enumerate(self.sim_joint_names):
            lo, hi = self._sim_limits[idx]
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 1000)
            slider.setValue(self._sim_value_to_slider(idx, float(self.sim_q[idx])))

            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(4)
            spin.setSingleStep(0.01)
            spin.setValue(float(self.sim_q[idx]))

            slider.valueChanged.connect(partial(self._on_sim_slider, idx))
            spin.valueChanged.connect(partial(self._on_sim_spin, idx))

            slider_layout.addWidget(QLabel(name), idx, 0)
            slider_layout.addWidget(slider, idx, 1)
            slider_layout.addWidget(spin, idx, 2)
            self.sim_sliders.append(slider)
            self.sim_spins.append(spin)

        btn_row = len(self.sim_sliders)
        self.sim_reset_btn = QPushButton(self._tr("sim_reset_zero"))
        self.sim_reset_btn.clicked.connect(self._on_sim_reset)
        slider_layout.addWidget(self.sim_reset_btn, btn_row, 0, 1, 3)

        self._sim_ready = True
        self._update_sim_plot()
        self.settings_page.urdf_path_label.setText(str(urdf_path))
        self.settings_page.urdf_refresh_btn.clicked.connect(lambda: self.settings_page.urdf_path_label.setText(str(self._select_sim_urdf_path())))
        self.settings_page.reset_view_btn.clicked.connect(self._on_sim_reset_view)
        return sim_tab

    def _on_sim_reset_view(self):
        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            self._apply_vtk_camera_preset()
        elif self._sim_backend == "pybullet" and self._sim_ready:
            self._sim_pb_camera.update({"distance": 1.4, "yaw": 45.0, "pitch": -30.0})
            self._update_sim_plot()
        elif self._sim_backend == "kinematics" and self.sim_ax is not None:
            self.sim_ax.view_init(elev=25, azim=45)
            self.sim_canvas.draw_idle()

    def _sim_value_to_slider(self, idx: int, value: float) -> int:
        lo, hi = self._sim_limits[idx]
        if hi <= lo:
            return 0
        return int(round((value - lo) / (hi - lo) * 1000))

    def _sim_slider_to_value(self, idx: int, slider_val: int) -> float:
        lo, hi = self._sim_limits[idx]
        if hi <= lo:
            return 0.0
        return lo + (hi - lo) * (slider_val / 1000.0)

    def _on_sim_slider(self, idx: int, slider_val: int):
        if not self._sim_ready or self._sim_updating:
            return
        self._sim_updating = True
        value = self._sim_slider_to_value(idx, slider_val)
        self.sim_q[idx] = value
        self.sim_spins[idx].setValue(value)
        self._update_sim_plot()
        self._sim_updating = False
        self._sync_quick_joint_panel()

    def _on_sim_spin(self, idx: int, value: float):
        if not self._sim_ready or self._sim_updating:
            return
        self._sim_updating = True
        self.sim_q[idx] = float(value)
        self.sim_sliders[idx].setValue(self._sim_value_to_slider(idx, float(value)))
        self._update_sim_plot()
        self._sim_updating = False
        self._sync_quick_joint_panel()

    def _on_sim_reset(self):
        if not self._sim_ready:
            return
        home_q = self._resolve_sim_home_q()
        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            if home_q is not None:
                self.sim_q = np.asarray(home_q, dtype=float)
            else:
                self.sim_q = np.zeros(len(self.sim_q), dtype=float)
            self._update_sim_plot()
            self._sync_quick_joint_panel()
            if not self.connected:
                self._update_quick_pose_from_sim()
            return
        self._sim_updating = True
        if home_q is not None:
            self.sim_q = np.asarray(home_q, dtype=float)
        else:
            self.sim_q = np.zeros(len(self.sim_q), dtype=float)
        for i, (lo, hi) in enumerate(self._sim_limits):
            self.sim_q[i] = float(np.clip(self.sim_q[i], lo, hi))
            self.sim_sliders[i].setValue(self._sim_value_to_slider(i, self.sim_q[i]))
            self.sim_spins[i].setValue(self.sim_q[i])
        self._update_sim_plot()
        self._sim_updating = False
        self._sync_quick_joint_panel()

    def _sim_chain_positions(self, q: np.ndarray) -> np.ndarray:
        T = np.eye(4, dtype=float)
        positions = [T[:3, 3].copy()]
        qi = 0
        for joint in self.sim_robot.chain_joints:
            T = T @ self._sim_tf_origin(joint.origin_xyz, joint.origin_rpy)
            if joint.jtype in ("revolute", "continuous"):
                q_model = float(q[qi]) + float(self._sim_joint_offsets[qi]) if qi < len(self._sim_joint_offsets) else float(q[qi])
                T = T @ self._sim_tf_rot(joint.axis, q_model)
                qi += 1
            elif joint.jtype == "prismatic":
                q_model = float(q[qi]) + float(self._sim_joint_offsets[qi]) if qi < len(self._sim_joint_offsets) else float(q[qi])
                T = T @ self._sim_tf_trans(joint.axis * q_model)
                qi += 1
            positions.append(T[:3, 3].copy())
        return np.array(positions, dtype=float)

    def _sim_set_axes_equal(self, pts: np.ndarray):
        if pts.size == 0:
            return
        mins = pts.min(axis=0)
        maxs = pts.max(axis=0)
        center = (mins + maxs) * 0.5
        radius = float(np.max(maxs - mins)) * 0.5
        if radius < 1e-3:
            radius = 0.1
        self.sim_ax.set_xlim(center[0] - radius, center[0] + radius)
        self.sim_ax.set_ylim(center[1] - radius, center[1] + radius)
        self.sim_ax.set_zlim(center[2] - radius, center[2] + radius)

    def _update_sim_pybullet(self):
        if self._sim_pb_client is None or self._sim_pb_robot_id is None:
            return

        model_q = self._sim_user_to_model_q(np.asarray(self.sim_q, dtype=float))
        for i, joint_idx in enumerate(self._sim_pb_joint_indices):
            _pb.resetJointState(
                self._sim_pb_robot_id,
                joint_idx,
                float(model_q[i]),
                physicsClientId=self._sim_pb_client,
            )

        _pb.stepSimulation(physicsClientId=self._sim_pb_client)

        display_w = max(320, int(self.sim_view_label.width()))
        display_h = max(240, int(self.sim_view_label.height()))
        cam = self._sim_pb_camera
        scale = max(1.0, float(cam.get("supersample", 1.0)))
        render_w = max(320, int(display_w * scale))
        render_h = max(240, int(display_h * scale))

        view = _pb.computeViewMatrixFromYawPitchRoll(
            cameraTargetPosition=cam["target"],
            distance=cam["distance"],
            yaw=cam["yaw"],
            pitch=cam["pitch"],
            roll=0.0,
            upAxisIndex=2,
            physicsClientId=self._sim_pb_client,
        )
        proj = _pb.computeProjectionMatrixFOV(
            fov=cam["fov"],
            aspect=float(render_w) / float(render_h),
            nearVal=cam["near"],
            farVal=cam["far"],
        )
        renderer = self._sim_pb_renderer if self._sim_pb_renderer is not None else _pb.ER_BULLET_HARDWARE_OPENGL
        _, _, rgba, _, _ = _pb.getCameraImage(
            width=render_w,
            height=render_h,
            viewMatrix=view,
            projectionMatrix=proj,
            lightDirection=cam["light_direction"],
            lightColor=cam["light_color"],
            lightDistance=cam["light_distance"],
            lightAmbientCoeff=cam["light_ambient"],
            lightDiffuseCoeff=cam["light_diffuse"],
            lightSpecularCoeff=cam["light_specular"],
            shadow=cam["shadow"],
            renderer=renderer,
            physicsClientId=self._sim_pb_client,
        )
        arr = np.asarray(rgba, dtype=np.uint8).reshape(render_h, render_w, 4)
        if (
            renderer == _pb.ER_BULLET_HARDWARE_OPENGL
            and not self._sim_pb_renderer_fallback_done
            and int(arr[:, :, :3].max()) <= 3
        ):
            try:
                _, _, rgba2, _, _ = _pb.getCameraImage(
                    width=render_w,
                    height=render_h,
                    viewMatrix=view,
                    projectionMatrix=proj,
                    lightDirection=cam["light_direction"],
                    lightColor=cam["light_color"],
                    lightDistance=cam["light_distance"],
                    lightAmbientCoeff=cam["light_ambient"],
                    lightDiffuseCoeff=cam["light_diffuse"],
                    lightSpecularCoeff=cam["light_specular"],
                    shadow=cam["shadow"],
                    renderer=_pb.ER_TINY_RENDERER,
                    physicsClientId=self._sim_pb_client,
                )
                arr2 = np.asarray(rgba2, dtype=np.uint8).reshape(render_h, render_w, 4)
                if int(arr2[:, :, :3].max()) > 3:
                    arr = arr2
                    self._sim_pb_renderer = _pb.ER_TINY_RENDERER
                    self._sim_pb_renderer_fallback_done = True
                    self.log("PyBullet OpenGL 黑屏，已自动回退到 TinyRenderer", "warning")
            except Exception:
                pass

        rgb = np.ascontiguousarray(arr[:, :, :3])
        qimg = QImage(rgb.tobytes(), render_w, render_h, 3 * render_w, QImage.Format_RGB888)
        if render_w != display_w or render_h != display_h:
            qimg = qimg.scaled(display_w, display_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.sim_view_label.setPixmap(QPixmap.fromImage(qimg))

    def _cleanup_sim_backend(self):
        self._on_quick_jog_released(enqueue_stop=False)

        if self.sim_vtk_view is not None:
            try:
                self.sim_vtk_view.shutdown()
            except Exception:
                pass
            self.sim_vtk_view = None

        if getattr(self, "_sim_pb_client", None) is not None and PYBULLET_AVAILABLE:
            try:
                _pb.disconnect(self._sim_pb_client)
            except Exception:
                pass
        self._sim_pb_client = None
        self._sim_pb_robot_id = None
        self._sim_pb_joint_indices = []
        self._sim_pb_ee_link_index = None
        self._sim_pb_renderer = None
        self._sim_pb_renderer_fallback_done = False
        self._sim_joint_offsets = np.zeros(0, dtype=float)
        self._sim_model_limits = []
        self._sim_fk = None
        self._sim_matrix_to_rpy = None
        self._sim_solve_ik = None

    def _update_sim_plot(self):
        if not self._sim_ready:
            return
        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            model_q = self._sim_user_to_model_q(np.asarray(self.sim_q, dtype=float))
            self.sim_vtk_view.set_joint_values(model_q.tolist())
            self.sim_q = self._sim_model_to_user_q(np.array(self.sim_vtk_view.joint_values, dtype=float))
            self._sync_quick_joint_panel()
            if not self.connected:
                self._update_quick_pose_from_sim()
            return
        if self._sim_backend == "pybullet":
            self._update_sim_pybullet()
            self._sync_quick_joint_panel()
            if not self.connected:
                self._update_quick_pose_from_sim()
            return
        if self._sim_backend == "kinematics" and self.sim_ax is not None:
            pts = self._sim_chain_positions(self.sim_q)
            self.sim_ax.clear()
            self.sim_ax.plot(pts[:, 0], pts[:, 1], pts[:, 2], "-o", color="#2563EB", linewidth=2)
            self.sim_ax.set_xlabel("X")
            self.sim_ax.set_ylabel("Y")
            self.sim_ax.set_zlabel("Z")
            self._sim_set_axes_equal(pts)
            self.sim_canvas.draw_idle()
            self._sync_quick_joint_panel()
            if not self.connected:
                self._update_quick_pose_from_sim()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "sim_view_label", None):
            if event.type() == QEvent.Resize and self._sim_backend == "pybullet" and self._sim_ready:
                QTimer.singleShot(0, self._update_sim_plot)
                return False
            if event.type() == QEvent.Wheel and self._sim_backend == "pybullet" and self._sim_ready:
                delta = event.angleDelta().y()
                if delta == 0:
                    delta = event.pixelDelta().y()
                if delta != 0:
                    steps = float(delta) / 120.0
                    cam = self._sim_pb_camera
                    old_distance = float(cam["distance"])
                    new_distance = float(np.clip(old_distance * (0.9 ** steps), 0.20, 5.00))
                    if abs(new_distance - old_distance) > 1e-9:
                        cam["distance"] = new_distance
                        self._update_sim_plot()
                return True
        return super().eventFilter(obj, event)

    # ==================== Close ====================

    def closeEvent(self, event):
        if self.connected:
            reply = QMessageBox.question(
                self,
                self._tr("msg_confirm"),
                self._tr("msg_confirm_exit_disconnect"),
                QMessageBox.Yes | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            self.on_disconnect()

        if self.camera_window is not None:
            self.camera_window.close()
        if self.speech_window is not None:
            self.speech_window.close()

        self._on_quick_jog_released(enqueue_stop=False)
        self._stop_sdk_command_worker()
        self._stop_sdk_sync_worker()
        self._sdk_disconnect_robot()
        self._cleanup_sim_backend()
        event.accept()



def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ArmControlGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

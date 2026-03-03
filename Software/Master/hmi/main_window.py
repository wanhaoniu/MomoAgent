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
from functools import partial
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
from PyQt5.QtCore import QEvent, QSize, QTimer, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QImage, QPixmap
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from hmi.camera_window import CameraWindow
from hmi.pages import JobPage, QuickMovePage, SettingsPage
from hmi.speech_window import SpeechInputWindow
from hmi.theme import apply_soft_effects, get_stylesheet
from hmi.widgets import GlobalStatusBar
from main import ArmClient, list_positions, list_recordings

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
    from video_client_h264 import H264VideoClient as VideoClient

    VIDEO_AVAILABLE = True
except ImportError:
    VIDEO_AVAILABLE = False

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

    PAGE_QUICK_MOVE = 0
    PAGE_JOB = 1
    PAGE_SETTINGS = 2
    PAGE_HOME = PAGE_QUICK_MOVE

    def __init__(self):
        super().__init__()

        self.client: Optional[ArmClient] = None
        self.video_client = None
        self.video_thread: Optional[VideoThread] = None
        self.camera_window: Optional[CameraWindow] = None
        self.speech_window: Optional[SpeechInputWindow] = None
        self.connected = False
        self.connecting = False
        self.recording = False

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
        self._sim_fk = None
        self._sim_matrix_to_rpy = None
        self._sim_solve_ik = None
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

        self.refresh_positions()
        self.refresh_recordings()
        self._apply_language()
        self._update_connection_widgets()
        self._update_global_status_bar()

    def _build_translations(self):
        return {
            "zh": {
                "window_title": "SoarmMoce Control",
                "nav_home": "Home",
                "nav_quick": "Quick Move",
                "nav_job": "Job",
                "nav_settings": "Settings",
                "btn_back_home": "⌂",
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
                "state_connecting": "Connecting",
                "state_connected": "Connected",
                "state_disconnected": "Disconnected",
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
                "quick_zero": "回零点",
                "quick_free": "自由移动",
                "job_recordings": "录制",
                "job_positions": "点位",
                "job_logs": "日志",
                "settings_connection": "连接",
                "settings_robot": "Robot Model / URDF",
                "settings_motion": "Motion",
                "settings_ui": "UI",
                "settings_jog_style": "Jog 风格",
                "settings_jog_minimal": "极简线条（推荐）",
                "settings_jog_soft": "柔和键帽",
                "label_server_ip": "服务器 IP:",
                "label_ctl_port": "控制端口:",
                "label_cam_port": "摄像头端口:",
                "label_leader_port": "主臂串口:",
                "label_leader_id": "主臂 ID:",
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
                "btn_home": "🏠 回零",
                "sim_loading": "3D 视图加载中...",
                "sim_not_initialized": "仿真模型未初始化",
                "sim_urdf_not_found": "URDF 读取失败：未找到可用 URDF 文件",
                "sim_urdf_loaded": "URDF 模型已加载",
                "sim_view_unavailable": "3D 视图不可用：{error}",
                "sim_fallback_enabled": "已自动切换到 FK 线框回退模式",
                "sim_pybullet_init_failed": "PyBullet 初始化失败：{error}",
                "sim_slider_group": "关节角 (rad)",
                "sim_reset_zero": "重置为 0",
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
                "msg_confirm_home": "确定要回零吗?",
                "msg_confirm_exit_home": "要在退出前回零吗?",
                "msg_test_conn_placeholder": "连接测试入口预留（可扩展为 ping/握手）",
                "log_connecting": "正在连接 {ip}:{port}...",
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
                "log_home_start": "正在回零...",
                "log_home_done": "回零完成",
                "log_home_failed": "回零失败",
                "rec_item_fmt": "{name} ({frames} 帧, {duration:.1f}s)",
            },
            "en": {
                "window_title": "SoarmMoce Control",
                "nav_home": "Home",
                "nav_quick": "Quick Move",
                "nav_job": "Job",
                "nav_settings": "Settings",
                "btn_back_home": "⌂",
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
                "state_connecting": "Connecting",
                "state_connected": "Connected",
                "state_disconnected": "Disconnected",
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
                "quick_zero": "Zero",
                "quick_free": "Free Move",
                "job_recordings": "Recordings",
                "job_positions": "Positions",
                "job_logs": "Logs",
                "settings_connection": "Connection",
                "settings_robot": "Robot Model / URDF",
                "settings_motion": "Motion",
                "settings_ui": "UI",
                "settings_jog_style": "Jog Style",
                "settings_jog_minimal": "Minimal Line (Recommended)",
                "settings_jog_soft": "Soft Keycap",
                "label_server_ip": "Server IP:",
                "label_ctl_port": "Control Port:",
                "label_cam_port": "Camera Port:",
                "label_leader_port": "Leader Serial:",
                "label_leader_id": "Leader ID:",
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
                "sim_reset_zero": "Reset to 0",
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
                "msg_confirm_exit_home": "Return to home before exit?",
                "msg_test_conn_placeholder": "Connection test placeholder (ping/handshake can be added)",
                "log_connecting": "Connecting to {ip}:{port}...",
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
        self.job_page = JobPage()
        self.settings_page = SettingsPage()

        self.stack.addWidget(self.quick_page)
        self.stack.addWidget(self.job_page)
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
        self.back_home_btn.clicked.connect(lambda: self._set_page(self.PAGE_QUICK_MOVE))
        layout.addWidget(self.back_home_btn)

        # Keep top bar minimal: quick shortcut + Job + Settings.
        self.nav_buttons = []
        self.top_job_btn = QPushButton(self._tr("nav_job"))
        self.top_job_btn.setObjectName("primaryBtn")
        self.top_job_btn.setFixedWidth(44)
        self.top_job_btn.clicked.connect(lambda: self._set_page(self.PAGE_JOB))
        layout.addWidget(self.top_job_btn)

        self.top_settings_btn = QPushButton(self._tr("nav_settings"))
        self.top_settings_btn.setObjectName("primaryBtn")
        self.top_settings_btn.setFixedWidth(44)
        self.top_settings_btn.clicked.connect(lambda: self._set_page(self.PAGE_SETTINGS))
        layout.addWidget(self.top_settings_btn)

        layout.addStretch()

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
        self._top_job_has_icon = self._set_button_icon(self.top_job_btn, "job", size=18)
        self._top_settings_has_icon = self._set_button_icon(self.top_settings_btn, "settings", size=18)
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
        text = text.replace("📷 ", "").replace("🎤 ", "").replace("🧾 ", "").replace("⌂", "").strip()
        return text if text else self._tr(key)

    def _set_camera_btn_text(self, opened: bool):
        key = "btn_camera_close" if opened else "btn_camera"
        self.camera_btn.setText(self._plain_header_text(key))

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
        self.quick_page.home_clicked.connect(self.on_home)

        self.job_page.save_pos_btn.clicked.connect(self.on_save_pos)
        self.job_page.goto_btn.clicked.connect(self.on_goto_pos)
        self.job_page.del_pos_btn.clicked.connect(self.on_del_pos)
        self.job_page.refresh_pos_btn.clicked.connect(self.refresh_positions)
        self.job_page.pos_list.itemDoubleClicked.connect(self.on_goto_pos)

        self.job_page.record_btn.clicked.connect(self.on_toggle_record)
        self.job_page.play_btn.clicked.connect(self.on_play_rec)
        self.job_page.del_rec_btn.clicked.connect(self.on_del_rec)
        self.job_page.rec_list.itemDoubleClicked.connect(self.on_play_rec)

        self.settings_page.connect_btn.clicked.connect(self.on_connect)
        self.settings_page.disconnect_btn.clicked.connect(self.on_disconnect)
        self.settings_page.test_conn_btn.clicked.connect(self.on_test_connection)
        self.settings_page.default_speed_spin.valueChanged.connect(self.on_speed_changed)
        self.settings_page.ui_lang_combo.currentIndexChanged.connect(self.on_settings_language_changed)
        self.settings_page.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        self.settings_page.jog_style_combo.currentIndexChanged.connect(self.on_jog_style_changed)
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

    def _apply_language(self):
        self.setWindowTitle(self._tr("window_title"))

        if getattr(self, "_back_home_has_icon", False):
            self.back_home_btn.setText("")
        else:
            self.back_home_btn.setText(self._tr("nav_quick"))
        self.back_home_btn.setToolTip(self._tr("nav_quick"))
        if getattr(self, "_top_job_has_icon", False):
            self.top_job_btn.setText("")
        else:
            self.top_job_btn.setText(self._tr("nav_job"))
        self.top_job_btn.setToolTip(self._tr("nav_job"))
        if getattr(self, "_top_settings_has_icon", False):
            self.top_settings_btn.setText("")
        else:
            self.top_settings_btn.setText(self._tr("nav_settings"))
        self.top_settings_btn.setToolTip(self._tr("nav_settings"))
        self._set_camera_btn_text(self._is_camera_window_visible())
        self._set_speech_btn_text(self._is_speech_window_visible())
        self.log_btn.setText(self._plain_header_text("btn_log"))
        self.lang_label.setText(self._tr("lang_label"))

        self.quick_page.set_texts(self._tr)
        self.job_page.set_texts(self._tr, self.recording)
        self.settings_page.set_texts(self._tr)

        self.mode_text = self._tr("mode_manual")
        self.control_owner_text = self._tr("owner_local")

        if self.camera_window is not None:
            self.camera_window.set_window_title(self._tr("camera_window_title"))
        if self.speech_window is not None:
            self.speech_window.set_window_title(self._tr("speech_window_title"))

        self._sync_language_combos()
        self._update_global_status_bar()

        if self.connected:
            self.statusBar().showMessage(self._tr("status_connected"))
        elif self.connecting:
            self.statusBar().showMessage(self._tr("status_connecting"))
        else:
            self.statusBar().showMessage(self._tr("status_ready"))

        self.refresh_recordings()

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

    def _on_quick_joint_step(self, idx: int, direction: float):
        if not self._sim_ready or idx >= len(self.sim_q):
            return
        step_deg = max(0.1, float(self.quick_page.step_angle_spin.value()))
        delta = math.radians(step_deg) * float(direction)
        lo, hi = self._sim_limits[idx]
        self.sim_q[idx] = float(np.clip(self.sim_q[idx] + delta, lo, hi))
        self._update_sim_plot()

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

    def _on_quick_jog_released(self):
        self._jog_hold_key = None
        if self._jog_hold_timer.isActive():
            self._jog_hold_timer.stop()

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
    def _rpy_to_rotmat(rpy: np.ndarray) -> np.ndarray:
        roll, pitch, yaw = [float(x) for x in np.asarray(rpy, dtype=float).reshape(3)]
        cr, sr = math.cos(roll), math.sin(roll)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]], dtype=float)
        ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]], dtype=float)
        rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=float)
        return rz @ ry @ rx

    @staticmethod
    def _rotvec_to_rotmat(rotvec: np.ndarray) -> np.ndarray:
        rv = np.asarray(rotvec, dtype=float).reshape(3)
        angle = float(np.linalg.norm(rv))
        if angle < 1e-12:
            return np.eye(3, dtype=float)
        axis = rv / angle
        x, y, z = float(axis[0]), float(axis[1]), float(axis[2])
        c = math.cos(angle)
        s = math.sin(angle)
        v = 1.0 - c
        return np.array(
            [
                [x * x * v + c, x * y * v - z * s, x * z * v + y * s],
                [y * x * v + z * s, y * y * v + c, y * z * v - x * s],
                [z * x * v - y * s, z * y * v + x * s, z * z * v + c],
            ],
            dtype=float,
        )

    @staticmethod
    def _rotmat_to_rpy(rotmat: np.ndarray) -> np.ndarray:
        R = np.asarray(rotmat, dtype=float).reshape(3, 3)
        sy = -float(R[2, 0])
        sy = float(np.clip(sy, -1.0, 1.0))
        cy = float(max(0.0, 1.0 - sy * sy) ** 0.5)
        if cy < 1e-9:
            yaw = math.atan2(-float(R[0, 1]), float(R[1, 1]))
            pitch = math.asin(sy)
            roll = 0.0
        else:
            yaw = math.atan2(float(R[1, 0]), float(R[0, 0]))
            pitch = math.asin(sy)
            roll = math.atan2(float(R[2, 1]), float(R[2, 2]))
        return np.array([roll, pitch, yaw], dtype=float)

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
        rest = [float(self.sim_q[i]) if i < len(self.sim_q) else 0.0 for i in range(len(joint_indices))]
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
        return q_out

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
                self._sim_limits,
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
                limits,
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
        if not self._sim_ready or len(self.sim_q) == 0:
            return

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

        pose = self._get_sim_pose_xyzrpy()
        if pose is None:
            return
        xyz_now, rpy_now = pose
        R_now = self._rpy_to_rotmat(rpy_now)

        step_mm = max(0.1, float(self.quick_page.step_dist_spin.value()))
        step_rad = math.radians(max(0.1, float(self.quick_page.step_angle_spin.value())))
        delta_pos_local = np.zeros(3, dtype=float)
        delta_rot_local = np.zeros(3, dtype=float)
        if key_norm in trans_map:
            delta_pos_local = trans_map[key_norm] * (step_mm / 1000.0)
        else:
            delta_rot_local = rot_map[key_norm] * step_rad

        coord_mode = str(self.quick_page.coord_combo.currentText()).strip().lower()
        use_tool = coord_mode.startswith("tool")
        if use_tool:
            target_xyz = np.asarray(xyz_now, dtype=float) + (R_now @ delta_pos_local)
            R_target = R_now @ self._rotvec_to_rotmat(delta_rot_local)
        else:
            target_xyz = np.asarray(xyz_now, dtype=float) + delta_pos_local
            R_target = self._rotvec_to_rotmat(delta_rot_local) @ R_now
        target_rpy = self._rotmat_to_rpy(R_target)

        q_target = self._solve_sim_ik(target_xyz, target_rpy)
        if q_target is None:
            return
        if q_target.shape[0] != len(self.sim_q):
            return

        q_clipped = np.asarray(q_target, dtype=float).copy()
        for i, (lo, hi) in enumerate(self._sim_limits):
            q_clipped[i] = float(np.clip(q_clipped[i], float(lo), float(hi)))
        self.sim_q = q_clipped
        self._update_sim_plot()

    def _on_quick_origin(self):
        if not self._sim_ready:
            return
        self._on_quick_jog_released()
        self._on_sim_reset()
        self.statusBar().showMessage("Simulation reset to zero")

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

    def _update_connection_widgets(self):
        self.settings_page.set_connected(self.connected)
        self.job_page.set_connected(self.connected)
        self.quick_page.set_motion_enabled(self.connected or self._sim_ready)

    def _state_connection_text(self) -> str:
        if self.connecting:
            return self._tr("state_connecting")
        return self._tr("state_connected") if self.connected else self._tr("state_disconnected")

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
        display_frame = frame.copy()
        if self.recording:
            cv2.circle(display_frame, (display_frame.shape[1] - 30, 30), 15, (0, 0, 255), -1)
            cv2.putText(display_frame, "REC", (display_frame.shape[1] - 70, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

        self.last_frame = frame.copy()
        self.last_frame_fps = float(fps)
        self.last_frame_latency = float(latency)

        if self._is_camera_window_visible():
            self.camera_window.set_frame(
                display_frame,
                f"FPS: {int(max(0.0, fps))}",
                self._tr("latency_value", value=float(latency)),
                f"RTT: {self.last_rtt_text}",
                self.recording,
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
            if hasattr(self.speech_window, "agent_action_started"):
                self.speech_window.agent_action_started.connect(self._on_speech_agent_action_started)
            if hasattr(self.speech_window, "agent_session_changed"):
                self.speech_window.agent_session_changed.connect(self._on_speech_agent_session_changed)
            if hasattr(self.speech_window, "tool_request"):
                self.speech_window.tool_request.connect(self._on_speech_tool_request)
        return self.speech_window

    def _on_speech_window_closed(self):
        self._set_speech_btn_text(False)

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

    def _on_speech_agent_action_started(self, action_name: str):
        action = str(action_name or "").strip() or "unknown"
        self.log(f"[OpenClaw] 正在执行动作: {action}", "info")
        self.statusBar().showMessage(f"正在执行动作: {action}...")

    def _on_speech_agent_session_changed(self, session_id: str):
        sid = str(session_id or "").strip()
        if sid:
            self.log(f"[OpenClaw] Session: {sid}", "info")

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

    def _tool_move_robot_arm_main_thread(self, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        if not self._sim_ready:
            return False, {"ok": False, "error": "simulation is not ready"}

        pose = self._get_sim_pose_xyzrpy()
        if pose is None:
            return False, {"ok": False, "error": "failed to read current pose"}
        xyz_now, rpy_now = pose

        x = self._tool_to_float(payload.get("x", 0.0), float(xyz_now[0]))
        y = self._tool_to_float(payload.get("y", 0.0), float(xyz_now[1]))
        z = self._tool_to_float(payload.get("z", 0.0), float(xyz_now[2]))
        frame = str(payload.get("frame", "base") or "base").strip().lower()
        duration = max(0.2, min(20.0, self._tool_to_float(payload.get("duration", 2.0), 2.0)))
        wait = bool(payload.get("wait", True))

        if frame not in ("base", "tool"):
            return False, {"ok": False, "error": "frame must be 'base' or 'tool'"}

        target_xyz = np.array([x, y, z], dtype=float)
        if frame == "tool":
            # Tool frame mode treats input xyz as local offset in meters.
            R_now = self._rpy_to_rotmat(np.asarray(rpy_now, dtype=float))
            target_xyz = np.asarray(xyz_now, dtype=float) + (R_now @ target_xyz)

        target_rpy = np.asarray(rpy_now, dtype=float)
        q_target = self._solve_sim_ik(np.asarray(target_xyz, dtype=float), target_rpy)
        if q_target is None:
            return False, {"ok": False, "error": "IK failed for target", "target_xyz_m": target_xyz.tolist()}
        if q_target.shape[0] != len(self.sim_q):
            return False, {"ok": False, "error": "joint vector size mismatch"}

        q_clipped = np.asarray(q_target, dtype=float).copy()
        for i, (lo, hi) in enumerate(self._sim_limits):
            q_clipped[i] = float(np.clip(q_clipped[i], float(lo), float(hi)))
        self.sim_q = q_clipped
        self._update_sim_plot()

        pose_after = self._get_sim_pose_xyzrpy()
        if pose_after is None:
            return False, {"ok": False, "error": "failed to read pose after move"}
        xyz_after, rpy_after = pose_after
        err = float(np.linalg.norm(np.asarray(xyz_after, dtype=float) - np.asarray(target_xyz, dtype=float)))

        result = {
            "ok": err <= 0.10,
            "message": "moved" if err <= 0.10 else "move completed with large error",
            "backend": f"main-thread-{self._sim_backend}",
            "frame": frame,
            "duration": duration,
            "wait": wait,
            "target_xyz_m": [float(v) for v in target_xyz.tolist()],
            "actual_xyz_m": [float(v) for v in np.asarray(xyz_after, dtype=float).tolist()],
            "actual_rpy_rad": [float(v) for v in np.asarray(rpy_after, dtype=float).tolist()],
            "position_error_m": err,
            "joint_values_rad": [float(v) for v in np.asarray(self.sim_q, dtype=float).tolist()],
        }
        return bool(result["ok"]), result

    def _tool_get_robot_state_main_thread(self) -> Tuple[bool, Dict[str, object]]:
        pose = self._get_sim_pose_xyzrpy()
        if pose is None:
            return False, {"ok": False, "error": "failed to read robot pose"}
        xyz, rpy = pose

        joints: Dict[str, float] = {}
        if len(self.sim_q) > 0:
            for i, q in enumerate(np.asarray(self.sim_q, dtype=float).tolist()):
                if i < len(self.sim_joint_names):
                    name = str(self.sim_joint_names[i])
                else:
                    name = f"joint_{i + 1}"
                joints[name] = float(q)

        result = {
            "ok": True,
            "backend": f"main-thread-{self._sim_backend}",
            "simulation_ready": bool(self._sim_ready),
            "joints_rad": joints,
            "ee_xyz_m": [float(v) for v in np.asarray(xyz, dtype=float).tolist()],
            "ee_rpy_rad": [float(v) for v in np.asarray(rpy, dtype=float).tolist()],
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
            self._on_quick_jog_released()
        except Exception:
            pass
        return True, {"ok": True, "message": "stopped", "backend": f"main-thread-{self._sim_backend}"}

    def _on_speech_tool_request(self, tool_name: str, payload: Dict[str, object], request_id: str):
        speech = self.speech_window
        req_id = str(request_id or "").strip()
        if speech is None or not req_id:
            return

        name = str(tool_name or "").strip()
        args = payload if isinstance(payload, dict) else {}
        ok = False
        result: Dict[str, object]

        try:
            if name == "move_robot_arm":
                ok, result = self._tool_move_robot_arm_main_thread(args)
            elif name == "get_robot_state":
                ok, result = self._tool_get_robot_state_main_thread()
            elif name == "get_camera_frame":
                ok, result = self._tool_get_camera_frame_main_thread(args)
            elif name == "stop_robot":
                ok, result = self._tool_stop_robot_main_thread()
            else:
                ok = False
                result = {"ok": False, "error": f"unsupported tool: {name}"}
        except Exception as exc:
            ok = False
            result = {"ok": False, "error": str(exc)}

        if hasattr(speech, "submit_tool_result"):
            speech.submit_tool_result(req_id, bool(ok), result)

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
        self.job_page.log_text.append(line)
        self.log_dock_view.append(line)
        self.job_page.log_text.verticalScrollBar().setValue(self.job_page.log_text.verticalScrollBar().maximum())
        self.log_dock_view.verticalScrollBar().setValue(self.log_dock_view.verticalScrollBar().maximum())

    # ==================== Connection ====================

    def on_test_connection(self):
        QMessageBox.information(self, self._tr("msg_tip"), self._tr("msg_test_conn_placeholder"))

    def on_connect(self):
        if self.connected or self.connecting:
            return

        ip = self.settings_page.ip_input.text().strip()
        port = self.settings_page.port_input.value()
        cam_port = self.settings_page.cam_port_input.value()
        leader_port = self.settings_page.leader_port_input.text().strip()
        leader_id = self.settings_page.leader_id_combo.currentText()

        self.connecting = True
        self._update_connection_widgets()
        self._update_global_status_bar()
        self.statusBar().showMessage(self._tr("status_connecting"))
        self.log(self._tr("log_connecting", ip=ip, port=port))

        def connect_task():
            try:
                self.client = ArmClient(
                    name="gui_arm",
                    server_ip=ip,
                    ctl_port=port,
                    leader_port=leader_port,
                    leader_id=leader_id,
                )
                self.client.start()

                if VIDEO_AVAILABLE and not self._using_virtual_vtk_camera():
                    self.video_client = VideoClient(server_ip=ip, video_port=cam_port)
                    threading.Thread(target=self.video_client.start, daemon=True).start()
                    self.video_thread = VideoThread(self.video_client)
                    self.video_thread.frame_ready.connect(self.update_camera)
                    self.video_thread.start()

                self.connected = True
                self.connecting = False
                self.log(self._tr("log_connect_success"), "success")
                QTimer.singleShot(0, self._on_connected)
            except Exception as exc:
                self.connected = False
                self.connecting = False
                self.log(self._tr("log_connect_failed", error=exc), "error")
                QTimer.singleShot(0, self._on_connect_failed)

        threading.Thread(target=connect_task, daemon=True).start()

    def _on_connected(self):
        self._update_connection_widgets()
        self.statusBar().showMessage(self._tr("status_connected"))
        self._update_global_status_bar()

    def _on_connect_failed(self):
        self._update_connection_widgets()
        self.statusBar().showMessage(self._tr("status_disconnected"))
        self._update_global_status_bar()

    def on_disconnect(self):
        if not self.connected and not self.connecting:
            return

        self.log(self._tr("log_disconnecting"))

        if self.video_thread:
            self.video_thread.stop()
            self.video_thread = None

        if self.video_client:
            try:
                self.video_client.stop()
            except Exception:
                pass
            self.video_client = None

        if self.client:
            try:
                self.client.stop()
            except Exception:
                pass
            self.client = None

        self.connected = False
        self.connecting = False
        self.last_frame = None
        self.last_frame_fps = 0.0
        self.last_frame_latency = 0.0
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
            else:
                pixmap = self._build_placeholder_pixmap(self.camera_window.camera_label)
                self.camera_window.set_placeholder(pixmap, self._tr("camera_disconnected"))

        self.log(self._tr("log_disconnected"), "warning")
        self.statusBar().showMessage(self._tr("status_disconnected"))
        self._update_connection_widgets()
        self._update_global_status_bar()

    def on_home(self):
        if not self.client:
            if self._sim_ready:
                self._on_sim_reset()
                self.statusBar().showMessage("Simulation reset to zero")
            return

        reply = QMessageBox.question(
            self,
            self._tr("msg_confirm"),
            self._tr("msg_confirm_home"),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.log(self._tr("log_home_start"))

        def home_task():
            success = self.client.return_to_zero(3.0)
            if success:
                self.log(self._tr("log_home_done"), "success")
            else:
                self.log(self._tr("log_home_failed"), "error")

        threading.Thread(target=home_task, daemon=True).start()

    # ==================== Camera frames ====================

    def update_camera(self, frame: np.ndarray, latency: float, fps: float):
        if self._using_virtual_vtk_camera():
            return
        self._display_camera_frame(frame, fps=float(fps), latency=float(latency))

    # ==================== Position management ====================

    def refresh_positions(self):
        self.job_page.pos_list.clear()
        for name in sorted(list_positions()):
            if not name.startswith("_"):
                self.job_page.pos_list.addItem(name)

    def on_save_pos(self):
        name = self.job_page.pos_name_input.text().strip()
        if not name:
            QMessageBox.warning(self, self._tr("msg_tip"), self._tr("msg_input_pos_name"))
            return
        if self.client:
            self.client.savepos(name)
            self.log(self._tr("log_pos_saved", name=name), "success")
            self.job_page.pos_name_input.clear()
            self.refresh_positions()

    def on_goto_pos(self):
        item = self.job_page.pos_list.currentItem()
        if not item:
            return
        name = item.text()
        duration, ok = QInputDialog.getDouble(
            self,
            self._tr("msg_jump_title"),
            self._tr("msg_jump_prompt", name=name),
            2.0,
            0.5,
            10.0,
            1,
        )
        if ok and self.client:
            self.log(self._tr("log_goto_start", name=name))

            def goto_task():
                success = self.client.goto(name, duration)
                if success:
                    self.log(self._tr("log_goto_done", name=name), "success")
                else:
                    self.log(self._tr("log_goto_failed"), "error")

            threading.Thread(target=goto_task, daemon=True).start()

    def on_del_pos(self):
        item = self.job_page.pos_list.currentItem()
        if not item:
            return
        name = item.text()
        reply = QMessageBox.question(
            self,
            self._tr("msg_confirm"),
            self._tr("msg_confirm_delete_pos", name=name),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes and self.client:
            self.client.delpos(name)
            self.log(self._tr("log_pos_deleted", name=name), "warning")
            self.refresh_positions()

    # ==================== Recording management ====================

    def refresh_recordings(self):
        self.job_page.rec_list.clear()
        recordings = list_recordings()
        for name, info in sorted(recordings.items()):
            item = QListWidgetItem(
                self._tr("rec_item_fmt", name=name, frames=info["frames"], duration=info["duration"])
            )
            item.setData(Qt.UserRole, name)
            self.job_page.rec_list.addItem(item)

    def on_toggle_record(self):
        if not self.recording:
            name = self.job_page.rec_name_input.text().strip()
            if not name:
                QMessageBox.warning(self, self._tr("msg_tip"), self._tr("msg_input_rec_name"))
                return
            if self.client:
                self.client.start_record(name)
                self.recording = True
                self.job_page.record_btn.setText(self._tr("btn_record_stop"))
                self.job_page.rec_name_input.setEnabled(False)
                self.log(self._tr("log_record_start", name=name), "success")
        else:
            if self.client:
                self.client.stop_record()
                self.recording = False
                self.job_page.record_btn.setText(self._tr("btn_record_start"))
                self.job_page.rec_name_input.setEnabled(True)
                self.job_page.rec_name_input.clear()
                self.log(self._tr("log_record_saved"), "success")
                self.refresh_recordings()

    def on_play_rec(self):
        item = self.job_page.rec_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        times = self.job_page.play_times_spin.value()
        if self.client:
            self.log(self._tr("log_play_start", name=name, times=times))

            def play_task():
                success = self.client.play(name, times)
                if success:
                    self.log(self._tr("log_play_done"), "success")
                else:
                    self.log(self._tr("log_play_failed"), "error")

            threading.Thread(target=play_task, daemon=True).start()

    def on_del_rec(self):
        item = self.job_page.rec_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        reply = QMessageBox.question(
            self,
            self._tr("msg_confirm"),
            self._tr("msg_confirm_delete_rec", name=name),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes and self.client:
            self.client.delete_recording(name)
            self.log(self._tr("log_record_deleted", name=name), "warning")
            self.refresh_recordings()

    # ==================== Status update ====================

    def _set_quick_pose_labels(self, xyz: np.ndarray, rpy: np.ndarray):
        vals = [float(xyz[0]), float(xyz[1]), float(xyz[2]), float(rpy[0]), float(rpy[1]), float(rpy[2])]
        for key, value in zip(("X", "Y", "Z", "Rx", "Ry", "Rz"), vals):
            self.quick_page.pose_labels[key].setText(f"{value:.3f}")

    def _update_quick_pose_from_client(self):
        if not self.connected or not self.client:
            return
        acc = self.client.get_accumulators()
        for i, key in enumerate(("X", "Y", "Z", "Rx", "Ry", "Rz")):
            if i < len(acc):
                value = list(acc.values())[i]
            else:
                value = 0.0
            self.quick_page.pose_labels[key].setText(f"{value:.2f}")

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
        if not self.connected or not self.client:
            self.last_rtt_text = "--"
            if self._sim_ready:
                self._update_quick_pose_from_sim()
        else:
            buf = list(self.client.rtt_ms_buf)
            if buf:
                self.last_rtt_text = f"{min(buf):.1f}/{sum(buf)/len(buf):.1f}/{max(buf):.1f} ms"
            else:
                self.last_rtt_text = "--"
            self._update_quick_pose_from_client()

        self._update_global_status_bar()

    # ==================== Simulation / 3D ====================

    def _select_sim_urdf_path(self) -> Optional[Path]:
        repo_root = Path(__file__).resolve().parents[3]
        candidates = [
            repo_root / "sdk" / "src" / "soarmMoce_sdk" / "resources" / "urdf" / "soarmoce_urdf.urdf",
            repo_root / "Urdf" / "urdf" / "soarmoce_purple.urdf",
            repo_root / "Urdf" / "urdf" / "soarmoce_urdf.urdf",
            repo_root / "Soarm101" / "SO101" / "so101_new_calib.urdf",
            Path(__file__).resolve().parents[1] / "so101.urdf",
        ]
        for path in candidates:
            if path.exists():
                # so101.urdf requires a sibling assets directory, otherwise PyBullet load fails.
                if path.name == "so101.urdf" and not (path.parent / "assets").exists():
                    continue
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
            self._sim_limits = limits
            self.sim_q = np.array(q_init, dtype=float)
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
                self._sim_limits = list(self.sim_vtk_view.joint_limits)
                self.sim_q = np.array(self.sim_vtk_view.joint_values, dtype=float)
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
            self.sim_q = np.zeros(len(self.sim_joint_names), dtype=float)
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
        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            self.sim_vtk_view.reset_zero()
            self.sim_q = np.array(self.sim_vtk_view.joint_values, dtype=float)
            self._sync_quick_joint_panel()
            if not self.connected:
                self._update_quick_pose_from_sim()
            return
        self._sim_updating = True
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
                T = T @ self._sim_tf_rot(joint.axis, float(q[qi]))
                qi += 1
            elif joint.jtype == "prismatic":
                T = T @ self._sim_tf_trans(joint.axis * float(q[qi]))
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

        for i, joint_idx in enumerate(self._sim_pb_joint_indices):
            _pb.resetJointState(
                self._sim_pb_robot_id,
                joint_idx,
                float(self.sim_q[i]),
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
        self._on_quick_jog_released()

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
        self._sim_fk = None
        self._sim_matrix_to_rpy = None
        self._sim_solve_ik = None

    def _update_sim_plot(self):
        if not self._sim_ready:
            return
        if self._sim_backend == "vtk" and self.sim_vtk_view is not None:
            self.sim_vtk_view.set_joint_values(self.sim_q.tolist())
            self.sim_q = np.array(self.sim_vtk_view.joint_values, dtype=float)
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
                self._tr("msg_confirm_exit_home"),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Yes and self.client:
                try:
                    self.client.return_to_zero(3.0)
                except Exception:
                    pass
            self.on_disconnect()

        if self.camera_window is not None:
            self.camera_window.close()
        if self.speech_window is not None:
            self.speech_window.close()

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

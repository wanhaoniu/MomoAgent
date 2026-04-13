"""Refreshed settings page for the current local-SDK GUI."""

from __future__ import annotations

import os

from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QLineEdit,
)


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        self._connected = False
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.hardware_tab = QWidget()
        self.robot_tab = QWidget()
        self.motion_tab = QWidget()
        self.ui_tab = QWidget()

        self.tabs.addTab(self.hardware_tab, "Hardware")
        self.tabs.addTab(self.robot_tab, "Robot Model / URDF")
        self.tabs.addTab(self.motion_tab, "Motion")
        self.tabs.addTab(self.ui_tab, "UI")

        self._build_hardware_tab()
        self._build_robot_tab()
        self._build_motion_tab()
        self._build_ui_tab()

    def _build_hardware_tab(self):
        layout = QVBoxLayout(self.hardware_tab)

        camera_source_default = str(os.getenv("SOARMMOCE_CAMERA_SOURCE", "virtual")).strip().lower()
        camera_device_default = str(
            os.getenv("SOARMMOCE_CAMERA_DEVICE", os.getenv("SOARMMOCE_CAMERA_NAME_HINT", ""))
        ).strip()
        camera_rotation_default = str(os.getenv("SOARMMOCE_CAMERA_ROTATION", "0")).strip()

        self.hardware_group = QGroupBox("Hardware")
        runtime_form = QFormLayout(self.hardware_group)
        self.runtime_status_value = QLabel("--")
        self.runtime_status_value.setWordWrap(True)
        self.sdk_config_path_value = QLabel("--")
        self.sdk_config_path_value.setWordWrap(True)
        self.serial_port_value = QLabel("--")
        self.serial_port_value.setWordWrap(True)

        self.camera_source_combo = QComboBox()
        self.camera_source_combo.addItem("Virtual Preview", "virtual")
        self.camera_source_combo.addItem("Local V4L2", "v4l2")
        self.camera_device_input = QLineEdit(camera_device_default)
        self.camera_device_input.setPlaceholderText("/dev/video0 or LRCP G-720P")
        self.camera_rotation_combo = QComboBox()
        self.camera_rotation_combo.addItem("0°", 0)
        self.camera_rotation_combo.addItem("90°", 90)
        self.camera_rotation_combo.addItem("180°", 180)
        self.camera_rotation_combo.addItem("270°", 270)

        runtime_form.addRow("Runtime", self.runtime_status_value)
        runtime_form.addRow("SDK Config", self.sdk_config_path_value)
        runtime_form.addRow("Serial Port", self.serial_port_value)
        runtime_form.addRow("Camera Source", self.camera_source_combo)
        runtime_form.addRow("Local Camera", self.camera_device_input)
        runtime_form.addRow("Camera Rotation", self.camera_rotation_combo)

        idx = self.camera_source_combo.findData(
            camera_source_default if camera_source_default in ("virtual", "v4l2") else "virtual"
        )
        self.camera_source_combo.setCurrentIndex(max(0, idx))
        idx = self.camera_rotation_combo.findData(
            int(camera_rotation_default) if camera_rotation_default.lstrip("-").isdigit() else 0
        )
        if idx >= 0:
            self.camera_rotation_combo.setCurrentIndex(idx)
        self.camera_source_combo.currentIndexChanged.connect(self._on_camera_source_changed)
        self._on_camera_source_changed()

        layout.addWidget(self.hardware_group)
        layout.addStretch()

    def _on_camera_source_changed(self):
        is_v4l2 = (self.camera_source_combo.currentData() or "virtual") == "v4l2"
        self.camera_device_input.setEnabled(is_v4l2)
        self.camera_rotation_combo.setEnabled(is_v4l2)

    def _build_robot_tab(self):
        layout = QVBoxLayout(self.robot_tab)

        self.robot_group = QGroupBox("URDF")
        form = QFormLayout(self.robot_group)
        self.urdf_path_label = QLabel("--")
        self.urdf_path_label.setWordWrap(True)
        self.urdf_refresh_btn = QPushButton("Auto Detect")
        self.aa_mode_combo = QComboBox()
        self.aa_mode_combo.addItem("FXAA (Recommended)", ("fxaa", 0))
        self.aa_mode_combo.addItem("MSAA x4", ("msaa", 4))
        self.aa_mode_combo.addItem("MSAA x8 (May black-screen)", ("msaa", 8))
        self.aa_mode_combo.addItem("Off", ("off", 0))
        self.material_preset_combo = QComboBox()
        self.material_preset_combo.addItem("Soft (Recommended)", "soft")
        self.material_preset_combo.addItem("Default", "default")
        self.background_theme_combo = QComboBox()
        self.background_theme_combo.addItem("Studio (Recommended)", "studio")
        self.background_theme_combo.addItem("Dark", "dark")
        self.background_theme_combo.addItem("White (Legacy)", "white")
        self.camera_preset_combo = QComboBox()
        self.camera_preset_combo.addItem("Iso (Recommended)", "iso")
        self.camera_preset_combo.addItem("Front", "front")
        self.camera_preset_combo.addItem("Top", "top")
        self.apply_view_btn = QPushButton("Apply View Style")
        self.reset_view_btn = QPushButton("Reset View")

        form.addRow("URDF Path", self.urdf_path_label)
        form.addRow("", self.urdf_refresh_btn)
        form.addRow("Anti Aliasing", self.aa_mode_combo)
        form.addRow("Material", self.material_preset_combo)
        form.addRow("Background", self.background_theme_combo)
        form.addRow("Camera Preset", self.camera_preset_combo)
        button_row = QHBoxLayout()
        button_row.addWidget(self.apply_view_btn)
        button_row.addWidget(self.reset_view_btn)
        form.addRow("", button_row)

        layout.addWidget(self.robot_group)
        layout.addStretch()

    def _build_motion_tab(self):
        layout = QVBoxLayout(self.motion_tab)
        self.motion_group = QGroupBox("Motion")
        form = QFormLayout(self.motion_group)

        self.default_speed_spin = QSpinBox()
        self.default_speed_spin.setRange(1, 100)
        self.default_speed_spin.setValue(50)
        self.default_step_dist_spin = QDoubleSpinBox()
        self.default_step_dist_spin.setRange(0.1, 200.0)
        self.default_step_dist_spin.setValue(20.0)
        self.default_step_dist_spin.setSuffix(" mm")
        self.default_step_angle_spin = QDoubleSpinBox()
        self.default_step_angle_spin.setRange(0.1, 180.0)
        self.default_step_angle_spin.setValue(5.0)
        self.default_step_angle_spin.setSuffix(" deg")

        form.addRow("Default Speed", self.default_speed_spin)
        form.addRow("Step Distance", self.default_step_dist_spin)
        form.addRow("Step Angle", self.default_step_angle_spin)

        layout.addWidget(self.motion_group)
        layout.addStretch()

    def _build_ui_tab(self):
        layout = QVBoxLayout(self.ui_tab)
        self.ui_group = QGroupBox("UI")
        form = QFormLayout(self.ui_group)

        self.ui_lang_combo = QComboBox()
        self.ui_lang_combo.addItem("中文", "zh")
        self.ui_lang_combo.addItem("English", "en")
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.addItem("Dark", "dark")
        self.jog_style_label = QLabel("Jog Style")
        self.jog_style_combo = QComboBox()
        self.jog_style_combo.addItem("Minimal Line (Recommended)", "line")
        self.jog_style_combo.addItem("Soft Keycap", "soft")

        form.addRow("Language", self.ui_lang_combo)
        form.addRow("Theme", self.theme_combo)
        form.addRow(self.jog_style_label, self.jog_style_combo)

        layout.addWidget(self.ui_group)
        layout.addStretch()

    def set_runtime_summary(self, *, status_text: str, config_path: str, serial_port: str) -> None:
        self.runtime_status_value.setText(str(status_text or "--"))
        self.sdk_config_path_value.setText(str(config_path or "--"))
        self.serial_port_value.setText(str(serial_port or "--"))

    def set_connected(self, connected: bool) -> None:
        self._connected = bool(connected)

    def set_texts(self, tr):
        self.tabs.setTabText(0, tr("settings_hardware"))
        self.tabs.setTabText(1, tr("settings_robot"))
        self.tabs.setTabText(2, tr("settings_motion"))
        self.tabs.setTabText(3, tr("settings_ui"))
        self.hardware_group.setTitle(tr("settings_hardware"))
        self.robot_group.setTitle("URDF")
        self.motion_group.setTitle(tr("settings_motion"))
        self.ui_group.setTitle(tr("settings_ui"))

        idx = self.camera_source_combo.findData("virtual")
        if idx >= 0:
            self.camera_source_combo.setItemText(idx, tr("camera_source_virtual"))
        idx = self.camera_source_combo.findData("v4l2")
        if idx >= 0:
            self.camera_source_combo.setItemText(idx, tr("camera_source_v4l2"))

        idx = self.jog_style_combo.findData("line")
        if idx >= 0:
            self.jog_style_combo.setItemText(idx, tr("settings_jog_minimal"))
        idx = self.jog_style_combo.findData("soft")
        if idx >= 0:
            self.jog_style_combo.setItemText(idx, tr("settings_jog_soft"))

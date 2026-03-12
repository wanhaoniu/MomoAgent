"""Settings page with secondary tabs."""

import os

from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.connection_tab = QWidget()
        self.robot_tab = QWidget()
        self.motion_tab = QWidget()
        self.ui_tab = QWidget()

        self.tabs.addTab(self.connection_tab, "Connection")
        self.tabs.addTab(self.robot_tab, "Robot Model / URDF")
        self.tabs.addTab(self.motion_tab, "Motion")
        self.tabs.addTab(self.ui_tab, "UI")

        self._build_connection_tab()
        self._build_robot_tab()
        self._build_motion_tab()
        self._build_ui_tab()

    def _build_connection_tab(self):
        layout = QVBoxLayout(self.connection_tab)
        group = QGroupBox("Connection")
        form = QGridLayout(group)

        camera_source_default = str(os.getenv("SOARMMOCE_CAMERA_SOURCE", "udp")).strip().lower()
        camera_device_default = str(
            os.getenv("SOARMMOCE_CAMERA_DEVICE", os.getenv("SOARMMOCE_CAMERA_NAME_HINT", ""))
        ).strip()
        camera_rotation_default = str(os.getenv("SOARMMOCE_CAMERA_ROTATION", "0")).strip()

        self.server_ip_label = QLabel("Server IP")
        self.ip_input = QLineEdit("192.168.66.130")
        self.control_port_label = QLabel("Control Port")
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(6666)
        self.camera_source_label = QLabel("Camera Source")
        self.camera_source_combo = QComboBox()
        self.camera_source_combo.addItem("UDP Stream", "udp")
        self.camera_source_combo.addItem("Local V4L2", "v4l2")
        self.camera_port_label = QLabel("Camera Port")
        self.cam_port_input = QSpinBox()
        self.cam_port_input.setRange(1, 65535)
        self.cam_port_input.setValue(6000)
        self.camera_device_label = QLabel("Local Camera")
        self.camera_device_input = QLineEdit(camera_device_default)
        self.camera_device_input.setPlaceholderText("/dev/video0 or LRCP G-720P")
        self.camera_rotation_label = QLabel("Camera Rotation")
        self.camera_rotation_combo = QComboBox()
        self.camera_rotation_combo.addItem("0°", 0)
        self.camera_rotation_combo.addItem("90°", 90)
        self.camera_rotation_combo.addItem("180°", 180)
        self.camera_rotation_combo.addItem("270°", 270)
        self.leader_port_label = QLabel("Leader Serial")
        self.leader_port_input = QLineEdit("/dev/ttyACM1")
        self.leader_id_label = QLabel("Leader ID")
        self.leader_id_combo = QComboBox()
        self.leader_id_combo.addItems(["black_arm_leader", "brown_arm_leader"])

        form.addWidget(self.server_ip_label, 0, 0)
        form.addWidget(self.ip_input, 0, 1)
        form.addWidget(self.control_port_label, 1, 0)
        form.addWidget(self.port_input, 1, 1)
        form.addWidget(self.camera_source_label, 2, 0)
        form.addWidget(self.camera_source_combo, 2, 1)
        form.addWidget(self.camera_port_label, 3, 0)
        form.addWidget(self.cam_port_input, 3, 1)
        form.addWidget(self.camera_device_label, 4, 0)
        form.addWidget(self.camera_device_input, 4, 1)
        form.addWidget(self.camera_rotation_label, 5, 0)
        form.addWidget(self.camera_rotation_combo, 5, 1)
        form.addWidget(self.leader_port_label, 6, 0)
        form.addWidget(self.leader_port_input, 6, 1)
        form.addWidget(self.leader_id_label, 7, 0)
        form.addWidget(self.leader_id_combo, 7, 1)

        btn_row = QHBoxLayout()
        self.test_conn_btn = QPushButton("Test")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setObjectName("disconnectBtn")
        self.disconnect_btn.setEnabled(False)
        btn_row.addWidget(self.test_conn_btn)
        btn_row.addWidget(self.connect_btn)
        btn_row.addWidget(self.disconnect_btn)

        idx = self.camera_source_combo.findData(camera_source_default if camera_source_default in ("udp", "v4l2") else "udp")
        self.camera_source_combo.setCurrentIndex(max(0, idx))
        idx = self.camera_rotation_combo.findData(int(camera_rotation_default) if camera_rotation_default.lstrip("-").isdigit() else 0)
        if idx >= 0:
            self.camera_rotation_combo.setCurrentIndex(idx)
        self.camera_source_combo.currentIndexChanged.connect(self._on_camera_source_changed)
        self._on_camera_source_changed()

        layout.addWidget(group)
        layout.addLayout(btn_row)
        layout.addStretch()

    def _on_camera_source_changed(self):
        is_udp = (self.camera_source_combo.currentData() or "udp") == "udp"
        self.camera_port_label.setEnabled(is_udp)
        self.cam_port_input.setEnabled(is_udp)

        is_v4l2 = not is_udp
        self.camera_device_label.setEnabled(is_v4l2)
        self.camera_device_input.setEnabled(is_v4l2)
        self.camera_rotation_label.setEnabled(is_v4l2)
        self.camera_rotation_combo.setEnabled(is_v4l2)

    def _build_robot_tab(self):
        layout = QVBoxLayout(self.robot_tab)

        urdf_group = QGroupBox("URDF")
        form = QFormLayout(urdf_group)
        self.urdf_path_label = QLabel("--")
        self.urdf_refresh_btn = QPushButton("Auto Detect")
        self.mesh_path_input = QLineEdit()
        self.mesh_path_input.setPlaceholderText("Mesh path (optional)")
        self.render_mode_combo = QComboBox()
        self.render_mode_combo.addItems(["OpenGL Priority", "Fallback"])
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
        form.addRow("Mesh Path", self.mesh_path_input)
        form.addRow("Render Mode", self.render_mode_combo)
        form.addRow("Anti Aliasing", self.aa_mode_combo)
        form.addRow("Material", self.material_preset_combo)
        form.addRow("Background", self.background_theme_combo)
        form.addRow("Camera Preset", self.camera_preset_combo)
        form.addRow("", self.apply_view_btn)
        form.addRow("", self.reset_view_btn)

        layout.addWidget(urdf_group)
        layout.addStretch()

    def _build_motion_tab(self):
        layout = QVBoxLayout(self.motion_tab)
        group = QGroupBox("Motion")
        form = QFormLayout(group)

        self.default_speed_spin = QSpinBox()
        self.default_speed_spin.setRange(1, 100)
        self.default_speed_spin.setValue(50)
        self.default_step_dist_spin = QDoubleSpinBox()
        self.default_step_dist_spin.setRange(0.1, 200.0)
        self.default_step_dist_spin.setValue(5.0)
        self.default_step_dist_spin.setSuffix(" mm")
        self.default_step_angle_spin = QDoubleSpinBox()
        self.default_step_angle_spin.setRange(0.1, 180.0)
        self.default_step_angle_spin.setValue(5.0)
        self.default_step_angle_spin.setSuffix(" deg")
        self.soft_limit_combo = QComboBox()
        self.soft_limit_combo.addItems(["Placeholder", "Enabled", "Disabled"])

        form.addRow("Default Speed", self.default_speed_spin)
        form.addRow("Step Distance", self.default_step_dist_spin)
        form.addRow("Step Angle", self.default_step_angle_spin)
        form.addRow("Soft Limit", self.soft_limit_combo)

        layout.addWidget(group)
        layout.addStretch()

    def _build_ui_tab(self):
        layout = QVBoxLayout(self.ui_tab)
        group = QGroupBox("UI")
        form = QFormLayout(group)

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
        self.layout_pref_combo = QComboBox()
        self.layout_pref_combo.addItems(["Default", "Compact", "Wide"])

        form.addRow("Language", self.ui_lang_combo)
        form.addRow("Theme", self.theme_combo)
        form.addRow(self.jog_style_label, self.jog_style_combo)
        form.addRow("Layout", self.layout_pref_combo)

        layout.addWidget(group)
        layout.addStretch()

    def set_connected(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)

    def set_texts(self, tr):
        self.tabs.setTabText(0, tr("settings_connection"))
        self.tabs.setTabText(1, tr("settings_robot"))
        self.tabs.setTabText(2, tr("settings_motion"))
        self.tabs.setTabText(3, tr("settings_ui"))

        self.server_ip_label.setText(tr("label_server_ip"))
        self.control_port_label.setText(tr("label_ctl_port"))
        self.camera_source_label.setText(tr("label_camera_source"))
        self.camera_port_label.setText(tr("label_cam_port"))
        self.camera_device_label.setText(tr("label_camera_device"))
        self.camera_rotation_label.setText(tr("label_camera_rotation"))
        self.leader_port_label.setText(tr("label_leader_port"))
        self.leader_id_label.setText(tr("label_leader_id"))
        self.jog_style_label.setText(tr("settings_jog_style"))

        idx = self.camera_source_combo.findData("udp")
        if idx >= 0:
            self.camera_source_combo.setItemText(idx, tr("camera_source_udp"))
        idx = self.camera_source_combo.findData("v4l2")
        if idx >= 0:
            self.camera_source_combo.setItemText(idx, tr("camera_source_v4l2"))

        idx = self.jog_style_combo.findData("line")
        if idx >= 0:
            self.jog_style_combo.setItemText(idx, tr("settings_jog_minimal"))
        idx = self.jog_style_combo.findData("soft")
        if idx >= 0:
            self.jog_style_combo.setItemText(idx, tr("settings_jog_soft"))

        self.connect_btn.setText(tr("btn_connect"))
        self.disconnect_btn.setText(tr("btn_disconnect"))

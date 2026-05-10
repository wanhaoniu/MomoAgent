"""Quick Move page (three-column layout)."""

from __future__ import annotations

from PyQt5.QtCore import QByteArray, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QIcon, QPainter, QPixmap, QTransform
from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt5.QtSvg import QSvgRenderer

    _SVG_AVAILABLE = True
except Exception:
    QSvgRenderer = None
    _SVG_AVAILABLE = False


_ARROW_PATH = (
    "M645.2 749.2l311.4-232.8c2.7-2 2.7-6 0-8L645.2 275.7c-3.3-2.5-8-0.1-8 4v152.6H69.8c-2.8 "
    "0-5 2.2-5 5v150.3c0 2.8 2.2 5 5 5h567.4v152.5c0 4.2 4.7 6.5 8 4.1z"
)
_ROTATE_PATH = (
    "M984.630527 826.955786l-511.803153-78.738946 170.863514-156.61176501A386.41138 386.41138 "
    "0 0 0 289.090042 354.522107a378.616225 378.616225 0 0 0-171.769012 41.37731599l-117.360399-"
    "119.40761199A567.156632 567.156632 0 0 1 289.090042 197.044214a575.424221 575.424221 0 0 1 "
    "487.551557 272.672972L945.261053 315.152634z"
)
_ARROW_LINE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
    '<path d="M512 184L512 842" fill="none" stroke="{color}" stroke-width="94" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M334 366L512 184L690 366" fill="none" stroke="{color}" stroke-width="94" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)
_ROTATE_LINE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
    '<path d="M214 646A306 306 0 1 1 808 646" fill="none" stroke="{color}" stroke-width="82" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M706 530L820 646L674 730" fill="none" stroke="{color}" stroke-width="82" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)
_ARROW_ROTATION_OFFSET = -90


class QuickMovePage(QWidget):
    JOG_STYLE_LINE = "line"
    JOG_STYLE_SOFT = "soft"

    speed_changed = pyqtSignal(int)
    home_clicked = pyqtSignal()
    pose_move_requested = pyqtSignal(dict)
    calibration_clicked = pyqtSignal()
    record_sequence_clicked = pyqtSignal()
    replay_sequence_clicked = pyqtSignal()
    stop_sequence_clicked = pyqtSignal()
    open_sequence_file_clicked = pyqtSignal()
    pose_target_changed = pyqtSignal(dict)
    pose_preview_requested = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._theme = "light"
        self._jog_style = self.JOG_STYLE_LINE
        self._jog_icon_color = "#FFFFFF"
        self._jog_icon_cache = {}
        self._target_pose_initialized = False
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        main = QHBoxLayout()
        main.setSpacing(10)
        root.addLayout(main, stretch=1)

        self.jog_buttons = {}
        self.joint_rows = []
        self.pose_labels = {}

        self.step_mode_combo = QComboBox()
        self.step_mode_combo.addItems(["Step", "Continuous"])
        self.step_mode_combo.setCurrentIndex(0)
        self.step_dist_spin = QDoubleSpinBox()
        self.step_dist_spin.setRange(0.1, 200.0)
        self.step_dist_spin.setValue(5.0)
        self.step_dist_spin.setSuffix(" mm")
        self.step_angle_spin = QDoubleSpinBox()
        self.step_angle_spin.setRange(0.1, 180.0)
        self.step_angle_spin.setValue(5.0)
        self.step_angle_spin.setSuffix(" deg")

        self.left_group = QGroupBox("Control")
        left_layout = QVBoxLayout(self.left_group)
        left_layout.setContentsMargins(8, 10, 8, 8)
        left_layout.setSpacing(8)
        self.control_tabs = QTabWidget()
        self.control_tabs.setObjectName("controlTabs")
        left_layout.addWidget(self.control_tabs)
        self.left_group.setMinimumWidth(390)
        self.left_group.setMaximumWidth(470)

        self.joint_tab = self._build_joint_tab()
        self.sequence_tab = self._build_sequence_tab()
        self.inverse_tab = self._build_inverse_tab()
        self.tools_tab = self._build_tools_tab()
        self.control_tabs.addTab(self.joint_tab, "Joint")
        self.control_tabs.addTab(self.sequence_tab, "Sequence")
        self.control_tabs.addTab(self.inverse_tab, "IK")
        self.control_tabs.addTab(self.tools_tab, "Tools")

        main.addWidget(self.left_group, stretch=0)

        self.center_group = QGroupBox("3D View")
        center_layout = QVBoxLayout(self.center_group)
        center_layout.setSpacing(10)
        axis_row = QHBoxLayout()
        self.tcp_summary_label = QLabel("TCP: --")
        self.tcp_summary_label.setObjectName("tcpSummaryLabel")
        self.coord_label = QLabel("Coordinate")
        self.coord_combo = QComboBox()
        self.coord_combo.addItems(["Base", "Tool", "User"])
        axis_row.addWidget(self.tcp_summary_label)
        axis_row.addStretch()
        axis_row.addWidget(self.coord_label)
        axis_row.addWidget(self.coord_combo)

        # Keep speed controls for existing logic, but remove from this top row layout.
        self.speed_label = QLabel("Speed")
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(1, 100)
        self.speed_slider.setValue(50)
        self.speed_value = QLabel("50%")
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        self.speed_label.setVisible(False)
        self.speed_slider.setVisible(False)
        self.speed_value.setVisible(False)

        center_layout.addLayout(axis_row)

        self.sim_host = QWidget()
        self.sim_host_layout = QVBoxLayout(self.sim_host)
        self.sim_host_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(self.sim_host, stretch=1)

        main.addWidget(self.center_group, stretch=1)

        quick_action_row = QHBoxLayout()
        self.goto_zero_btn = QPushButton("Home")
        self.free_move_btn = QPushButton("Free Move")
        self.status_light = QLabel("●")
        self.status_light.setStyleSheet("color:#10B981; font-size:16px;")

        self.goto_zero_btn.clicked.connect(self.home_clicked.emit)

        quick_action_row.addWidget(self.goto_zero_btn)
        quick_action_row.addWidget(self.free_move_btn)
        quick_action_row.addStretch()
        quick_action_row.addWidget(self.status_light)

        root.addLayout(quick_action_row)

        self._motion_enabled = False
        self._cartesian_motion_enabled = False
        self._refresh_motion_controls()

    def _build_joint_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.joint_group = QGroupBox("Joint Control")
        joint_layout = QVBoxLayout(self.joint_group)
        joint_layout.setSpacing(8)
        for idx in range(6):
            row = QHBoxLayout()
            row.setSpacing(8)
            minus_btn = QPushButton("-")
            plus_btn = QPushButton("+")
            minus_btn.setAutoRepeat(False)
            plus_btn.setAutoRepeat(False)
            minus_btn.setEnabled(False)
            plus_btn.setEnabled(False)
            value_label = QLabel("0.000")
            value_label.setObjectName("jointValueLabel")
            value_label.setAlignment(Qt.AlignCenter)
            joint_label = QLabel(f"J{idx + 1}")
            joint_label.setMinimumWidth(28)
            row.addWidget(joint_label)
            row.addWidget(minus_btn)
            row.addWidget(value_label, stretch=1)
            row.addWidget(plus_btn)
            joint_layout.addLayout(row)
            self.joint_rows.append((joint_label, minus_btn, value_label, plus_btn))

        step_form = QFormLayout()
        step_form.addRow("Step Angle", self.step_angle_spin)
        joint_layout.addLayout(step_form)

        layout.addWidget(self.joint_group)

        self.pose_group = QGroupBox("TCP")
        pose_grid = QGridLayout(self.pose_group)
        pose_grid.setHorizontalSpacing(10)
        pose_grid.setVerticalSpacing(8)
        for i, key in enumerate(["X", "Y", "Z", "Rx", "Ry", "Rz"]):
            name = QLabel(key)
            value = QLabel("--")
            value.setObjectName("poseValueLabel")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            pose_grid.addWidget(name, i // 2, (i % 2) * 2)
            pose_grid.addWidget(value, i // 2, (i % 2) * 2 + 1)
            self.pose_labels[key] = value
        layout.addWidget(self.pose_group)
        layout.addStretch()
        return tab

    def _build_sequence_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        form = QFormLayout()
        self.sequence_pose_count_spin = QSpinBox()
        self.sequence_pose_count_spin.setRange(1, 20)
        self.sequence_pose_count_spin.setValue(3)
        self.sequence_move_duration_spin = QDoubleSpinBox()
        self.sequence_move_duration_spin.setRange(0.2, 20.0)
        self.sequence_move_duration_spin.setValue(1.5)
        self.sequence_move_duration_spin.setSuffix(" s")
        form.addRow("Poses", self.sequence_pose_count_spin)
        form.addRow("Move", self.sequence_move_duration_spin)
        layout.addLayout(form)

        self.sequence_list = QListWidget()
        layout.addWidget(self.sequence_list, stretch=1)

        grid = QGridLayout()
        self.record_sequence_btn = QPushButton("Record")
        self.replay_sequence_btn = QPushButton("Replay")
        self.stop_sequence_btn = QPushButton("Stop")
        self.open_sequence_file_btn = QPushButton("Open JSON")
        grid.addWidget(self.record_sequence_btn, 0, 0)
        grid.addWidget(self.replay_sequence_btn, 0, 1)
        grid.addWidget(self.stop_sequence_btn, 1, 0)
        grid.addWidget(self.open_sequence_file_btn, 1, 1)
        layout.addLayout(grid)

        self.sequence_status_label = QLabel("sdk/workspace/runtime/recorded_pose_sequence.json")
        self.sequence_status_label.setObjectName("hintLabel")
        self.sequence_status_label.setWordWrap(True)
        layout.addWidget(self.sequence_status_label)

        self.record_sequence_btn.clicked.connect(self.record_sequence_clicked.emit)
        self.replay_sequence_btn.clicked.connect(self.replay_sequence_clicked.emit)
        self.stop_sequence_btn.clicked.connect(self.stop_sequence_clicked.emit)
        self.open_sequence_file_btn.clicked.connect(self.open_sequence_file_clicked.emit)
        return tab

    def _build_inverse_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.pose_target_group = QGroupBox("Target TCP")
        grid = QGridLayout(self.pose_target_group)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.pose_target_spins = {}
        specs = [
            ("X", -1.0, 1.0, 4, " m"),
            ("Y", -1.0, 1.0, 4, " m"),
            ("Z", -1.0, 1.0, 4, " m"),
            ("Rx", -3.142, 3.142, 3, " rad"),
            ("Ry", -3.142, 3.142, 3, " rad"),
            ("Rz", -3.142, 3.142, 3, " rad"),
        ]
        for idx, (key, minimum, maximum, decimals, suffix) in enumerate(specs):
            label = QLabel(key)
            spin = QDoubleSpinBox()
            spin.setRange(float(minimum), float(maximum))
            spin.setDecimals(int(decimals))
            spin.setSingleStep(0.001 if decimals >= 4 else 0.01)
            spin.setSuffix(suffix)
            spin.setKeyboardTracking(True)
            grid.addWidget(label, idx, 0)
            grid.addWidget(spin, idx, 1)
            self.pose_target_spins[key] = spin
        layout.addWidget(self.pose_target_group)

        options = QFormLayout()
        self.pose_duration_spin = QDoubleSpinBox()
        self.pose_duration_spin.setRange(0.2, 20.0)
        self.pose_duration_spin.setValue(1.2)
        self.pose_duration_spin.setSuffix(" s")
        options.addRow("Duration", self.pose_duration_spin)
        layout.addLayout(options)

        self.pose_fill_current_btn = QPushButton("Use Current")
        self.pose_preview_btn = QPushButton("Preview")
        self.pose_send_btn = QPushButton("Send")
        self.pose_send_btn.setObjectName("primaryBtn")
        button_row = QHBoxLayout()
        button_row.addWidget(self.pose_fill_current_btn)
        button_row.addWidget(self.pose_preview_btn)
        button_row.addWidget(self.pose_send_btn)
        layout.addLayout(button_row)

        self.pose_fill_current_btn.clicked.connect(self.fill_pose_target_from_current)
        self.pose_preview_btn.clicked.connect(self._on_pose_preview_clicked)
        self.pose_send_btn.clicked.connect(self._on_pose_send_clicked)
        for spin in self.pose_target_spins.values():
            spin.valueChanged.connect(lambda _value: self._emit_pose_target_changed())
            spin.editingFinished.connect(self._emit_pose_target_changed)
        layout.addStretch()
        return tab

    def _build_tools_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.calibration_btn = QPushButton("URDF Calibration")
        self.calibration_btn.setObjectName("primaryBtn")
        self.record_script_btn = QPushButton("Record Script")
        self.replay_script_btn = QPushButton("Replay Script")
        layout.addWidget(self.calibration_btn)
        layout.addWidget(self.record_script_btn)
        layout.addWidget(self.replay_script_btn)
        layout.addStretch()

        self.calibration_btn.clicked.connect(self.calibration_clicked.emit)
        self.record_script_btn.clicked.connect(self.record_sequence_clicked.emit)
        self.replay_script_btn.clicked.connect(self.replay_sequence_clicked.emit)
        return tab

    def fill_pose_target_from_current(self):
        for key, spin in self.pose_target_spins.items():
            raw = str(self.pose_labels.get(key).text() if key in self.pose_labels else "").strip()
            try:
                spin.setValue(float(raw))
            except Exception:
                continue
        self._target_pose_initialized = True
        self._emit_pose_target_changed()

    def ensure_pose_target_initialized(self, xyz, rpy):
        if self._target_pose_initialized:
            return
        try:
            vals = {
                "X": float(xyz[0]),
                "Y": float(xyz[1]),
                "Z": float(xyz[2]),
                "Rx": float(rpy[0]),
                "Ry": float(rpy[1]),
                "Rz": float(rpy[2]),
            }
        except Exception:
            return
        for key, value in vals.items():
            spin = self.pose_target_spins.get(key)
            if spin is None:
                continue
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)
        self._target_pose_initialized = True
        self._emit_pose_target_changed()

    def _pose_target_payload(self) -> dict:
        return {
            "xyz": [
                float(self.pose_target_spins["X"].value()),
                float(self.pose_target_spins["Y"].value()),
                float(self.pose_target_spins["Z"].value()),
            ],
            "rpy": [
                float(self.pose_target_spins["Rx"].value()),
                float(self.pose_target_spins["Ry"].value()),
                float(self.pose_target_spins["Rz"].value()),
            ],
            "duration": float(self.pose_duration_spin.value()),
        }

    def _emit_pose_target_changed(self):
        self._target_pose_initialized = True
        self.pose_target_changed.emit(self._pose_target_payload())

    def _on_pose_send_clicked(self):
        payload = self._pose_target_payload()
        self.pose_target_changed.emit(payload)
        self.pose_move_requested.emit(payload)

    def _on_pose_preview_clicked(self):
        payload = self._pose_target_payload()
        self.pose_target_changed.emit(payload)
        self.pose_preview_requested.emit(payload)

    def set_tcp_summary(self, xyz, rpy):
        try:
            values = [float(xyz[0]), float(xyz[1]), float(xyz[2]), float(rpy[0]), float(rpy[1]), float(rpy[2])]
        except Exception:
            self.tcp_summary_label.setText("TCP: --")
            return
        self.tcp_summary_label.setText(
            "TCP: X {0:.3f}  Y {1:.3f}  Z {2:.3f} m  |  Rx {3:.3f}  Ry {4:.3f}  Rz {5:.3f} rad".format(
                *values
            )
        )

    def _make_jog_key(
        self,
        key: str,
        symbol: str,
        icon_kind: str = "arrow",
        rotation: int = 0,
        mirror_x: bool = False,
    ) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("jogKeyBtn")
        btn.setFixedSize(58, 58)
        btn.setProperty("jogStyle", self._jog_style)
        btn.setAutoRepeat(False)
        btn.setEnabled(False)
        btn.setToolTip(key)
        btn._jog_icon_spec = (icon_kind, int(rotation), bool(mirror_x), symbol)
        self._apply_jog_icon(btn)
        self.jog_buttons[key] = btn
        return btn

    def _make_jog_hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("jogHintLabel")
        label.setAlignment(Qt.AlignCenter)
        return label

    def _build_translation_pad(self) -> QWidget:
        pad = QFrame()
        pad.setObjectName("jogPadFrame")
        grid = QGridLayout(pad)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        z_plus = self._make_jog_key("+Z", "▲", icon_kind="arrow", rotation=0)
        z_minus = self._make_jog_key("-Z", "▼", icon_kind="arrow", rotation=180)
        x_plus = self._make_jog_key("+X", "▲", icon_kind="arrow", rotation=0)
        x_minus = self._make_jog_key("-X", "▼", icon_kind="arrow", rotation=180)
        y_plus = self._make_jog_key("+Y", "◀", icon_kind="arrow", rotation=270)
        y_minus = self._make_jog_key("-Y", "▶", icon_kind="arrow", rotation=90)

        grid.addWidget(self._make_jog_hint("Up\n(+Z)"), 0, 0)
        grid.addWidget(z_plus, 0, 1)
        grid.addWidget(z_minus, 0, 3)
        grid.addWidget(self._make_jog_hint("Down\n(-Z)"), 0, 4)

        center = QFrame()
        center.setObjectName("jogPadCenter")
        center_grid = QGridLayout(center)
        center_grid.setContentsMargins(10, 10, 10, 10)
        center_grid.setHorizontalSpacing(8)
        center_grid.setVerticalSpacing(8)
        center_grid.addWidget(x_plus, 0, 1, alignment=Qt.AlignCenter)
        center_grid.addWidget(y_plus, 1, 0, alignment=Qt.AlignCenter)
        center_grid.addWidget(y_minus, 1, 2, alignment=Qt.AlignCenter)
        center_grid.addWidget(x_minus, 2, 1, alignment=Qt.AlignCenter)

        grid.addWidget(center, 1, 1, 3, 3)
        grid.addWidget(self._make_jog_hint("Left\n(+Y)"), 2, 0)
        grid.addWidget(self._make_jog_hint("Right\n(-Y)"), 2, 4)
        grid.addWidget(self._make_jog_hint("Forward\n(+X)"), 1, 4)
        grid.addWidget(self._make_jog_hint("Back\n(-X)"), 3, 4)
        return pad

    def _build_rotation_pad(self) -> QWidget:
        pad = QFrame()
        pad.setObjectName("jogPadFrame")
        grid = QGridLayout(pad)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        rz_minus = self._make_jog_key("-Rz", "↺", icon_kind="rz", rotation=0, mirror_x=True)
        rz_plus = self._make_jog_key("+Rz", "↻", icon_kind="rz", rotation=0, mirror_x=False)
        # Keep Rz as circular-rotation semantics; use directional arrows for Rx/Ry
        # so the visual direction is explicit and consistent with +/- labels.
        ry_plus = self._make_jog_key("+Ry", "▲", icon_kind="arrow", rotation=0, mirror_x=False)
        ry_minus = self._make_jog_key("-Ry", "▼", icon_kind="arrow", rotation=180, mirror_x=False)
        rx_plus = self._make_jog_key("+Rx", "◀", icon_kind="arrow", rotation=270, mirror_x=False)
        rx_minus = self._make_jog_key("-Rx", "▶", icon_kind="arrow", rotation=90, mirror_x=False)

        top_bar = QFrame()
        top_bar.setObjectName("jogPadArc")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(12, 10, 12, 10)
        top_bar_layout.addWidget(rz_minus)
        top_bar_layout.addStretch()
        top_bar_layout.addWidget(rz_plus)
        grid.addWidget(top_bar, 0, 1, 1, 3)
        grid.addWidget(self._make_jog_hint("-Rz"), 0, 0)
        grid.addWidget(self._make_jog_hint("+Rz"), 0, 4)

        center = QFrame()
        center.setObjectName("jogPadCenter")
        center_grid = QGridLayout(center)
        center_grid.setContentsMargins(10, 10, 10, 10)
        center_grid.setHorizontalSpacing(8)
        center_grid.setVerticalSpacing(8)
        center_grid.addWidget(ry_plus, 0, 1, alignment=Qt.AlignCenter)
        center_grid.addWidget(rx_plus, 1, 0, alignment=Qt.AlignCenter)
        center_grid.addWidget(rx_minus, 1, 2, alignment=Qt.AlignCenter)
        center_grid.addWidget(ry_minus, 2, 1, alignment=Qt.AlignCenter)
        grid.addWidget(center, 2, 1, 3, 3)

        grid.addWidget(self._make_jog_hint("+Rx"), 3, 0)
        grid.addWidget(self._make_jog_hint("-Rx"), 3, 4)
        grid.addWidget(self._make_jog_hint("+Ry"), 1, 2)
        grid.addWidget(self._make_jog_hint("-Ry"), 5, 2)
        return pad

    def _render_svg_markup_icon(
        self,
        svg_markup: str,
        size: int,
        rotation: int,
        mirror_x: bool,
        cache_key: tuple,
    ) -> QIcon:
        if not _SVG_AVAILABLE or QSvgRenderer is None:
            return QIcon()

        key = (cache_key, size, int(rotation) % 360, bool(mirror_x), self._jog_icon_color)
        cached = self._jog_icon_cache.get(key)
        if cached is not None:
            return cached

        renderer = QSvgRenderer(QByteArray(svg_markup.encode("utf-8")))
        if not renderer.isValid():
            return QIcon()

        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        renderer.render(painter)
        painter.end()

        transform = QTransform()
        if mirror_x:
            transform.scale(-1.0, 1.0)
            transform.translate(-size, 0)
        if rotation:
            transform.translate(size / 2.0, size / 2.0)
            transform.rotate(int(rotation))
            transform.translate(-size / 2.0, -size / 2.0)
        if not transform.isIdentity():
            pix = pix.transformed(transform, Qt.SmoothTransformation)

        icon = QIcon(pix)
        self._jog_icon_cache[key] = icon
        return icon

    def _render_filled_path_icon(self, path_data: str, size: int, rotation: int, mirror_x: bool) -> QIcon:
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            f'<path d="{path_data}" fill="{self._jog_icon_color}"/></svg>'
        )
        return self._render_svg_markup_icon(svg, size, rotation, mirror_x, ("filled", path_data))

    def _render_line_icon(self, icon_kind: str, size: int, rotation: int, mirror_x: bool) -> QIcon:
        if icon_kind == "arrow":
            svg = _ARROW_LINE_SVG.format(color=self._jog_icon_color)
            return self._render_svg_markup_icon(svg, size, rotation, mirror_x, ("line", "arrow"))
        svg = _ROTATE_LINE_SVG.format(color=self._jog_icon_color)
        return self._render_svg_markup_icon(svg, size, rotation, mirror_x, ("line", "rotate"))

    def _apply_jog_icon(self, button: QPushButton):
        spec = getattr(button, "_jog_icon_spec", None)
        if not spec:
            return
        icon_kind, rotation, mirror_x, fallback = spec
        if icon_kind == "rz":
            icon = self._render_filled_path_icon(_ROTATE_PATH, 30, rotation, mirror_x)
        elif icon_kind == "arrow":
            corrected_rotation = int(rotation) + _ARROW_ROTATION_OFFSET
            icon = self._render_filled_path_icon(_ARROW_PATH, 30, corrected_rotation, mirror_x)
        elif self._jog_style == self.JOG_STYLE_LINE:
            icon = self._render_line_icon(icon_kind, 30, rotation, mirror_x)
        else:
            path_data = _ARROW_PATH if icon_kind == "arrow" else _ROTATE_PATH
            icon = self._render_filled_path_icon(path_data, 30, rotation, mirror_x)
        if icon.isNull():
            button.setIcon(QIcon())
            button.setText(fallback)
        else:
            button.setText("")
            button.setIcon(icon)
            button.setIconSize(QSize(26, 26))

    def _refresh_jog_icons(self):
        self._jog_icon_cache.clear()
        for btn in self.jog_buttons.values():
            self._apply_jog_icon(btn)

    def _update_jog_icon_color(self):
        if self._jog_style == self.JOG_STYLE_SOFT:
            self._jog_icon_color = "#EAF3FF" if self._theme == "dark" else "#FFFFFF"
        else:
            self._jog_icon_color = "#3748CA"

    def _repolish(self, widget: QWidget):
        style = widget.style()
        if style is None:
            return
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def set_jog_visual_style(self, style: str):
        style_norm = str(style).strip().lower()
        if style_norm not in (self.JOG_STYLE_LINE, self.JOG_STYLE_SOFT):
            style_norm = self.JOG_STYLE_LINE
        self._jog_style = style_norm
        for btn in self.jog_buttons.values():
            btn.setProperty("jogStyle", style_norm)
            self._repolish(btn)
        self._update_jog_icon_color()
        self._refresh_jog_icons()

    def set_theme(self, theme: str):
        theme_norm = str(theme).strip().lower()
        if theme_norm not in ("light", "dark"):
            theme_norm = "light"
        self._theme = theme_norm
        self._update_jog_icon_color()
        self._refresh_jog_icons()

    def _on_speed_changed(self, value: int):
        self.speed_value.setText(f"{value}%")
        self.speed_changed.emit(value)

    def set_sim_widget(self, sim_widget: QWidget):
        while self.sim_host_layout.count():
            item = self.sim_host_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self.sim_host_layout.addWidget(sim_widget)

    def set_motion_enabled(self, enabled: bool):
        self._motion_enabled = bool(enabled)
        self._refresh_motion_controls()

    def set_cartesian_enabled(self, enabled: bool):
        self._cartesian_motion_enabled = bool(enabled)
        self._refresh_motion_controls()

    def _refresh_motion_controls(self):
        motion_enabled = bool(self._motion_enabled)
        cartesian_enabled = bool(motion_enabled and self._cartesian_motion_enabled)

        for _, minus_btn, _, plus_btn in self.joint_rows:
            minus_btn.setEnabled(motion_enabled)
            plus_btn.setEnabled(motion_enabled)

        self.goto_zero_btn.setEnabled(motion_enabled)
        self.step_angle_spin.setEnabled(motion_enabled)

        for btn in self.jog_buttons.values():
            btn.setEnabled(cartesian_enabled)
        self.step_mode_combo.setEnabled(cartesian_enabled)
        self.step_dist_spin.setEnabled(cartesian_enabled)
        self.coord_label.setEnabled(cartesian_enabled)
        self.coord_combo.setEnabled(cartesian_enabled)
        self.free_move_btn.setEnabled(motion_enabled)

    def set_status_light(self, level: str):
        color = {
            "normal": "#10B981",
            "warning": "#F59E0B",
            "fault": "#EF4444",
        }.get(level, "#10B981")
        self.status_light.setStyleSheet(f"color:{color}; font-size:16px;")

    def set_texts(self, tr):
        self.left_group.setTitle(tr("quick_control_title"))
        self.center_group.setTitle(tr("quick_center_title"))
        self.joint_group.setTitle(tr("quick_right_title"))
        self.control_tabs.setTabText(0, tr("quick_tab_joint"))
        self.control_tabs.setTabText(1, tr("quick_tab_sequence"))
        self.control_tabs.setTabText(2, tr("quick_tab_inverse"))
        self.control_tabs.setTabText(3, tr("quick_tab_tools"))
        self.coord_label.setText(tr("quick_coord"))
        self.speed_label.setText(tr("quick_speed"))
        self.pose_group.setTitle(tr("quick_tcp"))
        self.pose_target_group.setTitle(tr("quick_pose_target"))
        self.pose_fill_current_btn.setText(tr("quick_pose_fill_current"))
        self.pose_preview_btn.setText(tr("quick_pose_preview"))
        self.pose_send_btn.setText(tr("quick_pose_send"))
        self.record_sequence_btn.setText(tr("quick_record_sequence"))
        self.replay_sequence_btn.setText(tr("quick_replay_sequence"))
        self.stop_sequence_btn.setText(tr("quick_stop_sequence"))
        self.open_sequence_file_btn.setText(tr("quick_sequence_file"))
        self.calibration_btn.setText(tr("quick_calibration"))
        self.record_script_btn.setText(tr("quick_record_script"))
        self.replay_script_btn.setText(tr("quick_replay_script"))
        self.goto_zero_btn.setText(tr("quick_zero"))
        self.free_move_btn.setText(tr("quick_free"))

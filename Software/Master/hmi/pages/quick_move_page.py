"""Quick Move page (three-column layout)."""

from __future__ import annotations

from PyQt5.QtCore import QByteArray, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QIcon, QPainter, QPixmap, QTransform
from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
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

    def __init__(self):
        super().__init__()
        self._theme = "light"
        self._jog_style = self.JOG_STYLE_LINE
        self._jog_icon_color = "#FFFFFF"
        self._jog_icon_cache = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        top = QHBoxLayout()
        root.addLayout(top, stretch=1)

        self.left_group = QGroupBox("Cartesian Jog")
        left_layout = QVBoxLayout(self.left_group)
        left_layout.setSpacing(12)

        self.jog_buttons = {}
        trans_widget = self._build_translation_pad()
        rot_widget = self._build_rotation_pad()

        left_layout.addWidget(trans_widget)
        left_layout.addWidget(rot_widget)

        step_row = QHBoxLayout()
        self.step_mode_combo = QComboBox()
        self.step_mode_combo.addItems(["Step", "Continuous"])
        self.step_mode_combo.setCurrentIndex(0)
        self.step_dist_spin = QDoubleSpinBox()
        self.step_dist_spin.setRange(0.1, 200.0)
        self.step_dist_spin.setValue(20.0)
        self.step_dist_spin.setSuffix(" mm")
        self.step_angle_spin = QDoubleSpinBox()
        self.step_angle_spin.setRange(0.1, 180.0)
        self.step_angle_spin.setValue(5.0)
        self.step_angle_spin.setSuffix(" deg")

        step_row.addWidget(self.step_mode_combo)
        step_row.addWidget(self.step_dist_spin)
        step_row.addWidget(self.step_angle_spin)
        left_layout.addLayout(step_row)

        top.addWidget(self.left_group, stretch=1)

        self.center_group = QGroupBox("3D View")
        center_layout = QVBoxLayout(self.center_group)
        axis_row = QHBoxLayout()
        self.coord_label = QLabel("Coordinate")
        self.coord_combo = QComboBox()
        self.coord_combo.addItems(["Base", "Tool", "User"])
        axis_row.addWidget(self.coord_label)
        axis_row.addWidget(self.coord_combo)
        axis_row.addStretch()

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

        top.addWidget(self.center_group, stretch=2)

        self.right_group = QGroupBox("Joint Control")
        right_layout = QVBoxLayout(self.right_group)

        self.joint_rows = []
        for idx in range(6):
            row = QHBoxLayout()
            minus_btn = QPushButton("-")
            plus_btn = QPushButton("+")
            minus_btn.setAutoRepeat(False)
            plus_btn.setAutoRepeat(False)
            minus_btn.setEnabled(False)
            plus_btn.setEnabled(False)
            value_label = QLabel("0.00")
            joint_label = QLabel(f"J{idx + 1}")
            row.addWidget(joint_label)
            row.addWidget(minus_btn)
            row.addWidget(value_label)
            row.addWidget(plus_btn)
            right_layout.addLayout(row)
            self.joint_rows.append((joint_label, minus_btn, value_label, plus_btn))

        self.pose_group = QGroupBox("TCP")
        pose_grid = QGridLayout(self.pose_group)
        self.pose_labels = {}
        keys = ["X", "Y", "Z", "Rx", "Ry", "Rz"]
        for i, key in enumerate(keys):
            name = QLabel(key)
            value = QLabel("--")
            pose_grid.addWidget(name, i, 0)
            pose_grid.addWidget(value, i, 1)
            self.pose_labels[key] = value
        right_layout.addWidget(self.pose_group)

        top.addWidget(self.right_group, stretch=1)

        bottom = QHBoxLayout()
        self.goto_origin_btn = QPushButton("Origin")
        self.goto_zero_btn = QPushButton("Home")
        self.free_move_btn = QPushButton("Free Move")
        self.status_light = QLabel("●")
        self.status_light.setStyleSheet("color:#10B981; font-size:16px;")

        self.goto_zero_btn.clicked.connect(self.home_clicked.emit)

        bottom.addWidget(self.goto_origin_btn)
        bottom.addWidget(self.goto_zero_btn)
        bottom.addWidget(self.free_move_btn)
        bottom.addStretch()
        bottom.addWidget(self.status_light)

        root.addLayout(bottom)

        self._motion_enabled = False
        self._cartesian_motion_enabled = False
        self._refresh_motion_controls()

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

        self.goto_origin_btn.setEnabled(motion_enabled)
        self.goto_zero_btn.setEnabled(motion_enabled)
        self.step_angle_spin.setEnabled(motion_enabled)

        for btn in self.jog_buttons.values():
            btn.setEnabled(cartesian_enabled)
        self.step_mode_combo.setEnabled(cartesian_enabled)
        self.step_dist_spin.setEnabled(cartesian_enabled)
        self.coord_label.setEnabled(cartesian_enabled)
        self.coord_combo.setEnabled(cartesian_enabled)
        self.free_move_btn.setEnabled(cartesian_enabled)

    def set_status_light(self, level: str):
        color = {
            "normal": "#10B981",
            "warning": "#F59E0B",
            "fault": "#EF4444",
        }.get(level, "#10B981")
        self.status_light.setStyleSheet(f"color:{color}; font-size:16px;")

    def set_texts(self, tr):
        self.left_group.setTitle(tr("quick_left_title"))
        self.center_group.setTitle(tr("quick_center_title"))
        self.right_group.setTitle(tr("quick_right_title"))
        self.coord_label.setText(tr("quick_coord"))
        self.speed_label.setText(tr("quick_speed"))
        self.pose_group.setTitle(tr("quick_tcp"))
        self.goto_origin_btn.setText(tr("quick_origin"))
        self.goto_zero_btn.setText(tr("quick_zero"))
        self.free_move_btn.setText(tr("quick_free"))

"""Reusable UI widgets for the HMI."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class StatusCard(QFrame):
    """Simple status card used on Home page."""

    def __init__(self, title: str, value: str = "--", detail: str = ""):
        super().__init__()
        self.setObjectName("statusCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("statusCardTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("statusCardValue")
        self.detail_label = QLabel(detail)
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("statusCardDetail")

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)

    def set_title(self, text: str):
        self.title_label.setText(text)

    def set_value(self, text: str):
        self.value_label.setText(text)

    def set_detail(self, text: str):
        self.detail_label.setText(text)


class GlobalStatusBar(QFrame):
    """Always-visible global status bar at the bottom of main window."""

    def __init__(self):
        super().__init__()
        self.setObjectName("globalStatusBar")
        self.setMinimumHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(14)

        self.connection_label = QLabel("Connection: --")
        self.robot_label = QLabel("Robot: --")
        self.mode_label = QLabel("Mode: --")
        self.rtt_label = QLabel("RTT: --")
        self.speed_label = QLabel("Speed: --")
        self.owner_label = QLabel("Owner: --")

        for label in (
            self.connection_label,
            self.robot_label,
            self.mode_label,
            self.rtt_label,
            self.speed_label,
            self.owner_label,
        ):
            label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            layout.addWidget(label)

        layout.addStretch()
        self.action_layout = QHBoxLayout()
        self.action_layout.setSpacing(8)
        layout.addLayout(self.action_layout)

    def set_connection(self, text: str):
        self.connection_label.setText(text)

    def set_robot(self, text: str):
        self.robot_label.setText(text)

    def set_mode(self, text: str):
        self.mode_label.setText(text)

    def set_rtt(self, text: str):
        self.rtt_label.setText(text)

    def set_speed(self, text: str):
        self.speed_label.setText(text)

    def set_owner(self, text: str):
        self.owner_label.setText(text)

    def add_action_widget(self, widget: QWidget):
        self.action_layout.addWidget(widget)

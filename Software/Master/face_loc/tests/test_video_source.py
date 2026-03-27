from __future__ import annotations

import cv2
import pytest

from face_tracking.config import SourceConfig
from face_tracking.video_source import MacOSVideoDevice, VideoSource


class FakeCapture:
    def __init__(self, opened: bool) -> None:
        self._opened = opened

    def isOpened(self) -> bool:
        return self._opened

    def release(self) -> None:
        return None

    def set(self, *_args: object, **_kwargs: object) -> bool:
        return True


def test_open_falls_back_from_dev_video_to_avfoundation_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = VideoSource(SourceConfig(type="capture", capture_uri="/dev/video0", api_preference="v4l2"))
    attempts: list[tuple[object, object | None]] = []

    def fake_video_capture(target: object, api: object | None = None) -> FakeCapture:
        attempts.append((target, api))
        return FakeCapture(opened=(target, api) == (0, cv2.CAP_AVFOUNDATION))

    monkeypatch.setattr("face_tracking.video_source.cv2.VideoCapture", fake_video_capture)
    monkeypatch.setattr(
        "face_tracking.video_source._list_macos_video_devices",
        lambda: [MacOSVideoDevice(index=0, name="OsmoPocket3")],
    )
    monkeypatch.setattr("face_tracking.video_source.sys.platform", "darwin")

    source.open()

    assert attempts == [
        (0, cv2.CAP_AVFOUNDATION),
    ]
    assert source.is_opened()


def test_open_prefers_named_macos_camera_over_default_index(monkeypatch: pytest.MonkeyPatch) -> None:
    source = VideoSource(
        SourceConfig(type="capture", capture_uri="/dev/video0", api_preference="v4l2", camera_name="OsmoPocket3")
    )
    attempts: list[tuple[object, object | None]] = []

    def fake_video_capture(target: object, api: object | None = None) -> FakeCapture:
        attempts.append((target, api))
        return FakeCapture(opened=(target, api) == (2, cv2.CAP_AVFOUNDATION))

    monkeypatch.setattr("face_tracking.video_source.cv2.VideoCapture", fake_video_capture)
    monkeypatch.setattr(
        "face_tracking.video_source._list_macos_video_devices",
        lambda: [
            MacOSVideoDevice(index=0, name="“小星果茶”的相机"),
            MacOSVideoDevice(index=2, name="OsmoPocket3"),
        ],
    )
    monkeypatch.setattr("face_tracking.video_source.sys.platform", "darwin")

    source.open()

    assert attempts == [
        (2, cv2.CAP_AVFOUNDATION),
    ]
    assert source.is_opened()


def test_open_prefers_external_camera_over_continuity_camera_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = VideoSource(SourceConfig(type="capture", capture_uri="/dev/video0", api_preference="v4l2"))
    attempts: list[tuple[object, object | None]] = []

    def fake_video_capture(target: object, api: object | None = None) -> FakeCapture:
        attempts.append((target, api))
        return FakeCapture(opened=(target, api) == (1, cv2.CAP_AVFOUNDATION))

    monkeypatch.setattr("face_tracking.video_source.cv2.VideoCapture", fake_video_capture)
    monkeypatch.setattr(
        "face_tracking.video_source._list_macos_video_devices",
        lambda: [
            MacOSVideoDevice(index=0, name="“小星果茶”的相机"),
            MacOSVideoDevice(index=1, name="OsmoPocket3"),
            MacOSVideoDevice(index=2, name="“小星果茶”的桌上视角相机"),
        ],
    )
    monkeypatch.setattr("face_tracking.video_source.sys.platform", "darwin")

    source.open()

    assert attempts == [
        (1, cv2.CAP_AVFOUNDATION),
    ]
    assert source.is_opened()


def test_open_error_mentions_camera_permissions_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = VideoSource(SourceConfig(type="capture", capture_uri="/dev/video0", api_preference="v4l2"))

    def fake_video_capture(_target: object, _api: object | None = None) -> FakeCapture:
        return FakeCapture(opened=False)

    monkeypatch.setattr("face_tracking.video_source.cv2.VideoCapture", fake_video_capture)
    monkeypatch.setattr(
        "face_tracking.video_source._list_macos_video_devices",
        lambda: [MacOSVideoDevice(index=0, name="OsmoPocket3")],
    )
    monkeypatch.setattr("face_tracking.video_source.sys.platform", "darwin")

    with pytest.raises(RuntimeError, match="Privacy & Security > Camera"):
        source.open()

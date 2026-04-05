from face_tracking.config import SelectionConfig
from face_tracking.schemas import FaceDetection
from face_tracking.selection import TargetSelector


def test_selects_largest_face_by_default() -> None:
    selector = TargetSelector(SelectionConfig(strategy="largest_face"))
    detections = [
        FaceDetection(bbox=(10, 10, 50, 50), confidence=0.90),
        FaceDetection(bbox=(10, 10, 120, 120), confidence=0.60),
    ]
    target = selector.select(detections, (480, 640, 3))
    assert target == detections[1]


def test_selects_closest_face_to_center() -> None:
    selector = TargetSelector(SelectionConfig(strategy="closest_to_center"))
    detections = [
        FaceDetection(bbox=(10, 10, 50, 50), confidence=0.90),
        FaceDetection(bbox=(300, 180, 340, 220), confidence=0.60),
    ]
    target = selector.select(detections, (480, 640, 3))
    assert target == detections[1]

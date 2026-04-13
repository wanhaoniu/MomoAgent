from face_tracking.schemas import FaceDetection, compute_offset_payload


def test_face_detection_center_uses_nose_landmark_when_available() -> None:
    detection = FaceDetection(
        bbox=(100.0, 60.0, 220.0, 240.0),
        confidence=0.95,
        landmarks=[
            (130.0, 110.0),
            (190.0, 112.0),
            (162.0, 145.0),
            (140.0, 190.0),
            (186.0, 191.0),
        ],
    )

    assert detection.bbox_center == (160.0, 150.0)
    assert detection.center == (162.0, 145.0)


def test_face_detection_center_falls_back_to_bbox_center_without_landmarks() -> None:
    detection = FaceDetection(bbox=(20.0, 40.0, 100.0, 180.0), confidence=0.8)

    assert detection.bbox_center == (60.0, 110.0)
    assert detection.center == (60.0, 110.0)


def test_compute_offset_payload_uses_shared_target_center() -> None:
    payload = compute_offset_payload((320.0, 240.0), (640, 480))

    assert payload == {"dx": 0.0, "dy": 38.4, "ndx": 0.0, "ndy": 0.16}


def test_compute_offset_payload_can_override_target_center() -> None:
    payload = compute_offset_payload((330.0, 180.0), (640, 480), target_center=(300.0, 150.0))

    assert payload == {"dx": 30.0, "dy": 30.0, "ndx": 0.09375, "ndy": 0.125}

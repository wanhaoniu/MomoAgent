from __future__ import annotations

import cv2
import numpy as np

from face_tracking.config import VisualizerConfig


class FrameVisualizer:
    def __init__(self, config: VisualizerConfig) -> None:
        self.config = config

    def draw(self, frame: np.ndarray, result: dict[str, object]) -> np.ndarray:
        frame_height, frame_width = frame.shape[:2]
        center_x = frame_width // 2
        center_y = frame_height // 2

        cv2.line(frame, (center_x - 25, center_y), (center_x + 25, center_y), (255, 255, 0), 1)
        cv2.line(frame, (center_x, center_y - 25), (center_x, center_y + 25), (255, 255, 0), 1)

        target_face = result.get("target_face")
        if isinstance(target_face, dict):
            bbox = target_face.get("bbox") or [0, 0, 0, 0]
            center = target_face.get("center") or [center_x, center_y]
            confidence = target_face.get("confidence", 0.0)
            area_ratio = target_face.get("area_ratio", 0.0)
            landmarks = target_face.get("landmarks")

            x1, y1, x2, y2 = [int(round(v)) for v in bbox]
            cx, cy = [int(round(v)) for v in center]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (30, 220, 30), 2)
            cv2.circle(frame, (cx, cy), 4, (30, 220, 30), -1)
            cv2.arrowedLine(frame, (center_x, center_y), (cx, cy), (0, 120, 255), 2, tipLength=0.08)

            if self.config.draw_landmarks and landmarks:
                for point in landmarks:
                    px, py = [int(round(v)) for v in point]
                    cv2.circle(frame, (px, py), 2, (255, 255, 255), -1)

            cv2.putText(
                frame,
                f"conf={confidence:.3f} area={area_ratio:.3f}",
                (max(10, x1), max(25, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (30, 220, 30),
                2,
                cv2.LINE_AA,
            )

        combined_hint = result.get("combined_hint") or ["HOLD"]
        fps = float(result.get("fps", 0.0) or 0.0)
        status = result.get("status", "unknown")

        lines = [
            f"status: {status}",
            f"hint: {', '.join(combined_hint)}",
            f"fps: {fps:.2f}",
        ]
        for index, line in enumerate(lines):
            cv2.putText(
                frame,
                line,
                (10, 30 + index * 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (10, 230, 255),
                2,
                cv2.LINE_AA,
            )

        return frame

    def show(self, frame: np.ndarray) -> bool:
        try:
            cv2.imshow(self.config.window_name, frame)
            key = cv2.waitKey(1) & 0xFF
        except cv2.error as exc:
            raise RuntimeError(
                "OpenCV GUI failed. On macOS, cv2.imshow must run on the main thread."
            ) from exc
        return key not in {27, ord("q")}

    def close(self) -> None:
        cv2.destroyAllWindows()

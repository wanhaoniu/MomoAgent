# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, Sequence
import numpy as np

from .base import TransportBase


class SerialTransport(TransportBase):
    """Serial transport placeholder.

    TODO: adapt to existing motor bus implementation (see Software/Master/so101_utils.py).
    """

    def __init__(self, dof: int, port: str, baudrate: int = 115200, timeout: float = 1.0):
        super().__init__(dof)
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)

    def connect(self) -> None:
        raise NotImplementedError("SerialTransport not implemented yet")

    def disconnect(self) -> None:
        pass

    def get_q(self) -> np.ndarray:
        raise NotImplementedError("SerialTransport not implemented yet")

    def send_movej(
        self,
        q: Sequence[float],
        duration: float,
        speed: Optional[float] = None,
        accel: Optional[float] = None,
    ) -> None:
        raise NotImplementedError("SerialTransport not implemented yet")

    def stop(self) -> None:
        pass

    def wait_until_stopped(self, timeout: float | None = None) -> bool:
        # Serial backend is not implemented yet, keep a predictable no-op contract.
        return True

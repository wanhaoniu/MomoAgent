"""HMI package."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path


def _configure_runtime_environment() -> None:
    os.environ.setdefault("PYTHONNOUSERSITE", "1")

    user_site = site.getusersitepackages()
    if user_site:
        normalized_user_site = os.path.normpath(user_site)
        sys.path[:] = [
            path
            for path in sys.path
            if os.path.normpath(path or os.curdir) != normalized_user_site
        ]

    pyqt_plugins = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "PyQt5"
        / "Qt5"
        / "plugins"
    )
    if pyqt_plugins.is_dir():
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(pyqt_plugins / "platforms")
        os.environ["QT_PLUGIN_PATH"] = str(pyqt_plugins)

    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")


_configure_runtime_environment()

from hmi.main_window import ArmControlGUI, main

__all__ = ["ArmControlGUI", "main"]

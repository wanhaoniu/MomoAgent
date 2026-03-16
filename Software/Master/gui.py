#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entry for the refactored multi-page HMI."""

from __future__ import annotations

import os
import re
import site
import sys
from pathlib import Path


def _max_glibcxx_version(lib_path: Path) -> int | None:
    try:
        data = lib_path.read_bytes()
    except OSError:
        return None
    matches = re.findall(rb"GLIBCXX_3\.4\.(\d+)", data)
    if not matches:
        return None
    return max(int(match) for match in matches)


def _maybe_reexec_with_system_libstdcxx() -> None:
    """Mesa's software OpenGL needs a newer libstdc++ than this env ships."""
    if os.environ.get("_SOARMMOCE_LIBSTDCXX_REEXEC") == "1":
        return

    env_libstdcxx = Path(sys.prefix) / "lib" / "libstdc++.so.6"
    system_libstdcxx = Path("/usr/lib/x86_64-linux-gnu/libstdc++.so.6")
    if not env_libstdcxx.is_file() or not system_libstdcxx.is_file():
        return

    env_glibcxx = _max_glibcxx_version(env_libstdcxx)
    system_glibcxx = _max_glibcxx_version(system_libstdcxx)
    if env_glibcxx is None or system_glibcxx is None:
        return
    if system_glibcxx <= env_glibcxx or system_glibcxx < 29:
        return

    preload_entries = [entry for entry in os.environ.get("LD_PRELOAD", "").split() if entry]
    system_libstdcxx_str = str(system_libstdcxx)
    if system_libstdcxx_str not in preload_entries:
        preload_entries.insert(0, system_libstdcxx_str)

    reexec_env = os.environ.copy()
    reexec_env["LD_PRELOAD"] = " ".join(preload_entries)
    reexec_env["_SOARMMOCE_LIBSTDCXX_REEXEC"] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], reexec_env)


def _configure_runtime_environment() -> None:
    """Prefer the active env's Qt/Python packages over user-site overrides."""
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


_maybe_reexec_with_system_libstdcxx()
_configure_runtime_environment()

from hmi.main_window import ArmControlGUI, main


if __name__ == "__main__":
    main()

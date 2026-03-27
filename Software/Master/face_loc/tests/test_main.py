from __future__ import annotations

from pathlib import Path

from face_tracking.main import DEFAULT_CONFIG, resolve_cli_config_path


def test_resolve_cli_config_path_uses_repo_default_for_bare_default_name(monkeypatch) -> None:
    monkeypatch.chdir(Path.home())

    resolved = resolve_cli_config_path("configs/default.yaml")

    assert resolved == DEFAULT_CONFIG.resolve()


def test_resolve_cli_config_path_prefers_existing_cwd_relative_file(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "configs" / "default.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("app_name: test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resolved = resolve_cli_config_path("configs/default.yaml")

    assert resolved == config_path.resolve()

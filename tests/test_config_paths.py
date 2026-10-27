"""Configuration validation and local path resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personal_assistant.config import Config, ConfigError
from personal_assistant.core.paths import PathPolicy
from personal_assistant.errors import PathDeniedError


def test_defaults_are_merged_and_all_markers_are_resolved(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": ["<APP_ROOT>/sandbox"]}}, workspace)

    assert str(workspace / "sandbox") == cfg.get("paths.write_roots")[0]
    assert "<APP_ROOT>" not in str(cfg.data)
    assert "<HOME>" not in str(cfg.data)
    # A default path pulled in by the merge must also be resolved.
    assert cfg.get("paths.database").startswith(str(workspace))
    assert cfg.get("permissions.audit_log").startswith(str(workspace))


def test_dangerous_write_root_is_rejected(workspace: Path) -> None:
    with pytest.raises(ConfigError, match="dangerous"):
        Config.from_dict({"paths": {"write_roots": ["C:\\Windows"]}}, workspace)


def test_sapi_requires_explicit_pretrained_voice_acknowledgement(workspace: Path) -> None:
    with pytest.raises(ConfigError, match="pretrained"):
        Config.from_dict({"speech": {"tts_engine": "sapi"}}, workspace)

    cfg = Config.from_dict(
        {"speech": {"tts_engine": "sapi", "allow_os_pretrained_tts": True}},
        workspace,
    )
    assert cfg.get("speech.tts_engine") == "sapi"


def test_invalid_values_are_rejected(workspace: Path) -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"app": {"auto_approve_max_risk": 9}}, workspace)
    with pytest.raises(ConfigError):
        Config.from_dict({"app": {"offline_mode": "cloud"}}, workspace)
    with pytest.raises(ConfigError):
        Config.from_dict({"speech": {"asr_engine": "whisper"}}, workspace)
    with pytest.raises(ConfigError):
        Config.from_dict({"files": {"max_read_bytes": 10}}, workspace)


def test_save_and_reload_round_trip(workspace: Path) -> None:
    cfg = Config.from_dict(
        {"paths": {"write_roots": [str(workspace / "sandbox")]}, "app": {"max_steps": 17}},
        workspace,
    )
    target = workspace / "config" / "saved.yaml"
    cfg.save(target)

    loaded = Config.load(path=target, root=workspace)
    assert loaded.get("app.max_steps") == 17
    assert loaded.source == target
    assert loaded.get("paths.write_roots") == [str(workspace / "sandbox")]


def test_public_dict_is_a_copy(workspace: Path) -> None:
    cfg = Config.from_dict({}, workspace)
    view = cfg.public_dict()
    view["app"]["max_steps"] = 1
    assert cfg.get("app.max_steps") == 40


def test_path_policy_read_and_write_allowlists(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": [str(workspace / "sandbox")]}}, workspace)
    policy = PathPolicy(cfg)
    sandbox = workspace / "sandbox"
    sandbox.mkdir(exist_ok=True)

    assert policy.check_write(sandbox / "nested" / "file.txt") == sandbox / "nested" / "file.txt"
    assert policy.check_read(workspace / "notes.txt") == workspace / "notes.txt"

    with pytest.raises(PathDeniedError):
        policy.check_write(workspace / "outside.txt")


def test_path_policy_blocks_traversal_and_internal_data(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": [str(workspace / "sandbox")]}}, workspace)
    cfg.ensure_directories()
    policy = PathPolicy(cfg)
    sandbox = workspace / "sandbox"

    with pytest.raises(PathDeniedError):
        policy.check_write(sandbox / ".." / "data" / "assistant.db")
    with pytest.raises(PathDeniedError):
        policy.check_read(workspace / "config" / "config.yaml")
    with pytest.raises(PathDeniedError):
        policy.check_read(workspace / "data" / "assistant.db")


def test_path_policy_blocks_ads_unc_devices_and_sensitive_names(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": [str(workspace / "sandbox")]}}, workspace)
    policy = PathPolicy(cfg)
    sandbox = workspace / "sandbox"
    sandbox.mkdir(exist_ok=True)

    with pytest.raises(PathDeniedError):
        policy.check_write(str(sandbox / "file.txt") + ":hidden")
    with pytest.raises(PathDeniedError):
        policy.check_write(r"\\server\share\file.txt")
    with pytest.raises(PathDeniedError):
        policy.check_write(sandbox / "CON")
    with pytest.raises(PathDeniedError):
        policy.check_write(sandbox / ".env")
    with pytest.raises(PathDeniedError):
        policy.check_write(sandbox / ".ssh" / "key")
    with pytest.raises(PathDeniedError):
        policy.check_read(sandbox / "id_rsa")


def test_path_policy_resolves_symlink_escape(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": [str(workspace / "sandbox")]}}, workspace)
    policy = PathPolicy(cfg)
    sandbox = workspace / "sandbox"
    outside = workspace / "outside"
    sandbox.mkdir(exist_ok=True)
    outside.mkdir(exist_ok=True)
    link = sandbox / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symbolic links is not permitted in this Windows session.")

    with pytest.raises(PathDeniedError):
        policy.check_write(link / "file.txt")
    with pytest.raises(PathDeniedError):
        policy.check_write(link)


def test_relative_paths_resolve_under_app_root(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": [str(workspace / "sandbox")]}}, workspace)
    policy = PathPolicy(cfg)
    path = policy.check_write("sandbox/relative.txt")
    assert path == workspace / "sandbox" / "relative.txt"


def test_frozen_build_seeds_bundled_model_once(workspace: Path, monkeypatch) -> None:
    cfg = Config.from_dict({}, workspace)
    cfg.ensure_directories()
    target = cfg.path("intelligence.intent_model_path")
    assert cfg.seed_bundled_models() == []  # source runs never copy

    executable_dir = workspace / "portable"
    source = executable_dir / "models" / "intent_model.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"model_type": "test"}', encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable_dir / "LocalPersonalAssistant.exe"))

    copied = cfg.seed_bundled_models()
    assert copied == [target]
    assert target.read_text(encoding="utf-8") == '{"model_type": "test"}'

    target.write_text("user-trained", encoding="utf-8")
    assert cfg.seed_bundled_models() == []
    assert target.read_text(encoding="utf-8") == "user-trained"

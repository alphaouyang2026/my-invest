from pathlib import Path

import pytest

from app.core.config import Settings, resolve_jquants_api_key


def test_key_can_be_loaded_from_a_local_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "jquants.key"
    secret_file.write_text("  local-secret\n", encoding="utf-8")

    settings = Settings(jquants_api_key_file=secret_file)

    assert resolve_jquants_api_key(settings) == "local-secret"


def test_environment_key_and_file_are_mutually_exclusive(tmp_path: Path) -> None:
    secret_file = tmp_path / "jquants.key"
    secret_file.write_text("file-secret", encoding="utf-8")

    with pytest.raises(ValueError, match="mutually exclusive"):
        Settings(jquants_api_key="env-secret", jquants_api_key_file=secret_file)

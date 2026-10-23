import configparser
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bdfrx.exceptions import BulkDownloaderException
from bdfrx.oauth2 import OAuth2Authenticator, OAuth2TokenManager


@pytest.fixture()
def example_config() -> configparser.ConfigParser:
    out = configparser.ConfigParser()
    config_dict = {"DEFAULT": {"user_token": "example"}}
    out.read_dict(config_dict)
    return out


@pytest.mark.online
@pytest.mark.parametrize(
    "test_scopes",
    (
        {
            "history",
        },
        {"history", "creddits"},
        {"account", "flair"},
        {
            "*",
        },
    ),
)
def test_check_scopes(test_scopes: set[str]):
    OAuth2Authenticator._check_scopes(test_scopes, "fetch-scopes test")


@pytest.mark.parametrize(
    ("test_scopes", "expected"),
    (
        (
            "history",
            {
                "history",
            },
        ),
        ("history creddits", {"history", "creddits"}),
        ("history, creddits, account", {"history", "creddits", "account"}),
        ("history,creddits,account,flair", {"history", "creddits", "account", "flair"}),
    ),
)
def test_split_scopes(test_scopes: str, expected: set[str]):
    result = OAuth2Authenticator.split_scopes(test_scopes)
    assert result == expected


@pytest.mark.online
@pytest.mark.parametrize(
    "test_scopes",
    (
        {
            "random",
        },
        {"scope", "another_scope"},
    ),
)
def test_check_scopes_bad(test_scopes: set[str]):
    with pytest.raises(BulkDownloaderException):
        OAuth2Authenticator._check_scopes(test_scopes, "fetch-scopes test")


def test_token_manager_read(example_config: configparser.ConfigParser):
    mock_authoriser = MagicMock()
    mock_authoriser.refresh_token = None
    test_manager = OAuth2TokenManager(example_config, MagicMock())
    test_manager.pre_refresh_callback(mock_authoriser)
    assert mock_authoriser.refresh_token == example_config.get("DEFAULT", "user_token")


def test_token_manager_write(example_config: configparser.ConfigParser, tmp_path: Path):
    test_path = tmp_path / "test.cfg"
    mock_authoriser = MagicMock()
    mock_authoriser.refresh_token = "changed_token"
    test_manager = OAuth2TokenManager(example_config, test_path)
    test_manager.post_refresh_callback(mock_authoriser)
    assert example_config.get("DEFAULT", "user_token") == "changed_token"
    with test_path.open() as file:
        file_contents = file.read()
    assert "user_token = changed_token" in file_contents


def test_write_config_is_atomic_and_keeps_the_token(tmp_path: Path):
    from bdfrx.oauth2 import write_config

    location = tmp_path / "default_config.cfg"
    config = configparser.ConfigParser()
    config["DEFAULT"]["client_id"] = "abc"
    config["DEFAULT"]["user_token"] = "token"
    write_config(config, location)
    assert "user_token = token" in location.read_text()
    location.chmod(0o600)

    # Unchanged: not rewritten
    before = location.stat().st_mtime_ns
    write_config(config, location)
    assert location.stat().st_mtime_ns == before

    # Changed: replaced whole, keeping its permissions, with no temporary file left behind
    config["DEFAULT"]["user_token"] = "new"
    write_config(config, location)
    assert "user_token = new" in location.read_text()
    assert location.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["default_config.cfg"]

    # An empty config (e.g. read while another process was writing) never overwrites it
    write_config(configparser.ConfigParser(), location)
    assert "user_token = new" in location.read_text()

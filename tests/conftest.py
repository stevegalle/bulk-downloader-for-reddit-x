import configparser
import platform
from pathlib import Path

import praw
import pytest

from bdfrx.oauth2 import OAuth2TokenManager


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bdfrx's default config folder (~/.config/bdfrx) somewhere temporary, so no test can read or rewrite the
    real config, which holds the Reddit token. Tests that need credentials pass --config tests/test_config.cfg."""
    config_home = tmp_path_factory.mktemp("config_home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return config_home


@pytest.fixture(scope="session")
def reddit_instance():
    return praw.Reddit(
        client_id="0vIJGSRQEHgi_r0esNWvjg",
        client_secret=None,
        user_agent="test",
    )


@pytest.fixture(scope="session")
def authenticated_reddit_instance():
    test_config_path = Path("./tests/test_config.cfg")
    if not test_config_path.exists():
        pytest.skip("Refresh token must be provided to authenticate with OAuth2")
    cfg_parser = configparser.ConfigParser()
    cfg_parser.read(test_config_path)
    if not cfg_parser.has_option("DEFAULT", "user_token"):
        pytest.skip("Refresh token must be provided to authenticate with OAuth2")
    token_manager = OAuth2TokenManager(cfg_parser, test_config_path)
    return praw.Reddit(
        client_id=cfg_parser.get("DEFAULT", "client_id"),
        client_secret=None,
        user_agent=praw.const.USER_AGENT_FORMAT.format(":".join([platform.uname()[0], __package__, "test"])),
        token_manager=token_manager,
    )

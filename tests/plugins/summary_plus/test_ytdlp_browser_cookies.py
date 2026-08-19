import logging
import os
from pathlib import Path

from app.plugins.summary_plus import ytdlp_cookie_service


def test_live_debug_cookies_use_temporary_cookie_file(monkeypatch):
    monkeypatch.setattr(
        ytdlp_cookie_service,
        "_read_debug_browser_cookies",
        lambda _port, _domains: [{
            "domain": ".douyin.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 2_000_000_000,
            "name": "sessionid",
            "value": "secret-value",
        }],
    )

    with ytdlp_cookie_service.ytdlp_browser_cookie_args(
        platform="douyin",
        debug_port=19222,
        user_data_dir="tmp/chrome_data",
        profile_dir="Default",
        logger=logging.getLogger("test.ytdlp.cookies"),
    ) as args:
        assert args[0] == "--cookies"
        cookie_path = args[1]
        assert Path(cookie_path).is_file()
        contents = Path(cookie_path).read_text(encoding="utf-8")
        assert "#HttpOnly_.douyin.com" in contents
        assert "sessionid" in contents
        assert "secret-value" in contents

    assert not os.path.exists(cookie_path)


def test_browser_profile_is_used_when_debug_cookies_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        ytdlp_cookie_service,
        "_read_debug_browser_cookies",
        lambda _port, _domains: [],
    )

    with ytdlp_cookie_service.ytdlp_browser_cookie_args(
        platform="xiaohongshu",
        debug_port=19222,
        user_data_dir="tmp/chrome_data",
        profile_dir="Default",
        logger=logging.getLogger("test.ytdlp.cookies"),
    ) as args:
        assert args == [
            "--cookies-from-browser",
            f"chrome:{os.path.abspath(os.path.join('tmp', 'chrome_data', 'Default'))}",
        ]

import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.plugins.summary_plus.main import SummaryService


@pytest.mark.parametrize("tool_name", ["ffmpeg", "ffprobe"])
def test_resolve_media_tool_uses_virtualenv_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    tool_name: str,
) -> None:
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffprobe_path = tmp_path / "ffprobe.exe"
    ffmpeg_path.touch()
    ffprobe_path.touch()

    fake_run = SimpleNamespace(
        get_or_fetch_platform_executables_else_raise=lambda: (
            str(ffmpeg_path),
            str(ffprobe_path),
        )
    )
    fake_module = ModuleType("static_ffmpeg")
    fake_module.run = fake_run

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.delenv("FFPROBE_PATH", raising=False)
    monkeypatch.setattr(
        "app.plugins.summary_plus.main.shutil.which",
        lambda _: str(tmp_path / "system-ffmpeg.exe"),
    )
    monkeypatch.setitem(sys.modules, "static_ffmpeg", fake_module)

    service = SummaryService.__new__(SummaryService)
    service.logger = logging.getLogger(__name__)

    resolved = service._resolve_media_tool(tool_name, "summary_plus")

    expected = ffmpeg_path if tool_name == "ffmpeg" else ffprobe_path
    assert resolved == str(expected)

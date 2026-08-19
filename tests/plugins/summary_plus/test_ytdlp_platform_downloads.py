import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.plugins.summary_plus.main import SummaryService
from app.plugins.summary_plus.platform_service import _handle_douyin_async


DOUYIN_URL = "https://v.douyin.com/example123/"
XHS_URL = "https://www.xiaohongshu.com/explore/6a41a8c00000000008003151"


def _service_without_init() -> SummaryService:
    service = SummaryService.__new__(SummaryService)
    service.logger = logging.getLogger("test.summary_plus.ytdlp")
    service.ffmpeg_bin = "ffmpeg"
    service.ffprobe_bin = "ffprobe"
    service.yt_dlp_bin = "yt-dlp"
    service.chrome_debug_port = 19222
    service.chrome_user_data_dir = "tmp/chrome_data"
    service.chrome_profile_dir = "Default"
    service.xhs_max_download_duration = 300
    service.xhs_max_images = 9
    return service


class FakeWx:
    def __init__(self):
        self.sent = []

    def send_files(self, chat_name, file_paths):
        self.sent.append((chat_name, file_paths))


def test_douyin_handler_prefers_ytdlp_without_calling_tikhub():
    service = Mock()
    service._download_douyin_with_ytdlp.return_value = "tmp/videos/douyin.mp4"
    wx = FakeWx()

    _handle_douyin_async(service, wx, "测试群", DOUYIN_URL, logging.getLogger("test"))

    service._download_douyin_with_ytdlp.assert_called_once_with(DOUYIN_URL, timeout_sec=180)
    service.parse_douyin_video.assert_not_called()
    assert wx.sent == [("测试群", ["tmp/videos/douyin.mp4"])]


def test_douyin_handler_falls_back_to_tikhub_after_ytdlp_failure():
    service = Mock()
    service._download_douyin_with_ytdlp.return_value = None
    service.parse_douyin_video.return_value = ["https://video.example/douyin.mp4"]
    service._download_video.return_value = "tmp/videos/tikhub.mp4"
    wx = FakeWx()

    _handle_douyin_async(service, wx, "测试群", DOUYIN_URL, logging.getLogger("test"))

    service.parse_douyin_video.assert_called_once_with(DOUYIN_URL)
    service._download_video.assert_called_once_with(["https://video.example/douyin.mp4"])
    assert wx.sent == [("测试群", ["tmp/videos/tikhub.mp4"])]


def test_douyin_ytdlp_download_prefers_h264_format(monkeypatch):
    service = _service_without_init()
    captured = {}

    def fake_run(platform, arguments, timeout_sec):
        captured.update(platform=platform, arguments=arguments, timeout_sec=timeout_sec)
        output_template = arguments[arguments.index("-o") + 1]
        Path(output_template.replace("%(ext)s", "mp4")).write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(service, "_run_platform_ytdlp", fake_run)
    monkeypatch.setattr(service, "_probe_video_codec", lambda _path: "h264")

    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            os.chdir(tmp_dir)
            video_path = service._download_douyin_with_ytdlp(DOUYIN_URL)
        finally:
            os.chdir(original_cwd)

        assert video_path and Path(video_path).is_file()

    assert captured["platform"] == "douyin"
    format_selector = captured["arguments"][captured["arguments"].index("--format") + 1]
    assert format_selector.startswith("b[format_id^=h264_]/b[vcodec=h264]")
    assert captured["arguments"][-1] == DOUYIN_URL


def test_xhs_thumbnail_pairs_are_deduplicated_and_prefer_default_variant():
    service = _service_without_init()
    thumbnails = [
        {"url": "https://img.example/media-a!nd_prv_wlteh_jpg_3"},
        {"url": "https://img.example/media-b!nd_dft_wlteh_jpg_3"},
        {"url": "https://img.example/media-a!nd_dft_wlteh_jpg_3"},
        {"url": "https://img.example/media-b!nd_prv_wlteh_jpg_3"},
    ]

    urls = service._xhs_ytdlp_image_urls({"thumbnails": thumbnails})

    assert urls == [
        "https://img.example/media-a!nd_dft_wlteh_jpg_3",
        "https://img.example/media-b!nd_dft_wlteh_jpg_3",
    ]


def test_xhs_ytdlp_video_enforces_duration_before_download(monkeypatch):
    service = _service_without_init()
    service.xhs_max_download_duration = 30

    @contextmanager
    def fake_cookie_args(**_kwargs):
        yield ["--cookies", "temporary.txt"]

    monkeypatch.setattr(
        "app.plugins.summary_plus.main.ytdlp_browser_cookie_args",
        fake_cookie_args,
    )
    monkeypatch.setattr(
        service,
        "_xhs_ytdlp_info",
        lambda _url, cookie_args=None: {
            "id": "note-id",
            "duration": 31,
            "formats": [{"url": "video"}],
        },
    )
    download = Mock()
    monkeypatch.setattr(service, "_download_xhs_video_with_ytdlp", download)

    handled, path = service._process_xhs_note_with_ytdlp(XHS_URL)

    assert handled is True
    assert path is None
    download.assert_not_called()


def test_xhs_ytdlp_images_are_processed_without_tikhub(monkeypatch):
    service = _service_without_init()

    @contextmanager
    def fake_cookie_args(**_kwargs):
        yield ["--cookies", "temporary.txt"]

    monkeypatch.setattr(
        "app.plugins.summary_plus.main.ytdlp_browser_cookie_args",
        fake_cookie_args,
    )
    monkeypatch.setattr(
        service,
        "_xhs_ytdlp_info",
        lambda _url, cookie_args=None: {
            "id": "note-id",
            "formats": [],
            "thumbnails": [
                {"url": "https://img.example/media-a!nd_prv_wlteh_jpg_3"},
                {"url": "https://img.example/media-b!nd_dft_wlteh_jpg_3"},
                {"url": "https://img.example/media-a!nd_dft_wlteh_jpg_3"},
            ],
        },
    )
    process_images = Mock(return_value="tmp/images/xhs_long.jpg")
    monkeypatch.setattr(service, "_process_xhs_image_urls", process_images)

    handled, path = service._process_xhs_note_with_ytdlp(XHS_URL)

    assert handled is True
    assert path == "tmp/images/xhs_long.jpg"
    process_images.assert_called_once_with(
        [
            "https://img.example/media-a!nd_dft_wlteh_jpg_3",
            "https://img.example/media-b!nd_dft_wlteh_jpg_3",
        ],
        "note-id",
    )


def test_xhs_video_download_reuses_metadata_without_second_extraction(monkeypatch):
    service = _service_without_init()
    captured = {}

    def fake_run(platform, arguments, timeout_sec, cookie_args=None):
        captured.update(
            platform=platform,
            arguments=arguments,
            timeout_sec=timeout_sec,
            cookie_args=cookie_args,
        )
        info_path = arguments[arguments.index("--load-info-json") + 1]
        captured["info_path"] = info_path
        assert Path(info_path).is_file()
        output_template = arguments[arguments.index("-o") + 1]
        Path(output_template.replace("%(ext)s", "mp4")).write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(service, "_run_platform_ytdlp", fake_run)
    monkeypatch.setattr(service, "_probe_video_codec", lambda _path: "h264")

    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            os.chdir(tmp_dir)
            video_path = service._download_xhs_video_with_ytdlp(
                XHS_URL,
                "note-id",
                info={"id": "note-id", "formats": [{"url": "https://video.example/test.mp4"}]},
                cookie_args=["--cookies", "temporary.txt"],
            )
        finally:
            os.chdir(original_cwd)

        assert video_path and Path(video_path).is_file()

    assert captured["platform"] == "xiaohongshu"
    assert captured["cookie_args"] == ["--cookies", "temporary.txt"]
    assert "--load-info-json" in captured["arguments"]
    assert XHS_URL not in captured["arguments"]
    assert not os.path.exists(captured["info_path"])


def test_process_xhs_note_does_not_call_tikhub_after_ytdlp_success(monkeypatch):
    service = _service_without_init()
    monkeypatch.setattr(
        service,
        "_process_xhs_note_with_ytdlp",
        lambda _url: (True, "tmp/images/xhs_long.jpg"),
    )
    tikhub = Mock()
    monkeypatch.setattr(service, "_xhs_fetch_note_response", tikhub)

    assert service.process_xhs_note(XHS_URL) == "tmp/images/xhs_long.jpg"
    tikhub.assert_not_called()


def test_process_xhs_note_falls_back_to_tikhub_after_ytdlp_failure(monkeypatch):
    service = _service_without_init()
    monkeypatch.setenv("TIKHUB_API_TOKEN", "token")
    monkeypatch.setattr(service, "_process_xhs_note_with_ytdlp", lambda _url: (False, None))
    tikhub = Mock(return_value={"data": "payload"})
    monkeypatch.setattr(service, "_xhs_fetch_note_response", tikhub)
    note = {
        "id": "note-id",
        "type": "normal",
        "images_list": [{"url": "https://img.example/image.webp"}],
    }
    monkeypatch.setattr(service, "_xhs_extract_notes_from_response", lambda _payload: [note])
    monkeypatch.setattr(service, "_xhs_select_target_notes", lambda notes, _target: notes)
    monkeypatch.setattr(service, "_xhs_download_file", lambda _url, path: Path(path).write_bytes(b"image"))
    monkeypatch.setattr(
        service,
        "_xhs_convert_to_jpg",
        lambda _source, target: Path(target).write_bytes(b"jpeg"),
    )

    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            os.chdir(tmp_dir)
            result = service.process_xhs_note(XHS_URL)
        finally:
            os.chdir(original_cwd)

        assert result and Path(result).read_bytes() == b"jpeg"
    tikhub.assert_called_once_with("token", XHS_URL)

import hashlib

from PIL import Image

from app.utils.wechat_media import image_file_fingerprint, is_probable_wechat_media_window


def classify(**overrides):
    values = {
        "class_name": "mmui::ImagePreviewWindow",
        "name": "图片预览",
        "handle": 20,
        "process_id": 100,
        "expected_process_id": 100,
        "baseline_handles": {10},
        "listener_names": {"污合之众"},
    }
    values.update(overrides)
    return is_probable_wechat_media_window(**values)


def test_explicit_media_preview_is_classified():
    assert classify()


def test_main_and_listener_chat_windows_are_never_classified():
    assert not classify(class_name="mmui::MainWindow", name="微信")
    assert not classify(class_name="mmui::ChatSingleWindow", name="污合之众")
    assert not classify(
        class_name="mmui::FramelessMainWindow",
        name="污合之众",
    )


def test_preexisting_generic_wechat_window_is_not_closed_blindly():
    assert not classify(
        class_name="mmui::UnknownWindow",
        name="",
        handle=20,
        baseline_handles={20},
    )


def test_new_generic_same_process_wechat_popup_is_classified():
    assert classify(
        class_name="mmui::UnknownWindow",
        name="",
        handle=20,
        baseline_handles={10},
    )


def test_other_process_or_embedded_browser_is_not_classified():
    assert not classify(
        class_name="mmui::UnknownWindow",
        name="",
        process_id=200,
    )
    assert not classify(class_name="Chrome_WidgetWin_0", name="")


def test_explicit_media_window_in_wechat_helper_process_is_classified():
    assert classify(process_id=200)


def test_image_fingerprint_records_exact_bytes_and_dimensions(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (7, 5), (12, 34, 56)).save(path)
    payload = path.read_bytes()

    result = image_file_fingerprint(path)

    assert result["bytes"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["width"] == 7
    assert result["height"] == 5
    assert result["format"] == "PNG"

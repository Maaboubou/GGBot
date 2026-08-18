import base64

from app.plugins.builtin_chatbot.main import ChatBotPlugin


class FakeWxManager:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def download_quote_image(self, chat_name, message_id):
        self.calls.append((chat_name, message_id))
        return self.result


def make_plugin():
    return ChatBotPlugin.__new__(ChatBotPlugin)


def test_quote_download_failure_never_uses_recent_unrelated_image(tmp_path):
    unrelated = tmp_path / "unrelated.png"
    unrelated.write_bytes(b"this must never be sent")
    plugin = make_plugin()
    # Keep the former cache shape on the object to prove that even an old
    # runtime/test fixture cannot make the exact quote path fall back to it.
    plugin._last_images = {
        "group": {"path": str(unrelated), "time": 9999999999},
    }
    manager = FakeWxManager(result=None)

    result = plugin._process_quoted_image(
        {
            "chat_name": "group",
            "message_id": "quote-1",
            "has_quote_image": True,
        },
        manager,
    )

    assert result is None
    assert manager.calls == [("group", "quote-1")]


def test_quote_uses_only_path_returned_for_current_message(tmp_path):
    exact = tmp_path / "exact.png"
    exact_bytes = b"exact quoted image bytes"
    exact.write_bytes(exact_bytes)
    manager = FakeWxManager(result=str(exact))

    result = make_plugin()._process_quoted_image(
        {
            "chat_name": "group",
            "message_id": "quote-2",
            "has_quote_image": True,
        },
        manager,
    )

    assert base64.b64decode(result) == exact_bytes
    assert manager.calls == [("group", "quote-2")]


def test_existing_exact_quote_path_does_not_trigger_another_download(tmp_path):
    exact = tmp_path / "exact.png"
    exact_bytes = b"already downloaded exact image"
    exact.write_bytes(exact_bytes)
    manager = FakeWxManager(result=None)

    result = make_plugin()._process_quoted_image(
        {
            "chat_name": "group",
            "message_id": "quote-3",
            "has_quote_image": True,
            "quote_image_path": str(exact),
        },
        manager,
    )

    assert base64.b64decode(result) == exact_bytes
    assert manager.calls == []

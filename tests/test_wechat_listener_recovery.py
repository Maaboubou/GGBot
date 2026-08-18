from types import SimpleNamespace

from app.utils.wechat_listener_recovery import (
    find_exact_session,
    open_listener_from_existing_session,
    session_name,
)


class FakeSession:
    def __init__(self, name, successful_option="独立窗口显示"):
        self.name = name
        self.successful_option = successful_option
        self.selected_options = []

    def select_option(self, option):
        self.selected_options.append(option)
        return option == self.successful_option


class FakeWeChat:
    def __init__(self, sessions):
        self.sessions = sessions
        self.get_session_calls = 0

    def GetSession(self):
        self.get_session_calls += 1
        return list(self.sessions)


def test_session_name_is_defensive():
    assert session_name(SimpleNamespace(name=" 测试群 ")) == "测试群"
    assert session_name(object()) == ""


def test_find_exact_session_never_uses_partial_match():
    partial = FakeSession("污合之众二群")
    exact = FakeSession("污合之众")

    assert find_exact_session([partial, exact], "污合之众") is exact
    assert find_exact_session([partial], "污合之众") is None


def test_existing_session_is_opened_and_verified():
    session = FakeSession("污合之众")
    wechat = FakeWeChat([session])
    listener_chat = object()
    probes = iter([None, None, listener_chat])
    sleeps = []

    result, route = open_listener_from_existing_session(
        wechat,
        "污合之众",
        get_listener_chat=lambda _who: next(probes),
        verify_attempts=3,
        verify_delay=0.1,
        sleeper=sleeps.append,
    )

    assert result is listener_chat
    assert route == "session_menu:独立窗口显示"
    assert session.selected_options == ["独立窗口显示"]
    assert sleeps == [0.1, 0.1]


def test_missing_session_leaves_native_search_as_fallback():
    session = FakeSession("其他群")
    result, route = open_listener_from_existing_session(
        FakeWeChat([session]),
        "污合之众",
        get_listener_chat=lambda _who: None,
        sleeper=lambda _delay: None,
    )

    assert result is None
    assert route == "session_not_found"
    assert session.selected_options == []


def test_uncreated_window_is_reported_after_bounded_verification():
    session = FakeSession("污合之众")
    sleeps = []
    result, route = open_listener_from_existing_session(
        FakeWeChat([session]),
        "污合之众",
        get_listener_chat=lambda _who: None,
        verify_attempts=2,
        verify_delay=0.2,
        sleeper=sleeps.append,
    )

    assert result is None
    assert route == "window_not_created"
    assert sleeps == [0.2, 0.2]


def test_legacy_detach_menu_wording_is_supported():
    session = FakeSession("污合之众", successful_option="在独立窗口打开")
    chat = object()

    result, route = open_listener_from_existing_session(
        FakeWeChat([session]),
        "污合之众",
        get_listener_chat=lambda _who: chat,
        sleeper=lambda _delay: None,
    )

    assert result is chat
    assert route == "session_menu:在独立窗口打开"
    assert session.selected_options == ["独立窗口显示", "在独立窗口打开"]

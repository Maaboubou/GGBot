import asyncio
from unittest.mock import Mock

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.endpoints.wechat import ListenChatRequest, add_listen_chat, remove_listen_chat
from app.core.wechat_manager import WeChatManager
from app.main import _ensure_wechat_user_listener_preference_column, sync_all_listeners
from app.models.base import Base
from app.models.user_permission import WeChatUser


class FakePluginManager:
    def get_all_plugin_names(self):
        return ["builtin_chatbot"]


class FakeWeChatManager:
    def __init__(self, *, remove_success=True, desired=None, actual=None):
        self.added = []
        self.removed = []
        self.remove_success = remove_success
        self.desired = list(desired or [])
        self.actual = list(actual or [])

    def is_connected_cached(self):
        return True

    def get_listener_status(self):
        return {
            "status": "success",
            "desired": self.desired,
            "actual": self.actual,
            "missing": [],
            "probe_skipped": False,
        }

    def add_listen_chat(self, chat_name, exact=False):
        self.added.append((chat_name, exact))
        return True

    def remove_listen_chat(self, chat_name):
        self.removed.append(chat_name)
        return self.remove_success


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_listener_preference_migration_defaults_existing_users_to_enabled():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE wechat_users ("
                "id INTEGER PRIMARY KEY, chat_name VARCHAR NOT NULL UNIQUE"
                ")"
            )
        )
        connection.execute(text("INSERT INTO wechat_users (chat_name) VALUES ('旧群')"))

    db = sessionmaker(bind=engine)()
    try:
        _ensure_wechat_user_listener_preference_column(db)
        row = db.execute(
            text("SELECT listening_enabled FROM wechat_users WHERE chat_name = '旧群'")
        ).one()
        assert row[0] == 1
    finally:
        db.close()
        engine.dispose()


def test_reconnect_sync_skips_manually_paused_chat():
    engine, db = make_session()
    manager = FakeWeChatManager(
        desired=["手动暂停"],
        actual=["手动暂停"],
    )
    try:
        db.add_all(
            [
                WeChatUser(chat_name="继续监听", listening_enabled=True),
                WeChatUser(chat_name="手动暂停", listening_enabled=False),
            ]
        )
        db.commit()

        sync_all_listeners(db, manager, FakePluginManager())

        assert manager.added == [("继续监听", False)]
        assert manager.removed == ["手动暂停"]
    finally:
        db.close()
        engine.dispose()


def test_manual_stop_is_persisted_even_when_runtime_remove_fails():
    engine, db = make_session()
    manager = FakeWeChatManager(remove_success=False)
    try:
        db.add(WeChatUser(chat_name="测试群", listening_enabled=True))
        db.commit()

        result = asyncio.run(remove_listen_chat("测试群", manager, db))

        db.expire_all()
        user = db.query(WeChatUser).filter(WeChatUser.chat_name == "测试群").one()
        assert result["success"] is True
        assert result["runtime_success"] is False
        assert user.listening_enabled is False

        resumed = asyncio.run(add_listen_chat(ListenChatRequest(chat_name="测试群"), manager, db))
        db.expire_all()
        assert resumed["success"] is True
        assert user.listening_enabled is True
        assert manager.added == [("测试群", False)]
    finally:
        db.close()
        engine.dispose()


def test_runtime_manager_forgets_local_listener_when_bridge_is_unreachable(monkeypatch):
    manager = WeChatManager(event_bus=object())
    manager._listened_chats["测试群"] = {"added_time": 1, "message_count": 0}
    manager._stats["listened_chats_count"] = 1
    monkeypatch.setattr(
        requests,
        "post",
        Mock(side_effect=requests.ConnectionError("bridge unavailable")),
    )

    assert manager.remove_listen_chat("测试群") is False
    assert manager._listened_chats == {}
    assert manager._stats["listened_chats_count"] == 0

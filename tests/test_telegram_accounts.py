import asyncio
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon.tl.types import Chat, ChatPhotoEmpty

import app.collector as collector_module
import app.telegram_client as telegram_client
from app.config import Settings
from app.models import Base, MonitoredChat


def test_session_paths_are_resolved_and_deduplicated():
    configured = Settings(
        _env_file=None,
        telegram_session_path='./data/legacy.session',
        telegram_session_paths='./data/one.session, ./data/two.session, ./data/one.session',
    )

    assert [path.name for path in configured.resolved_session_paths] == ['one.session', 'two.session']


def test_account_lookup_accepts_index_and_stable_key(monkeypatch):
    managers = [
        SimpleNamespace(account_key='/sessions/one.session', label='one.session'),
        SimpleNamespace(account_key='/sessions/two.session', label='two.session'),
    ]
    monkeypatch.setattr(telegram_client, 'telegram_session_managers', managers)

    assert telegram_client.get_telegram_session_manager(1) is managers[1]
    assert telegram_client.get_telegram_session_manager('/sessions/one.session') is managers[0]
    assert telegram_client.get_telegram_session_manager('/moved/one.session') is managers[0]
    with pytest.raises(ValueError):
        telegram_client.get_telegram_session_manager(-1)


def test_dialog_sync_routes_two_accounts_and_deduplicates_shared_chats(monkeypatch):
    async def run():
        engine = create_engine('sqlite:///:memory:', future=True)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(engine)

        @contextmanager
        def fake_session_scope():
            db = session_factory()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        def chat(chat_id, title):
            return Chat(chat_id, title, ChatPhotoEmpty(), 1, datetime.utcnow(), 1)

        class Client:
            def __init__(self, entities):
                self.entities = entities

            async def iter_dialogs(self):
                for entity in self.entities:
                    yield SimpleNamespace(entity=entity)

        class Manager:
            def __init__(self, key, entities):
                self.account_key = key
                self.label = key
                self.group_count = None
                self.client = Client(entities)

            async def connect(self):
                return self.client

        shared = chat(99, 'shared')
        first = Manager('account-1', [chat(1, 'one'), shared])
        second = Manager('account-2', [chat(2, 'two'), shared])
        monkeypatch.setattr(collector_module, 'session_scope', fake_session_scope)
        monkeypatch.setattr(collector_module, 'telegram_session_managers', [first, second])

        collector = collector_module.TelegramCollector()
        inserted = await collector.sync_dialogs()

        with fake_session_scope() as db:
            rows = db.query(MonitoredChat).all()
            by_title = {row.title: row.telegram_account for row in rows}
        assert inserted == 3
        assert by_title == {'one': 'account-1', 'shared': 'account-1', 'two': 'account-2'}
        assert first.group_count == second.group_count == 2
        second.group_count = 1
        selected, _ = await collector._least_loaded_account()
        assert selected is second

    asyncio.run(run())


def test_handlers_are_registered_per_account_and_replaced_after_reconnect():
    async def run():
        class Client:
            def __init__(self):
                self.handlers = []

            def on(self, event):
                def register(handler):
                    self.handlers.append(handler)
                    return handler
                return register

            def remove_event_handler(self, handler):
                self.handlers.remove(handler)

        collector = collector_module.TelegramCollector()
        first_manager = SimpleNamespace(account_key='one', label='one')
        second_manager = SimpleNamespace(account_key='two', label='two')
        first_client = Client()
        replacement_client = Client()
        second_client = Client()

        await collector._register_handler(first_manager, first_client)
        await collector._register_handler(second_manager, second_client)
        await collector._register_handler(first_manager, replacement_client)

        assert set(collector._message_handlers) == {'one', 'two'}
        assert first_client.handlers == []
        assert len(replacement_client.handlers) == len(second_client.handlers) == 1

    asyncio.run(run())

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telethon import events, utils
from telethon.tl.types import User, Channel, Chat
from sqlalchemy import select

from .db import session_scope
from .models import MonitoredChat, TelegramUser, Message, MessageKeyword, SyncRun, AppSetting
from .telegram_client import telegram_session_manager
from .text_utils import normalize_text, extract_keywords
from .config import settings

logger = logging.getLogger(__name__)


class TelegramCollector:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone='UTC')
        self.started = False
        self.handler_registered = False

    async def start(self) -> None:
        if self.started:
            return
        client = await telegram_session_manager.connect()
        if client:
            await self._register_handler(client)
        self.scheduler.add_job(self.ensure_connected, 'interval', minutes=1, id='ensure_connected', replace_existing=True)
        self.scheduler.add_job(self.backfill_all_active_chats, 'interval', minutes=5, id='backfill_all', replace_existing=True)
        self.scheduler.start()
        self.started = True

    async def ensure_connected(self) -> None:
        client = await telegram_session_manager.connect()
        if client and not self.handler_registered:
            await self._register_handler(client)

    async def _register_handler(self, client) -> None:
        if self.handler_registered:
            return

        @client.on(events.NewMessage())
        async def new_message_handler(event):
            chat_id = event.chat_id
            if chat_id is None:
                return
            with session_scope() as db:
                chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == chat_id, MonitoredChat.is_active.is_(True))).scalar_one_or_none()
                if not chat:
                    return
            await self.persist_message(event.message, event.chat, event.sender)

        self.handler_registered = True
        logger.info('telegram new-message handler registered')

    async def sync_dialogs(self) -> int:
        client = await telegram_session_manager.connect()
        if not client:
            raise RuntimeError('telegram session unavailable')
        count = 0
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            with session_scope() as db:
                peer_id = utils.get_peer_id(entity)
                chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == peer_id)).scalar_one_or_none()
                if not chat:
                    chat = MonitoredChat(
                        telegram_id=peer_id,
                        access_hash=getattr(entity, 'access_hash', None),
                        title=getattr(entity, 'title', getattr(entity, 'username', str(entity.id))),
                        username=getattr(entity, 'username', None),
                        chat_type=entity.__class__.__name__.lower(),
                        is_active=False,
                    )
                    db.add(chat)
                    count += 1
                else:
                    chat.title = getattr(entity, 'title', getattr(entity, 'username', chat.title))
                    chat.username = getattr(entity, 'username', chat.username)
                    chat.access_hash = getattr(entity, 'access_hash', chat.access_hash)
                    chat.chat_type = entity.__class__.__name__.lower()
        return count

    async def backfill_all_active_chats(self) -> None:
        client = await telegram_session_manager.connect()
        if not client:
            return
        with session_scope() as db:
            chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
        for chat in chats:
            try:
                await self.backfill_chat(chat.telegram_id, settings.sync_lookback_messages)
            except Exception as exc:
                logger.warning('backfill chat %s failed: %s', chat.telegram_id, exc)

    async def backfill_chat(self, telegram_chat_id: int, limit: int = 1000) -> int:
        client = await telegram_session_manager.connect()
        if not client:
            raise RuntimeError('telegram session unavailable')
        total = 0
        with session_scope() as db:
            chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == telegram_chat_id)).scalar_one_or_none()
            if not chat:
                raise RuntimeError('chat not found in monitored list')
            run = SyncRun(chat_id=chat.id, run_type='backfill', status='running', started_at=datetime.utcnow())
            db.add(run)

        entity = await client.get_entity(telegram_chat_id)
        async for message in client.iter_messages(entity, limit=limit):
            sender = await message.get_sender()
            await self.persist_message(message, entity, sender)
            total += 1

        with session_scope() as db:
            chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == telegram_chat_id)).scalar_one()
            run = db.execute(select(SyncRun).where(SyncRun.chat_id == chat.id).order_by(SyncRun.id.desc())).scalars().first()
            if run:
                run.status = 'success'
                run.message = f'backfilled {total} messages'
                run.finished_at = datetime.utcnow()
        return total

    async def _download_media(self, tg_message, chat_telegram_id: int) -> dict | None:
        if getattr(tg_message, 'media', None) is None:
            return None

        media_root = settings.resolved_media_storage_path / str(chat_telegram_id)
        media_root.mkdir(parents=True, exist_ok=True)
        stem = media_root / f'{tg_message.id}'

        try:
            downloaded = await tg_message.download_media(file=str(stem))
        except Exception as exc:
            logger.warning('download media failed chat=%s message=%s err=%s', chat_telegram_id, tg_message.id, exc)
            return None

        if not downloaded:
            return None

        downloaded_path = Path(downloaded)
        relative_path = downloaded_path.relative_to(settings.resolved_media_storage_path)
        suffix = downloaded_path.suffix.lower()
        return {
            'media_path': relative_path.as_posix(),
            'media_name': downloaded_path.name,
            'media_is_image': suffix in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'},
            'media_is_video': suffix in {'.mp4', '.mov', '.m4v', '.webm'},
        }

    async def persist_message(self, tg_message, tg_chat, tg_sender) -> None:
        if tg_chat is not None:
            chat_telegram_id = utils.get_peer_id(tg_chat)
        else:
            chat_telegram_id = getattr(tg_message, 'chat_id', None)
        if chat_telegram_id is None:
            return
        with session_scope() as db:
            chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == chat_telegram_id)).scalar_one_or_none()
            if not chat:
                chat = MonitoredChat(
                    telegram_id=chat_telegram_id,
                    access_hash=getattr(tg_chat, 'access_hash', None),
                    title=getattr(tg_chat, 'title', getattr(tg_chat, 'username', str(chat_telegram_id))),
                    username=getattr(tg_chat, 'username', None),
                    chat_type=tg_chat.__class__.__name__.lower(),
                    is_active=False,
                )
                db.add(chat)
                db.flush()

            sender = None
            if isinstance(tg_sender, User):
                sender = db.execute(select(TelegramUser).where(TelegramUser.telegram_id == tg_sender.id)).scalar_one_or_none()
                if not sender:
                    sender = TelegramUser(
                        telegram_id=tg_sender.id,
                        username=getattr(tg_sender, 'username', None),
                        first_name=getattr(tg_sender, 'first_name', None),
                        last_name=getattr(tg_sender, 'last_name', None),
                        is_bot=getattr(tg_sender, 'bot', False),
                    )
                    db.add(sender)
                    db.flush()
                else:
                    sender.username = getattr(tg_sender, 'username', sender.username)
                    sender.first_name = getattr(tg_sender, 'first_name', sender.first_name)
                    sender.last_name = getattr(tg_sender, 'last_name', sender.last_name)
                    sender.is_bot = getattr(tg_sender, 'bot', sender.is_bot)

            existing = db.execute(
                select(Message).where(Message.chat_id == chat.id, Message.telegram_message_id == tg_message.id)
            ).scalar_one_or_none()
            normalized = normalize_text(getattr(tg_message, 'message', '') or getattr(tg_message, 'text', ''))
            media_meta = await self._download_media(tg_message, chat_telegram_id)
            meta_json = {
                'grouped_id': getattr(tg_message, 'grouped_id', None),
                'post_author': getattr(tg_message, 'post_author', None),
                'via_bot_id': getattr(tg_message, 'via_bot_id', None),
            }
            if media_meta:
                meta_json.update(media_meta)
            if not existing:
                existing = Message(
                    chat_id=chat.id,
                    sender_user_id=sender.id if sender else None,
                    telegram_message_id=tg_message.id,
                    message_date=tg_message.date.replace(tzinfo=None) if tg_message.date else datetime.utcnow(),
                    edit_date=tg_message.edit_date.replace(tzinfo=None) if tg_message.edit_date else None,
                    raw_text=getattr(tg_message, 'message', '') or getattr(tg_message, 'text', ''),
                    normalized_text=normalized,
                    reply_to_msg_id=getattr(getattr(tg_message, 'reply_to', None), 'reply_to_msg_id', None),
                    views=getattr(tg_message, 'views', None),
                    forwards=getattr(tg_message, 'forwards', None),
                    has_media=getattr(tg_message, 'media', None) is not None,
                    media_type=tg_message.media.__class__.__name__ if getattr(tg_message, 'media', None) is not None else None,
                    meta_json=meta_json,
                )
                db.add(existing)
                db.flush()
            else:
                existing.sender_user_id = sender.id if sender else existing.sender_user_id
                existing.raw_text = getattr(tg_message, 'message', '') or getattr(tg_message, 'text', '')
                existing.normalized_text = normalized
                existing.edit_date = tg_message.edit_date.replace(tzinfo=None) if tg_message.edit_date else existing.edit_date
                existing.views = getattr(tg_message, 'views', existing.views)
                existing.forwards = getattr(tg_message, 'forwards', existing.forwards)
                existing.has_media = getattr(tg_message, 'media', None) is not None
                existing.media_type = tg_message.media.__class__.__name__ if getattr(tg_message, 'media', None) is not None else None
                existing.meta_json = meta_json
                for kw in list(existing.keywords):
                    db.delete(kw)
                db.flush()

            chat.last_message_at = existing.message_date
            chat.last_synced_message_id = max(existing.telegram_message_id, chat.last_synced_message_id or 0)
            chat.title = getattr(tg_chat, 'title', chat.title)
            chat.username = getattr(tg_chat, 'username', chat.username)
            chat.access_hash = getattr(tg_chat, 'access_hash', chat.access_hash)
            for keyword, weight in extract_keywords(normalized):
                db.add(MessageKeyword(message_id=existing.id, keyword=keyword[:100], weight=weight))


collector = TelegramCollector()

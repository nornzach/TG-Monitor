from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from tzlocal import get_localzone

from telethon import events, utils
from telethon.errors import RPCError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import User, Channel, Chat
from sqlalchemy import select, func, or_
from sqlalchemy.dialects.mysql import insert as mysql_insert

from . import analysis_advanced
from .ai_service import extract_urls_from_text, run_key_lead_analysis_once, run_summary_for_chat, run_url_classification_once, upsert_discovered_urls
from .alerts import check_message_alerts
from .content_fingerprint import save_fingerprint
from .db import session_scope
from .join_targets import discover_join_targets_from_collected_data, normalize_join_target, sync_join_targets_with_monitored_chats
from .market_brief import generate_daily_brief
from .dashboard import refresh_keyword_summary
from .models import (
    MonitoredChat, TelegramJoinTarget, TelegramUser, Message, MessageKeyword,
    SyncRun, AppSetting, AiSummary, MessageEdit, MessageReaction,
    MessageViewsHistory, UserDailyStat, DailyChatStat,
)
from .telegram_client import get_telegram_session_manager, telegram_session_manager, telegram_session_managers
from .text_utils import normalize_text, extract_keywords
from .config import settings

logger = logging.getLogger(__name__)

# The scheduler runs in UTC, but the summarising window is wall-clock time in
# the user's own timezone (18:01 means 18:01 locally).
LOCAL_TIMEZONE = get_localzone()


def in_active_summary_window(now: datetime | None = None) -> bool:
    """True when AI summaries are allowed right now.

    The window may wrap past midnight (18:01 -> 08:30). Disabled mode always
    returns True so the plain batch-threshold behaviour applies.
    """
    if not settings.ai_summary_active_hours_enabled:
        return True

    current = (now or datetime.now()).time().replace(second=0, microsecond=0)
    start_hour, start_minute = settings.ai_summary_active_start_hm
    end_hour, end_minute = settings.ai_summary_active_end_hm
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)

    if start <= end:
        return start <= current < end
    return current >= start or current < end


class TelegramCollector:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone='UTC')
        self.started = False
        self._message_handlers: dict[str, tuple[object, object]] = {}
        self._handler_lock = asyncio.Lock()
        self._backfill_semaphore = asyncio.Semaphore(1)
        self._backfill_locks: dict[int, asyncio.Lock] = {}
        self._persist_locks: dict[int, asyncio.Lock] = {}
        self._summary_locks: dict[int, asyncio.Lock] = {}
        self._summary_queue_lock = asyncio.Lock()
        self._join_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.started:
            return

        def _refresh_keyword_summary_job():
            try:
                with session_scope() as db:
                    count = refresh_keyword_summary(db)
                    logger.info('keyword_summary refreshed: %d rows', count)
            except Exception as exc:
                logger.exception('keyword_summary refresh failed: %s', exc)

        self.scheduler.add_job(
            _refresh_keyword_summary_job,
            'interval',
            hours=1,
            id='refresh_keyword_summary',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        if settings.telegram_background_collection_enabled:
            if settings.telegram_live_listener_enabled:
                await self.ensure_connected()
            self.scheduler.add_job(self.ensure_connected, 'interval', minutes=1, id='ensure_connected', replace_existing=True)
            self.scheduler.add_job(
                self.backfill_all_active_chats,
                'interval',
                minutes=settings.sync_interval_minutes,
                id='backfill_all',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        else:
            logger.info('telegram background collection disabled; startup will not connect to Telegram')

        if settings.url_classification_enabled:
            self.scheduler.add_job(
                run_url_classification_once,
                'interval',
                minutes=settings.url_classification_interval_minutes,
                id='url_classification',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        if settings.key_lead_analysis_enabled:
            self.scheduler.add_job(
                run_key_lead_analysis_once,
                'interval',
                minutes=settings.key_lead_analysis_interval_minutes,
                id='key_lead_analysis',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        if settings.telegram_join_queue_enabled:
            self.scheduler.add_job(
                self.run_join_queue_once,
                'interval',
                minutes=settings.telegram_join_interval_minutes,
                id='telegram_join_queue',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        # Advanced analytics jobs
        self.scheduler.add_job(
            self.run_daily_analytics,
            'interval',
            hours=1,
            id='daily_analytics',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.run_anomaly_detection,
            'interval',
            hours=1,
            id='anomaly_detection',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.run_daily_brief,
            'cron',
            hour=1,
            minute=30,
            id='daily_brief',
            replace_existing=True,
            max_instances=1,
        )

        self._configure_summary_window()

        if self.scheduler.get_jobs():
            self.scheduler.start()
        self.started = True

    async def stop(self) -> None:
        self.started = False
        await self._unregister_handler()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        for manager in telegram_session_managers:
            await manager.disconnect()

    async def ensure_connected(self) -> None:
        if not settings.telegram_live_listener_enabled:
            return
        for manager in telegram_session_managers:
            try:
                client = await manager.connect()
            except sqlite3.OperationalError as exc:
                if 'database is locked' not in str(exc):
                    logger.warning('ensure_connected failed account=%s: %s', manager.label, exc)
                    continue
                logger.warning('ensure_connected session sqlite lock account=%s: %s', manager.label, exc)
                await asyncio.sleep(2)
                try:
                    client = await manager.connect()
                except Exception as retry_exc:
                    logger.warning('ensure_connected retry failed account=%s: %s', manager.label, retry_exc)
                    continue
            except Exception as exc:
                logger.warning('ensure_connected failed account=%s: %s', manager.label, exc)
                continue
            registered = self._message_handlers.get(manager.account_key)
            if client and (not registered or registered[0] is not client):
                await self._register_handler(manager, client)

    async def _register_handler(self, manager, client) -> None:
        async with self._handler_lock:
            registered = self._message_handlers.pop(manager.account_key, None)
            if registered:
                try:
                    registered[0].remove_event_handler(registered[1])
                except Exception:
                    logger.warning('failed to replace Telegram handler account=%s', manager.label, exc_info=True)

            @client.on(events.NewMessage())
            async def new_message_handler(event):
                chat_id = event.chat_id
                if chat_id is None:
                    return
                with session_scope() as db:
                    chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == chat_id, MonitoredChat.is_active.is_(True))).scalar_one_or_none()
                    if not chat:
                        return
                try:
                    await self.persist_message(event.message, event.chat, event.sender, client=client)
                except Exception as exc:
                    logger.warning('new_message_handler failed: %s', exc)

            self._message_handlers[manager.account_key] = (client, new_message_handler)
            logger.info('telegram new-message handler registered account=%s', manager.label)

    async def _unregister_handler(self) -> None:
        async with self._handler_lock:
            for account_key, (client, handler) in list(self._message_handlers.items()):
                try:
                    client.remove_event_handler(handler)
                except Exception:
                    logger.warning('telegram new-message handler unregister failed account=%s', account_key, exc_info=True)
            self._message_handlers.clear()
            logger.info('telegram new-message handlers unregistered')

    async def apply_runtime_config(self) -> None:
        if not settings.telegram_background_collection_enabled:
            await self._unregister_handler()
            if self.scheduler.running:
                for job_id in ('ensure_connected', 'backfill_all'):
                    try:
                        self.scheduler.remove_job(job_id)
                    except Exception:
                        pass
        else:
            if self.scheduler.running:
                self.scheduler.add_job(
                    self.backfill_all_active_chats,
                    'interval',
                    minutes=settings.sync_interval_minutes,
                    id='backfill_all',
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
            else:
                self.scheduler.add_job(self.ensure_connected, 'interval', minutes=1, id='ensure_connected', replace_existing=True)
                self.scheduler.add_job(
                    self.backfill_all_active_chats,
                    'interval',
                    minutes=settings.sync_interval_minutes,
                    id='backfill_all',
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )

            if settings.telegram_live_listener_enabled:
                await self.ensure_connected()
            else:
                await self._unregister_handler()

        if settings.url_classification_enabled:
            self.scheduler.add_job(
                run_url_classification_once,
                'interval',
                minutes=settings.url_classification_interval_minutes,
                id='url_classification',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        else:
            if self.scheduler.running:
                try:
                    self.scheduler.remove_job('url_classification')
                except Exception:
                    pass

        if settings.key_lead_analysis_enabled:
            self.scheduler.add_job(
                run_key_lead_analysis_once,
                'interval',
                minutes=settings.key_lead_analysis_interval_minutes,
                id='key_lead_analysis',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        else:
            if self.scheduler.running:
                try:
                    self.scheduler.remove_job('key_lead_analysis')
                except Exception:
                    pass

        if settings.telegram_join_queue_enabled:
            self.scheduler.add_job(
                self.run_join_queue_once,
                'interval',
                minutes=settings.telegram_join_interval_minutes,
                id='telegram_join_queue',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        else:
            if self.scheduler.running:
                try:
                    self.scheduler.remove_job('telegram_join_queue')
                except Exception:
                    pass

        self._configure_summary_window()

        if not self.scheduler.running and self.scheduler.get_jobs():
            self.scheduler.start()

    async def run_daily_analytics(self) -> dict:
        """Compute daily chat/user stats."""
        try:
            with session_scope() as db:
                chat_count = analysis_advanced.compute_daily_chat_stats(db)
                user_count = analysis_advanced.compute_user_daily_aggregates(db)
            logger.info('daily analytics computed chat_stats=%s user_stats=%s', chat_count, user_count)
            return {'status': 'success', 'chat_stats': chat_count, 'user_stats': user_count}
        except Exception as exc:
            logger.exception('daily analytics failed')
            return {'status': 'failed', 'reason': str(exc)}

    async def run_anomaly_detection(self) -> dict:
        """Detect chat anomalies and persist system events."""
        try:
            with session_scope() as db:
                events = analysis_advanced.detect_chat_anomalies(db)
            logger.info('anomaly detection found %s events', len(events))
            return {'status': 'success', 'events': len(events)}
        except Exception as exc:
            logger.exception('anomaly detection failed')
            return {'status': 'failed', 'reason': str(exc)}

    async def run_daily_brief(self) -> dict:
        """Generate daily cross-chat market brief."""
        try:
            result = await generate_daily_brief()
            return result
        except Exception as exc:
            logger.exception('daily brief failed')
            return {'status': 'failed', 'reason': str(exc)}

    async def run_join_queue_once(self) -> dict:
        if not settings.telegram_join_queue_enabled:
            return {'status': 'disabled'}
        if self._join_lock.locked():
            return {'status': 'busy'}
        async with self._join_lock:
            discover_result = {'inserted': 0, 'existing': 0, 'invalid': [], 'scanned': 0}
            try:
                discover_result = discover_join_targets_from_collected_data()
            except Exception as exc:
                logger.warning('auto-discover join targets failed: %s', exc)
            try:
                await self.sync_dialogs()
                sync_join_targets_with_monitored_chats()
            except Exception as exc:
                logger.warning('pre-join dialog sync failed: %s', exc)

            now = datetime.utcnow()
            with session_scope() as db:
                target = db.execute(
                    select(TelegramJoinTarget)
                    .where(
                        TelegramJoinTarget.status == 'pending',
                        or_(
                            TelegramJoinTarget.next_attempt_at.is_(None),
                            TelegramJoinTarget.next_attempt_at <= now,
                        ),
                    )
                    .order_by(TelegramJoinTarget.created_at.asc(), TelegramJoinTarget.id.asc())
                    .limit(1)
                ).scalar_one_or_none()
                target_id = target.id if target else None
            if not target_id:
                return {'status': 'empty', 'discovered': discover_result}
            result = await self._join_one_target(target_id)
            result['discovered'] = discover_result
            return result

    async def _join_one_target(self, target_id: int) -> dict:
        now = datetime.utcnow()
        with session_scope() as db:
            target = db.get(TelegramJoinTarget, target_id)
            if not target or target.status != 'pending':
                return {'status': 'skipped', 'target_id': target_id}
            try:
                normalized_key, target_type, payload = normalize_join_target(target.source)
            except ValueError as exc:
                target.status = 'failed'
                target.last_error = str(exc)
                target.last_attempt_at = now
                return {'status': 'failed', 'target_id': target_id, 'reason': str(exc)}
            target.normalized_key = normalized_key
            target.target_type = target_type
            target.attempt_count = (target.attempt_count or 0) + 1
            target.last_attempt_at = now
            target.last_error = None

        manager, client = await self._least_loaded_account()
        if not client:
            self._mark_join_target(target_id, 'failed', 'no Telegram account is available')
            return {'status': 'failed', 'target_id': target_id, 'reason': 'no Telegram account is available'}

        try:
            entity = None
            if target_type == 'invite':
                try:
                    checked = await client(CheckChatInviteRequest(payload))
                    entity = getattr(checked, 'chat', None)
                except Exception:
                    entity = None
                if entity is None:
                    result = await client(ImportChatInviteRequest(payload))
                    entity = self._extract_chat_from_result(result)
            else:
                entity = await client.get_entity(payload)
                await client(JoinChannelRequest(entity))

            if not isinstance(entity, (Channel, Chat)):
                raise RuntimeError('joined but Telegram did not return a chat entity')
            chat = self._activate_joined_chat(entity, manager.account_key)
            if manager.group_count is not None:
                manager.group_count += 1
            self._mark_join_target(
                target_id,
                'joined',
                None,
                entity=entity,
                monitored_chat_id=chat.id,
            )
            if settings.telegram_live_listener_enabled and manager.account_key not in self._message_handlers:
                await self._register_handler(manager, client)
            try:
                await self._backfill_one(chat.telegram_id)
            except Exception as sync_exc:
                self._mark_join_target(
                    target_id,
                    'joined',
                    f'joined, but initial sync failed: {sync_exc}',
                    entity=entity,
                    monitored_chat_id=chat.id,
                )
                logger.warning('initial sync after join failed target=%s chat=%s: %s', target_id, chat.telegram_id, sync_exc)
            return {'status': 'joined', 'target_id': target_id, 'chat_id': chat.id}
        except Exception as exc:
            return await self._handle_join_error(target_id, exc)

    async def _least_loaded_account(self):
        with session_scope() as db:
            counts = dict(db.execute(
                select(MonitoredChat.telegram_account, func.count(MonitoredChat.id))
                .where(MonitoredChat.telegram_account.is_not(None))
                .group_by(MonitoredChat.telegram_account)
            ).all())
        for manager in sorted(
            telegram_session_managers,
            key=lambda item: item.group_count if item.group_count is not None else counts.get(item.account_key, 0),
        ):
            try:
                client = await manager.connect()
                if client:
                    return manager, client
            except Exception as exc:
                logger.warning('join account unavailable account=%s: %s', manager.label, exc)
        return None, None

    def _extract_chat_from_result(self, result):
        for item in getattr(result, 'chats', []) or []:
            if isinstance(item, (Channel, Chat)):
                return item
        return None

    def _activate_joined_chat(self, entity, account_key: str) -> MonitoredChat:
        peer_id = utils.get_peer_id(entity)
        with session_scope() as db:
            chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == peer_id)).scalar_one_or_none()
            if not chat:
                chat = MonitoredChat(
                    telegram_id=peer_id,
                    access_hash=getattr(entity, 'access_hash', None),
                    title=getattr(entity, 'title', getattr(entity, 'username', str(getattr(entity, 'id', peer_id)))),
                    username=getattr(entity, 'username', None),
                    chat_type=entity.__class__.__name__.lower(),
                    telegram_account=account_key,
                    is_active=True,
                )
                db.add(chat)
                db.flush()
            else:
                chat.title = getattr(entity, 'title', getattr(entity, 'username', chat.title))
                chat.username = getattr(entity, 'username', chat.username)
                chat.access_hash = getattr(entity, 'access_hash', chat.access_hash)
                chat.chat_type = entity.__class__.__name__.lower()
                chat.telegram_account = chat.telegram_account or account_key
                chat.is_active = True
            db.expunge(chat)
            return chat

    def _mark_join_target(self, target_id: int, status: str, error: str | None, entity=None, monitored_chat_id: int | None = None, next_attempt_at: datetime | None = None) -> None:
        now = datetime.utcnow()
        with session_scope() as db:
            target = db.get(TelegramJoinTarget, target_id)
            if not target:
                return
            target.status = status
            target.last_error = error
            target.next_attempt_at = next_attempt_at
            if entity is not None:
                target.title = getattr(entity, 'title', getattr(entity, 'username', target.title))
                target.resolved_telegram_id = utils.get_peer_id(entity)
            if monitored_chat_id:
                target.monitored_chat_id = monitored_chat_id
            if status in {'joined', 'already_joined'}:
                target.joined_at = target.joined_at or now

    async def _handle_join_error(self, target_id: int, exc: Exception) -> dict:
        name = exc.__class__.__name__
        message = str(exc) or name
        if name == 'FloodWaitError':
            seconds = int(getattr(exc, 'seconds', settings.telegram_join_interval_minutes * 60))
            next_attempt_at = datetime.utcnow() + timedelta(seconds=seconds)
            self._mark_join_target(target_id, 'pending', f'FloodWait: retry after {seconds}s', next_attempt_at=next_attempt_at)
            return {'status': 'pending', 'target_id': target_id, 'reason': f'flood wait {seconds}s'}
        if name == 'InviteRequestSentError':
            self._mark_join_target(target_id, 'need_approval', 'join request sent; waiting for admin approval')
            return {'status': 'need_approval', 'target_id': target_id}
        if name == 'UserAlreadyParticipantError':
            try:
                await self.sync_dialogs()
                sync_join_targets_with_monitored_chats()
            except Exception:
                pass
            self._mark_join_target(target_id, 'already_joined', None)
            return {'status': 'already_joined', 'target_id': target_id}
        if isinstance(exc, RPCError) or name.endswith('Error'):
            self._mark_join_target(target_id, 'failed', message[:1000])
            return {'status': 'failed', 'target_id': target_id, 'reason': message}
        self._mark_join_target(target_id, 'failed', message[:1000])
        return {'status': 'failed', 'target_id': target_id, 'reason': message}

    async def sync_dialogs(self) -> int:
        count = 0
        synced_accounts = 0
        for manager in telegram_session_managers:
            try:
                client = await manager.connect()
                if not client:
                    continue
                group_count = 0
                async for dialog in client.iter_dialogs():
                    entity = dialog.entity
                    if not isinstance(entity, (Channel, Chat)):
                        continue
                    group_count += 1
                    peer_id = utils.get_peer_id(entity)
                    persist_lock = self._persist_locks.setdefault(peer_id, asyncio.Lock())
                    async with persist_lock:
                        with session_scope() as db:
                            chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == peer_id)).scalar_one_or_none()
                            if not chat:
                                chat = MonitoredChat(
                                    telegram_id=peer_id,
                                    access_hash=getattr(entity, 'access_hash', None),
                                    title=getattr(entity, 'title', getattr(entity, 'username', str(entity.id))),
                                    username=getattr(entity, 'username', None),
                                    chat_type=entity.__class__.__name__.lower(),
                                    telegram_account=manager.account_key,
                                    is_active=False,
                                )
                                db.add(chat)
                                count += 1
                            else:
                                chat.title = getattr(entity, 'title', getattr(entity, 'username', chat.title))
                                chat.username = getattr(entity, 'username', chat.username)
                                chat.access_hash = getattr(entity, 'access_hash', chat.access_hash)
                                chat.chat_type = entity.__class__.__name__.lower()
                                chat.telegram_account = chat.telegram_account or manager.account_key
                manager.group_count = group_count
                synced_accounts += 1
            except Exception as exc:
                logger.warning('dialog sync failed account=%s: %s', manager.label, exc)
        if not synced_accounts:
            raise RuntimeError('no Telegram account could synchronize dialogs')
        return count

    async def backfill_all_active_chats(self) -> None:
        with session_scope() as db:
            chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
        for chat in chats:
            try:
                await asyncio.wait_for(
                    self._backfill_one(chat.telegram_id),
                    timeout=settings.telegram_backfill_chat_timeout_seconds,
                )
                await asyncio.sleep(0.3)
            except asyncio.TimeoutError:
                logger.warning(
                    'backfill chat %s timed out after %s seconds',
                    chat.telegram_id,
                    settings.telegram_backfill_chat_timeout_seconds,
                )
            except (sqlite3.OperationalError, Exception) as exc:
                logger.warning('backfill chat %s failed: %s', chat.telegram_id, exc)
                if isinstance(exc, sqlite3.OperationalError) and 'database is locked' in str(exc):
                    try:
                        manager = get_telegram_session_manager(chat.telegram_account or 0)
                        await manager._reset_session_file()
                    except ValueError:
                        pass
                    await asyncio.sleep(2)

    async def _backfill_one(self, telegram_chat_id: int) -> int:
        lock = self._backfill_locks.setdefault(telegram_chat_id, asyncio.Lock())
        async with self._backfill_semaphore:
            async with lock:
                return await self.backfill_chat(telegram_chat_id, settings.sync_lookback_messages)

    async def backfill_chat(self, telegram_chat_id: int, limit: int = 1000) -> int:
        total = 0
        internal_chat_id: int | None = None
        run_id: int | None = None
        last_synced_message_id: int | None = None
        with session_scope() as db:
            chat = db.execute(select(MonitoredChat).where(MonitoredChat.telegram_id == telegram_chat_id)).scalar_one_or_none()
            if not chat:
                raise RuntimeError('chat not found in monitored list')
            internal_chat_id = chat.id
            last_synced_message_id = chat.last_synced_message_id
            account_key = chat.telegram_account
            run = SyncRun(chat_id=chat.id, run_type='backfill', status='running', started_at=datetime.utcnow())
            db.add(run)
            db.flush()
            run_id = run.id

        try:
            manager = get_telegram_session_manager(account_key or 0)
            client = await manager.connect()
        except Exception as exc:
            self._mark_sync_failed(run_id, exc)
            raise RuntimeError(f'Telegram account unavailable for chat {telegram_chat_id}: {exc}') from exc
        if not client:
            self._mark_sync_failed(run_id, RuntimeError('Telegram account unavailable'))
            raise RuntimeError('Telegram account unavailable')

        try:
            entity = await client.get_entity(telegram_chat_id)
            messages = []
            if last_synced_message_id:
                async for message in client.iter_messages(
                    entity,
                    min_id=last_synced_message_id,
                    limit=settings.sync_batch_size,
                    reverse=True,
                ):
                    messages.append(message)
            else:
                recent = [message async for message in client.iter_messages(entity, limit=limit)]
                messages = list(reversed(recent))

            for message in messages:
                sender = await message.get_sender()
                await self.persist_message(message, entity, sender, client=client, trigger_ai=False)
                total += 1
        except sqlite3.OperationalError as exc:
            if 'database is locked' in str(exc):
                logger.warning('session locked during backfill chat %s, will retry later', telegram_chat_id)
                await manager._reset_session_file()
            self._mark_sync_failed(run_id, exc)
            raise
        except asyncio.CancelledError:
            self._mark_sync_failed(run_id, RuntimeError('backfill cancelled or timed out'))
            raise
        except Exception as exc:
            self._mark_sync_failed(run_id, exc)
            raise

        with session_scope() as db:
            run = db.get(SyncRun, run_id) if run_id else None
            if run:
                run.status = 'success'
                run.message = f'backfilled {total} new messages'
                run.finished_at = datetime.utcnow()
        if internal_chat_id:
            await self._try_trigger_summary(internal_chat_id)
        return total

    def _mark_sync_failed(self, run_id: int | None, exc: Exception) -> None:
        if not run_id:
            return
        try:
            with session_scope() as db:
                run = db.get(SyncRun, run_id)
                if run:
                    run.status = 'failed'
                    run.message = str(exc)
                    run.finished_at = datetime.utcnow()
        except Exception:
            logger.warning('failed to mark sync run failed', exc_info=True)

    async def _fetch_user_about(self, client, tg_sender) -> str | None:
        if not settings.telegram_fetch_user_about_enabled:
            return None
        if not client or not isinstance(tg_sender, User):
            return None
        try:
            full = await client(GetFullUserRequest(tg_sender.id))
            return getattr(full.full_user, 'about', None) or None
        except Exception:
            return None

    async def _download_media(self, tg_message, chat_telegram_id: int) -> dict | None:
        if not settings.telegram_download_media_enabled:
            return None
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

    def _record_reactions(self, db, message_id: int, tg_message) -> None:
        """Persist message reactions if available."""
        try:
            reactions = getattr(tg_message, 'reactions', None)
            if not reactions:
                return
            results = getattr(reactions, 'results', [])
            if not results:
                return

            reaction_counts: dict[str, int] = {}
            for r in results:
                count = getattr(r, 'count', 0)
                reaction = getattr(r, 'reaction', None)
                reaction_type = getattr(reaction, 'emoticon', None) if reaction else None
                if not reaction_type and reaction:
                    document_id = getattr(reaction, 'document_id', None)
                    reaction_type = f'custom:{document_id}' if document_id else reaction.__class__.__name__
                reaction_type = str(reaction_type or 'like')[:100]
                reaction_counts[reaction_type] = max(reaction_counts.get(reaction_type, 0), count or 0)

            now = datetime.utcnow()
            with db.begin_nested():
                for reaction_type, count in reaction_counts.items():
                    stmt = mysql_insert(MessageReaction).values(
                        message_id=message_id,
                        reaction_type=reaction_type,
                        count=count,
                        updated_at=now,
                    )
                    db.execute(stmt.on_duplicate_key_update(
                        count=stmt.inserted.count,
                        updated_at=now,
                    ))
        except Exception as exc:
            logger.warning('record reactions failed msg=%s: %s', message_id, exc)

    def _record_views_history(self, db, message_id: int, tg_message) -> None:
        """Record views/forwards history for channel posts."""
        try:
            views = getattr(tg_message, 'views', None)
            forwards = getattr(tg_message, 'forwards', None)
            if views is None and forwards is None:
                return
            # Only record if changed significantly or not recorded recently
            last = db.execute(
                select(MessageViewsHistory)
                .where(MessageViewsHistory.message_id == message_id)
                .order_by(MessageViewsHistory.recorded_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last and last.views == views and last.forwards == forwards:
                return
            db.add(MessageViewsHistory(message_id=message_id, views=views or 0, forwards=forwards))
        except Exception as exc:
            logger.warning('record views history failed msg=%s: %s', message_id, exc)

    def _record_edit_history(self, db, message: Message, old_text: str | None, new_text: str | None) -> None:
        """Store edit history when message text changes."""
        if old_text == new_text:
            return
        if not old_text and not new_text:
            return
        db.add(MessageEdit(message_id=message.id, old_text=old_text, new_text=new_text))

    def _update_user_daily_stats(self, db, user_id: int, msg: Message) -> None:
        """Increment daily stats for a user."""
        try:
            from datetime import date
            today = msg.message_date.date() if msg.message_date else date.today()
            stat = db.execute(
                select(UserDailyStat).where(
                    UserDailyStat.user_id == user_id,
                    func.date(UserDailyStat.date) == today,
                )
            ).scalar_one_or_none()
            text = msg.normalized_text or msg.raw_text or ''
            word_count = len(text.split())
            if stat:
                stat.message_count = (stat.message_count or 0) + 1
                stat.word_count = (stat.word_count or 0) + word_count
                if msg.has_media:
                    stat.media_count = (stat.media_count or 0) + 1
                # Update active hours
                hours = dict(stat.active_hours_json or {})
                hour = msg.message_date.hour if msg.message_date else 0
                hours[str(hour)] = hours.get(str(hour), 0) + 1
                stat.active_hours_json = hours
            else:
                hour = msg.message_date.hour if msg.message_date else 0
                db.add(UserDailyStat(
                    user_id=user_id,
                    date=datetime.combine(today, datetime.min.time()),
                    message_count=1,
                    word_count=word_count,
                    media_count=1 if msg.has_media else 0,
                    active_hours_json={str(hour): 1},
                ))
        except Exception as exc:
            logger.warning('update user daily stats failed user=%s: %s', user_id, exc)

    def _update_chat_daily_stats(self, db, chat_id: int, msg: Message, is_new_user: bool) -> None:
        """Increment daily stats for a chat."""
        try:
            from datetime import date
            today = msg.message_date.date() if msg.message_date else date.today()
            stat = db.execute(
                select(DailyChatStat).where(
                    DailyChatStat.chat_id == chat_id,
                    func.date(DailyChatStat.date) == today,
                )
            ).scalar_one_or_none()
            text = msg.normalized_text or msg.raw_text or ''
            msg_len = len(text)
            url_count = len(extract_urls_from_text(text))
            if stat:
                old_avg = stat.avg_message_length or 0
                old_count = stat.message_count or 0
                stat.message_count = old_count + 1
                stat.avg_message_length = (old_avg * old_count + msg_len) / (old_count + 1)
                if msg.has_media:
                    stat.media_count = (stat.media_count or 0) + 1
                stat.url_count = (stat.url_count or 0) + url_count
                if is_new_user:
                    stat.new_user_count = (stat.new_user_count or 0) + 1
            else:
                db.add(DailyChatStat(
                    chat_id=chat_id,
                    date=datetime.combine(today, datetime.min.time()),
                    message_count=1,
                    unique_senders=1,
                    media_count=1 if msg.has_media else 0,
                    url_count=url_count,
                    new_user_count=1 if is_new_user else 0,
                    avg_message_length=float(msg_len),
                ))
        except Exception as exc:
            logger.warning('update chat daily stats failed chat=%s: %s', chat_id, exc)

    async def persist_message(self, tg_message, tg_chat, tg_sender, client=None, trigger_ai: bool = True) -> None:
        if tg_chat is not None:
            chat_telegram_id = utils.get_peer_id(tg_chat)
        else:
            chat_telegram_id = getattr(tg_message, 'chat_id', None)
        if chat_telegram_id is None:
            return

        # CPU & I/O work outside DB transaction
        raw_text = getattr(tg_message, 'message', '') or getattr(tg_message, 'text', '')
        normalized = normalize_text(raw_text)
        keywords_data = extract_keywords(normalized)
        try:
            media_meta = await self._download_media(tg_message, chat_telegram_id)
        except Exception as exc:
            media_meta = None
            logger.warning('media download failed in persist_message: %s', exc)
        try:
            about = await self._fetch_user_about(client, tg_sender) if client else None
        except Exception as exc:
            about = None
            logger.warning('fetch user about failed in persist_message: %s', exc)

        about_urls_to_upsert = extract_urls_from_text(about)
        should_upsert_about_urls = False
        about_urls_chat_id: int | None = None
        persist_lock = self._persist_locks.setdefault(chat_telegram_id, asyncio.Lock())
        async with persist_lock:
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
                            about=about,
                        )
                        db.add(sender)
                        db.flush()
                        should_upsert_about_urls = bool(about_urls_to_upsert)
                    else:
                        sender.username = getattr(tg_sender, 'username', sender.username)
                        sender.first_name = getattr(tg_sender, 'first_name', sender.first_name)
                        sender.last_name = getattr(tg_sender, 'last_name', sender.last_name)
                        sender.is_bot = getattr(tg_sender, 'bot', sender.is_bot)
                        if about and about != sender.about:
                            sender.about = about
                            should_upsert_about_urls = bool(about_urls_to_upsert)

                existing = db.execute(
                    select(Message).where(Message.chat_id == chat.id, Message.telegram_message_id == tg_message.id)
                ).scalar_one_or_none()
                is_new_message = existing is None
                meta_json = dict(existing.meta_json or {}) if existing else {}
                meta_json.update({
                    'grouped_id': getattr(tg_message, 'grouped_id', None),
                    'post_author': getattr(tg_message, 'post_author', None),
                    'via_bot_id': getattr(tg_message, 'via_bot_id', None),
                })
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
                    old_text = existing.raw_text
                    existing.sender_user_id = sender.id if sender else existing.sender_user_id
                    existing.raw_text = getattr(tg_message, 'message', '') or getattr(tg_message, 'text', '')
                    existing.normalized_text = normalized
                    existing.edit_date = tg_message.edit_date.replace(tzinfo=None) if tg_message.edit_date else existing.edit_date
                    existing.views = getattr(tg_message, 'views', existing.views)
                    existing.forwards = getattr(tg_message, 'forwards', existing.forwards)
                    existing.has_media = getattr(tg_message, 'media', None) is not None
                    existing.media_type = tg_message.media.__class__.__name__ if getattr(tg_message, 'media', None) is not None else None
                    existing.meta_json = meta_json
                    if old_text != existing.raw_text:
                        self._record_edit_history(db, existing, old_text, existing.raw_text)
                    for kw in list(existing.keywords):
                        db.delete(kw)
                    db.flush()

                chat.last_message_at = existing.message_date
                chat.last_synced_message_id = max(existing.telegram_message_id, chat.last_synced_message_id or 0)
                chat.title = getattr(tg_chat, 'title', chat.title)
                chat.username = getattr(tg_chat, 'username', chat.username)
                chat.access_hash = getattr(tg_chat, 'access_hash', chat.access_hash)
                for keyword, weight in keywords_data:
                    db.add(MessageKeyword(message_id=existing.id, keyword=keyword[:100], weight=weight))

                # Record reactions & views history
                self._record_reactions(db, existing.id, tg_message)
                self._record_views_history(db, existing.id, tg_message)

                # Daily stats & fingerprint
                if is_new_message:
                    if existing.sender_user_id:
                        self._update_user_daily_stats(db, existing.sender_user_id, existing)
                    is_new_user_today = db.execute(
                        select(func.count(Message.id)).where(
                            Message.chat_id == chat.id,
                            Message.sender_user_id == existing.sender_user_id,
                            Message.id != existing.id,
                            func.date(Message.message_date) == func.date(existing.message_date),
                        )
                    ).scalar() == 0
                    self._update_chat_daily_stats(db, chat.id, existing, is_new_user_today)

                # Content fingerprint for deduplication
                try:
                    save_fingerprint(db, existing.id, existing.normalized_text or existing.raw_text or '')
                except Exception as exc:
                    logger.warning('fingerprint failed msg=%s: %s', existing.id, exc)

                # Check alert rules for new messages only
                if is_new_message:
                    try:
                        check_message_alerts(db, existing, chat)
                    except Exception as exc:
                        logger.warning('Alert check failed for message %s: %s', existing.id, exc)

                if trigger_ai and existing.id:
                    asyncio.create_task(self._try_trigger_summary(chat.id))

                if should_upsert_about_urls:
                    about_urls_chat_id = chat.id

        if should_upsert_about_urls and about_urls_chat_id:
            try:
                inserted = upsert_discovered_urls(about_urls_to_upsert, category='other', chat_id=about_urls_chat_id)
                if inserted:
                    logger.info('profile URL discovery chat=%s urls=%d', about_urls_chat_id, inserted)
            except Exception as exc:
                logger.warning('profile URL discovery failed chat=%s: %s', about_urls_chat_id, exc)


    def _configure_summary_window(self) -> None:
        """(Re)register the backlog drain that opens the daily active window."""
        job_id = 'ai_summary_drain'
        if not settings.ai_summary_active_hours_enabled:
            if self.scheduler.get_job(job_id) is not None:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    logger.warning('failed to remove AI summary drain job', exc_info=True)
            return

        hour, minute = settings.ai_summary_active_start_hm
        self.scheduler.add_job(
            self.drain_summary_backlog,
            'cron',
            hour=hour,
            minute=minute,
            timezone=LOCAL_TIMEZONE,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # If the app was down at the window start, still drain within the hour.
            misfire_grace_time=3600,
        )
        logger.info(
            'AI summary active window %s-%s (%s); backlog drain at %02d:%02d',
            settings.ai_summary_active_start,
            settings.ai_summary_active_end,
            LOCAL_TIMEZONE,
            hour,
            minute,
        )

    @staticmethod
    def _pending_message_count(chat_id: int) -> int:
        with session_scope() as db:
            last_success = db.query(AiSummary).filter(
                AiSummary.chat_id == chat_id,
                AiSummary.status == 'success',
            ).order_by(AiSummary.id.desc()).first()
            last_id = last_success.end_message_id if last_success and last_success.end_message_id else 0
            return db.query(func.count(Message.id)).filter(
                Message.chat_id == chat_id,
                Message.id > last_id,
            ).scalar() or 0

    @staticmethod
    def _chats_with_pending_messages() -> list[int]:
        """Chats that still have unsummarised messages, oldest backlog first."""
        with session_scope() as db:
            last_success = (
                db.query(
                    AiSummary.chat_id.label('chat_id'),
                    func.max(AiSummary.end_message_id).label('last_message_id'),
                )
                .filter(AiSummary.status == 'success')
                .group_by(AiSummary.chat_id)
                .subquery()
            )
            rows = (
                db.query(Message.chat_id, func.min(Message.id).label('oldest_id'))
                .outerjoin(last_success, last_success.c.chat_id == Message.chat_id)
                .filter(Message.id > func.coalesce(last_success.c.last_message_id, 0))
                .group_by(Message.chat_id)
                .order_by(func.min(Message.id))
                .all()
            )
            return [row[0] for row in rows]

    async def drain_summary_backlog(self) -> None:
        """Drain the summary backlog for every chat, one API call at a time."""
        if not settings.ai_summary_active_hours_enabled:
            return
        if not in_active_summary_window():
            logger.info('backlog drain skipped: outside the active summary window')
            return
        if self._summary_queue_lock.locked():
            logger.info('backlog drain skipped: a previous run is still in progress')
            return

        batch_size = settings.ai_summary_active_batch_size or settings.ai_summary_batch_size
        async with self._summary_queue_lock:
            chat_ids = self._chats_with_pending_messages()
            logger.info('backlog drain started: %d chat(s) with pending messages, batch=%d',
                        len(chat_ids), batch_size)
            processed = 0
            for chat_id in chat_ids:
                if not in_active_summary_window():
                    logger.info('active window closed; stopping backlog drain')
                    break
                batches = 0
                while True:
                    pending = self._pending_message_count(chat_id)
                    if pending <= 0:
                        break
                    await run_summary_for_chat(chat_id, min_messages=1, batch_size=batch_size)
                    batches += 1
                    remaining = self._pending_message_count(chat_id)
                    if remaining >= pending:
                        logger.warning(
                            'backlog drain made no progress chat=%s pending=%d (missing API key, error, or locked row)',
                            chat_id, pending,
                        )
                        break
                    if settings.ai_summary_active_pause_seconds > 0:
                        await asyncio.sleep(settings.ai_summary_active_pause_seconds)
                if batches:
                    processed += 1
                    logger.info('backlog drain chat=%s batches=%d', chat_id, batches)
            logger.info('backlog drain finished: chats=%d processed=%d', len(chat_ids), processed)

    async def _try_trigger_summary(self, chat_id: int) -> None:
        # Outside the active window (and while a drain is running) messages just
        # accumulate: no API call is made.
        if not in_active_summary_window() or self._summary_queue_lock.locked():
            return
        if chat_id not in self._summary_locks:
            self._summary_locks[chat_id] = asyncio.Lock()
        lock = self._summary_locks[chat_id]
        if lock.locked():
            return
        async with lock:
            with session_scope() as db:
                running = db.query(AiSummary).filter(
                    AiSummary.chat_id == chat_id,
                    AiSummary.status == 'running',
                ).first()
                if running:
                    timeout_at = datetime.utcnow() - timedelta(minutes=settings.ai_summary_running_timeout_minutes)
                    if running.triggered_at and running.triggered_at < timeout_at:
                        running.status = 'failed'
                        running.error_message = 'AI summary timed out and was released for retry'
                        running.completed_at = datetime.utcnow()
                    else:
                        return
                last = db.query(AiSummary).filter(
                    AiSummary.chat_id == chat_id,
                ).order_by(AiSummary.id.desc()).first()
                # Enforce minimum trigger interval
                if last and last.triggered_at:
                    interval = timedelta(minutes=settings.ai_summary_min_trigger_interval_minutes)
                    if datetime.utcnow() - last.triggered_at < interval:
                        return
                last_success = db.query(AiSummary).filter(
                    AiSummary.chat_id == chat_id,
                    AiSummary.status == 'success',
                ).order_by(AiSummary.id.desc()).first()
                last_msg_id = last_success.end_message_id if last_success else 0
                new_count = db.query(func.count(Message.id)).filter(
                    Message.chat_id == chat_id,
                    Message.id > last_msg_id,
                ).scalar() or 0

            if settings.ai_summary_slide_window_enabled:
                min_required = min(settings.ai_summary_min_batch_size, settings.ai_summary_slide_window_size)
            else:
                min_required = settings.ai_summary_batch_size
            if new_count >= min_required:
                asyncio.create_task(run_summary_for_chat(chat_id))


collector = TelegramCollector()

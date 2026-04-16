from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import SQLiteSession

from .config import settings

logger = logging.getLogger(__name__)


class TelegramSessionManager:
    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> TelegramClient | None:
        if self.client and await self._maybe_await(self.client.is_user_authorized()):
            return self.client
        return None

    async def connect(self) -> TelegramClient | None:
        async with self._lock:
            if self.client and await self._maybe_await(self.client.is_connected()):
                return self.client

            if settings.telegram_session_mode == 'desktop':
                try:
                    client = await self._connect_from_desktop()
                    self.client = client
                    return client
                except BaseException as exc:
                    logger.warning('desktop session import failed: %s', exc)

            if settings.resolved_session_path.exists() and settings.telegram_api_id and settings.telegram_api_hash:
                client = TelegramClient(str(settings.resolved_session_path), settings.telegram_api_id, settings.telegram_api_hash)
                await client.connect()
                if await self._maybe_await(client.is_user_authorized()):
                    self.client = client
                    return client

            logger.warning('telegram session is not ready yet')
            return None

    async def _connect_from_desktop(self) -> TelegramClient:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession

        settings.resolved_session_path.parent.mkdir(parents=True, exist_ok=True)
        candidates = [settings.resolved_tdata_path]
        if settings.resolved_tdata_path.name != 'tdata':
            candidates.append(settings.resolved_tdata_path / 'tdata')
        keyfiles = [None, 'data', 'datas']

        last_error = None
        for candidate in candidates:
            if not candidate.exists():
                continue
            for keyfile in keyfiles:
                kwargs = {} if keyfile is None else {'keyFile': keyfile}
                try:
                    tdesktop = TDesktop(str(candidate), **kwargs)
                    client = await tdesktop.ToTelethon(str(settings.resolved_session_path), UseCurrentSession)
                    await client.connect()
                    if not await self._maybe_await(client.is_user_authorized()):
                        raise RuntimeError('desktop session imported but authorization is invalid')
                    logger.info('desktop session imported from %s with keyfile=%s', candidate, keyfile)
                    return client
                except BaseException as exc:
                    last_error = exc
                    logger.warning('desktop session import attempt failed path=%s keyfile=%s err=%s', candidate, keyfile, exc)
                    continue
        raise RuntimeError(f'failed to import Telegram Desktop session: {last_error}')

    async def manual_login(self, phone: str, code_callback, password: str | None = None) -> TelegramClient:
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise RuntimeError('manual login requires TELEGRAM_API_ID and TELEGRAM_API_HASH')
        settings.resolved_session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(str(settings.resolved_session_path), settings.telegram_api_id, settings.telegram_api_hash)
        await client.connect()
        await client.send_code_request(phone)
        code = await code_callback()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError('2FA password is required')
            await client.sign_in(password=password)
        self.client = client
        return client

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

telegram_session_manager = TelegramSessionManager()

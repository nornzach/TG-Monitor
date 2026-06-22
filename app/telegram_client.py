from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone

import socks
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from .config import settings

logger = logging.getLogger(__name__)


class UnsafeTelegramSessionError(RuntimeError):
    pass


class TelegramSessionManager:
    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self._lock = asyncio.Lock()
        self._manual_login_client: TelegramClient | None = None
        self._manual_login_phone: str | None = None
        self._manual_login_phone_code_hash: str | None = None

    @property
    def pending_manual_login_phone(self) -> str | None:
        return self._manual_login_phone

    def get_session_metadata(self) -> dict | None:
        return self._read_session_metadata()

    async def get_client(self) -> TelegramClient | None:
        await self._acquire_lock('get telegram client')
        try:
            if self.client and await self._wait_for_maybe(self.client.is_connected(), operation='check existing client connection'):
                try:
                    if await self._wait_for_maybe(self.client.is_user_authorized(), operation='check existing client authorization'):
                        return self.client
                except Exception:
                    self.client = None
            return None
        finally:
            self._lock.release()

    async def _disconnect_existing(self) -> None:
        if self.client:
            try:
                await self._wait_for_telegram_io(self.client.disconnect(), operation='disconnect existing client', timeout=5)
            except asyncio.TimeoutError:
                logger.warning('timeout disconnecting existing client')
            except Exception:
                pass
            self.client = None

    async def _reset_session_file(self) -> None:
        session_path = settings.resolved_session_path
        for p in [session_path,
                  session_path.parent / f'{session_path.name}-journal',
                  session_path.parent / f'{session_path.name}-wal',
                  session_path.parent / f'{session_path.name}-shm',
                  self._session_metadata_path()]:
            try:
                if p.exists():
                    p.unlink()
                    logger.info('deleted stale session file %s', p)
            except Exception as exc:
                logger.warning('failed to delete session file %s: %s', p, exc)

    def _proxy(self) -> tuple | None:
        if settings.telegram_proxy_host and settings.telegram_proxy_port:
            return (socks.SOCKS5, settings.telegram_proxy_host, settings.telegram_proxy_port)
        return None

    async def connect(self, allow_desktop_import: bool = False) -> TelegramClient | None:
        await self._acquire_lock('connect telegram session')
        try:
            if self.client and await self._wait_for_maybe(self.client.is_connected(), operation='check existing client connection'):
                return self.client

            await self._disconnect_existing()

            for attempt in range(3):
                try:
                    client = await self._try_existing_session()
                    if client:
                        self.client = client
                        return client
                except UnsafeTelegramSessionError:
                    raise
                except (sqlite3.OperationalError, Exception) as exc:
                    if isinstance(exc, sqlite3.OperationalError) and 'database is locked' in str(exc):
                        logger.warning('session locked on connect attempt %d/3, retrying without reset...', attempt + 1)
                        await asyncio.sleep(1)
                        continue
                    if attempt < 2:
                        logger.warning('session connect attempt %d/3 failed: %s, retrying...', attempt + 1, exc)
                        await asyncio.sleep(1)
                        continue
                    raise

            # 2) Desktop tdata import (slow path, only if no session file)
            if allow_desktop_import and settings.telegram_session_mode == 'desktop':
                try:
                    client = await self._connect_from_desktop()
                    self.client = client
                    return client
                except BaseException as exc:
                    logger.warning('desktop session import failed: %s', exc)

            logger.warning('telegram session is not ready yet')
            return None
        finally:
            self._lock.release()

    async def _try_existing_session(self) -> TelegramClient | None:
        if not settings.resolved_session_path.exists():
            return None
        self._assert_session_safe_to_reuse()

        api_id = settings.telegram_api_id
        api_hash = settings.telegram_api_hash

        if not api_id or not api_hash:
            try:
                from opentele.api import API
                api = API.TelegramDesktop()
                api_id = api.api_id
                api_hash = api.api_hash
            except Exception:
                logger.warning('no API credentials available for session file')
                return None

        client = TelegramClient(
            str(settings.resolved_session_path),
            api_id=api_id,
            api_hash=api_hash,
            receive_updates=False,
            proxy=self._proxy(),
            timeout=self._client_timeout(),
        )
        try:
            await self._wait_for_telegram_io(client.connect(), operation='connect existing session')
            if await self._wait_for_maybe(client.is_user_authorized(), operation='check existing session authorization'):
                logger.info('reused existing session file')
                return client
            await self._disconnect_client(client, operation='disconnect unauthorized session')
            return None
        except (Exception, asyncio.CancelledError):
            await self._disconnect_client(client, operation='disconnect failed session')
            raise

    async def _connect_from_desktop(self) -> TelegramClient:
        from opentele.td import TDesktop
        from opentele.api import CreateNewSession

        if not settings.telegram_desktop_import_enabled:
            raise UnsafeTelegramSessionError('Telegram Desktop import is disabled')
        if settings.telegram_desktop_import_mode != 'create_new':
            raise UnsafeTelegramSessionError('refusing to import Telegram Desktop with shared use_current mode')

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
                    client = await tdesktop.ToTelethon(str(settings.resolved_session_path), CreateNewSession)
                    try:
                        await self._wait_for_telegram_io(client.connect(), operation='connect imported desktop session')
                        if not await self._wait_for_maybe(client.is_user_authorized(), operation='check imported desktop session authorization'):
                            raise RuntimeError('desktop session imported but authorization is invalid')
                        logger.info(
                            'desktop session imported from %s with keyfile=%s mode=%s',
                            candidate,
                            keyfile,
                            settings.telegram_desktop_import_mode,
                        )
                        self._write_session_metadata('desktop_create_new')
                        return client
                    except BaseException:
                        await self._disconnect_client(client, operation='disconnect failed desktop session')
                        raise
                except BaseException as exc:
                    last_error = exc
                    logger.warning('desktop session import attempt failed path=%s keyfile=%s err=%s', candidate, keyfile, exc)
                    continue
        raise RuntimeError(f'failed to import Telegram Desktop session: {last_error}')

    async def manual_login(self, phone: str, code_callback, password: str | None = None) -> TelegramClient:
        await self.start_manual_login(phone)
        code = await code_callback()
        return await self.complete_manual_login(code, password=password)

    async def start_manual_login(self, phone: str) -> None:
        async with self._lock:
            await self._disconnect_existing()
            await self._disconnect_pending_manual_login()
            api_id, api_hash = self._resolve_api_credentials()
            settings.resolved_session_path.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                str(settings.resolved_session_path),
                api_id=api_id,
                api_hash=api_hash,
                receive_updates=False,
                proxy=self._proxy(),
                timeout=self._client_timeout(),
            )
            try:
                await client.connect()
                sent_code = await client.send_code_request(phone)
                self._manual_login_client = client
                self._manual_login_phone = phone
                self._manual_login_phone_code_hash = sent_code.phone_code_hash
            except Exception:
                await client.disconnect()
                raise

    async def complete_manual_login(self, code: str, password: str | None = None) -> TelegramClient | None:
        async with self._lock:
            if not self._manual_login_client or not self._manual_login_phone or not self._manual_login_phone_code_hash:
                raise RuntimeError('please request a Telegram login code first')
            try:
                try:
                    await self._manual_login_client.sign_in(
                        phone=self._manual_login_phone,
                        code=code,
                        phone_code_hash=self._manual_login_phone_code_hash,
                    )
                except SessionPasswordNeededError:
                    if not password:
                        raise RuntimeError('2FA password is required')
                    await self._manual_login_client.sign_in(password=password)
                me = None
                try:
                    me = await self._manual_login_client.get_me()
                except Exception:
                    logger.warning('manual login succeeded but get_me failed', exc_info=True)
                self._write_session_metadata('manual_login', me=me)
                await self._manual_login_client.disconnect()
                self.client = None
                self._manual_login_client = None
                self._manual_login_phone = None
                self._manual_login_phone_code_hash = None
                return self.client
            except Exception:
                raise

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _acquire_lock(self, operation: str) -> None:
        timeout = settings.telegram_connect_timeout_seconds
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f'{operation} timed out waiting for session lock after {timeout} seconds') from exc
        else:
            await self._lock.acquire()

    async def _wait_for_telegram_io(self, awaitable, *, operation: str, timeout: float | None = None):
        if timeout is None:
            timeout = settings.telegram_connect_timeout_seconds
        if timeout and timeout > 0:
            try:
                return await asyncio.wait_for(awaitable, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f'{operation} timed out after {timeout} seconds') from exc
        return await awaitable

    async def _wait_for_maybe(self, value, *, operation: str):
        if asyncio.iscoroutine(value):
            return await self._wait_for_telegram_io(value, operation=operation)
        return value

    def _client_timeout(self) -> int:
        timeout = settings.telegram_connect_timeout_seconds
        if timeout and timeout > 0:
            return max(1, int(timeout))
        return 10

    async def _disconnect_client(self, client: TelegramClient, *, operation: str) -> None:
        try:
            await self._wait_for_telegram_io(client.disconnect(), operation=operation, timeout=5)
        except asyncio.TimeoutError:
            logger.warning('%s timed out', operation)
        except Exception:
            pass

    async def _disconnect_pending_manual_login(self) -> None:
        if self._manual_login_client:
            try:
                await self._disconnect_client(self._manual_login_client, operation='disconnect pending manual login')
            except Exception:
                pass
        self._manual_login_client = None
        self._manual_login_phone = None
        self._manual_login_phone_code_hash = None

    def _resolve_api_credentials(self) -> tuple[int, str]:
        api_id = settings.telegram_api_id
        api_hash = settings.telegram_api_hash
        if not api_id or not api_hash:
            from opentele.api import API
            api = API.TelegramDesktop()
            api_id = api.api_id
            api_hash = api.api_hash
        return api_id, api_hash

    def _session_metadata_path(self):
        session_path = settings.resolved_session_path
        return session_path.parent / f'{session_path.name}.meta.json'

    def _read_session_metadata(self) -> dict | None:
        path = self._session_metadata_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise UnsafeTelegramSessionError(f'failed to read Telegram session metadata: {exc}') from exc

    def _assert_session_safe_to_reuse(self) -> None:
        if settings.telegram_session_mode != 'desktop':
            return
        metadata = self._read_session_metadata()
        if not metadata:
            raise UnsafeTelegramSessionError(
                'refusing to reuse legacy Telegram session without create_new metadata; '
                'delete data/telethon.session* and import a fresh independent session'
            )
        if metadata.get('session_kind') != 'desktop_create_new':
            raise UnsafeTelegramSessionError(
                f"refusing to reuse Telegram session created by {metadata.get('session_kind')!r}; "
                'desktop mode must use a create_new session'
            )

    def _write_session_metadata(self, session_kind: str, me=None) -> None:
        path = self._session_metadata_path()
        data = {
            'session_kind': session_kind,
            'desktop_import_mode': 'create_new',
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        if me:
            data['account'] = {
                'id': getattr(me, 'id', None),
                'username': getattr(me, 'username', None),
                'first_name': getattr(me, 'first_name', None),
            }
        path.write_text(
            json.dumps(data, ensure_ascii=True, indent=2) + '\n',
            encoding='utf-8',
        )

telegram_session_manager = TelegramSessionManager()

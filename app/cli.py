from __future__ import annotations

import argparse
import asyncio
import getpass

from .collector import collector
from .config import settings
from .db import init_database
from .telegram_client import telegram_session_manager


async def import_desktop_session() -> None:
    client = await telegram_session_manager.connect()
    if not client:
        raise RuntimeError('failed to connect using desktop session')
    me = await client.get_me()
    print(f'desktop session ready: {me.id} {getattr(me, "username", "") or getattr(me, "first_name", "") or ""}')


async def sync_dialogs() -> None:
    count = await collector.sync_dialogs()
    print(f'synced dialogs: {count}')


async def backfill(chat_id: int, limit: int) -> None:
    total = await collector.backfill_chat(chat_id, limit=limit)
    print(f'backfilled messages: {total}')


async def manual_login(phone: str, code: str | None, password: str | None) -> None:
    async def code_callback() -> str:
        if code:
            return code
        return input('Telegram code: ').strip()

    await telegram_session_manager.manual_login(phone, code_callback, password=password)
    print('manual login success')


def main() -> None:
    parser = argparse.ArgumentParser(description='TG Monitor Platform CLI')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('init-db')
    sub.add_parser('import-desktop-session')
    sub.add_parser('sync-dialogs')

    backfill_parser = sub.add_parser('backfill')
    backfill_parser.add_argument('--chat-id', required=True, type=int)
    backfill_parser.add_argument('--limit', default=settings.sync_lookback_messages, type=int)

    login_parser = sub.add_parser('manual-login')
    login_parser.add_argument('--phone', required=True)
    login_parser.add_argument('--code')
    login_parser.add_argument('--password')

    args = parser.parse_args()
    if args.command == 'init-db':
        init_database()
        print(f'database ready: {settings.database_name}')
        return
    if args.command == 'import-desktop-session':
        init_database()
        asyncio.run(import_desktop_session())
        return
    if args.command == 'sync-dialogs':
        init_database()
        asyncio.run(sync_dialogs())
        return
    if args.command == 'backfill':
        init_database()
        asyncio.run(backfill(args.chat_id, args.limit))
        return
    if args.command == 'manual-login':
        init_database()
        password = args.password or ''
        if not args.code:
            print('A login code will be requested after the app sends it to Telegram.')
        if password == 'ASK':
            password = getpass.getpass('2FA password: ')
        asyncio.run(manual_login(args.phone, args.code, password or None))
        return


if __name__ == '__main__':
    main()

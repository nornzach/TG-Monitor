from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select

from .db import session_scope
from .models import AiContact, AiUrl, AiUrlCategory, AiUrlClassification, MonitoredChat, TelegramJoinTarget

JOIN_STATUS_KEYS = {
    'pending',
    'joined',
    'already_joined',
    'need_approval',
    'failed',
    'skipped',
}


def normalize_join_target(value: str) -> tuple[str, str, str]:
    raw = (value or '').strip()
    if not raw:
        raise ValueError('empty target')

    candidate = raw
    if candidate.startswith('@'):
        username = candidate[1:].strip('/').lower()
        if not username:
            raise ValueError('empty username')
        return f'username:{username}', 'username', username

    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', candidate):
        if re.match(r'^[A-Za-z0-9_]{4,}$', candidate):
            username = candidate.strip('/').lower()
            return f'username:{username}', 'username', username
        candidate = 'https://' + candidate

    parsed = urlparse(candidate)
    host = (parsed.netloc or '').lower()
    path = parsed.path.strip('/')
    if host in {'t.me', 'telegram.me', 'telegram.dog'}:
        if path.startswith('+'):
            invite_hash = path[1:].split('/')[0]
            return f'invite:{invite_hash}', 'invite', invite_hash
        if path.startswith('joinchat/'):
            invite_hash = path.split('/', 1)[1].split('/')[0]
            return f'invite:{invite_hash}', 'invite', invite_hash
        if path:
            username = path.split('/')[0].lower()
            if username and username not in {'c', 's'}:
                return f'username:{username}', 'username', username

    raise ValueError('unsupported Telegram group link')


def enqueue_join_targets(raw_text: str) -> dict:
    return enqueue_join_target_values(
        [line.strip() for line in (raw_text or '').splitlines() if line.strip()],
        source_prefix='manual',
    )


def enqueue_join_target_values(values: list[str], source_prefix: str = 'auto') -> dict:
    inserted = 0
    existing = 0
    invalid: list[str] = []
    seen: set[str] = set()
    candidates = [value.strip() for value in values if value and value.strip()]
    with session_scope() as db:
        for candidate in candidates:
            try:
                normalized_key, target_type, _ = normalize_join_target(candidate)
            except ValueError:
                invalid.append(candidate)
                continue
            if normalized_key in seen:
                existing += 1
                continue
            seen.add(normalized_key)
            row = db.execute(
                select(TelegramJoinTarget).where(TelegramJoinTarget.normalized_key == normalized_key)
            ).scalar_one_or_none()
            if row:
                existing += 1
                continue
            db.add(TelegramJoinTarget(
                source=candidate,
                normalized_key=normalized_key,
                target_type=target_type,
                title=f'{source_prefix}: {candidate}' if source_prefix != 'manual' else None,
                status='pending',
            ))
            inserted += 1
    return {'inserted': inserted, 'existing': existing, 'invalid': invalid}


def discover_join_targets_from_collected_data() -> dict:
    values: list[str] = []
    url_candidates = 0
    contact_candidates = 0

    with session_scope() as db:
        telegram_category_ids = [
            row.id for row in db.execute(
                select(AiUrlCategory).where(AiUrlCategory.slug == 'telegram_group')
            ).scalars().all()
        ]
        telegram_url_ids = set()
        if telegram_category_ids:
            telegram_url_ids = set(db.execute(
                select(AiUrlClassification.url_id).where(AiUrlClassification.category_id.in_(telegram_category_ids))
            ).scalars().all())
        url_query = select(AiUrl).where(AiUrl.domain.in_(['t.me', 'telegram.me', 'telegram.dog']))
        urls = db.execute(url_query).scalars().all()
        for row in urls:
            if _is_joinable_url(row.url, row.primary_category_id in telegram_category_ids or row.id in telegram_url_ids):
                values.append(row.url)
                url_candidates += 1

        contacts = db.execute(
            select(AiContact).where(AiContact.contact_type == 'tg_group')
        ).scalars().all()
        for row in contacts:
            if _is_joinable_text(row.contact_value):
                values.append(row.contact_value)
                contact_candidates += 1

    result = enqueue_join_target_values(values, source_prefix='auto')
    result.update({
        'scanned': len(values),
        'url_candidates': url_candidates,
        'contact_candidates': contact_candidates,
    })
    return result


def _is_joinable_text(value: str | None) -> bool:
    if not value:
        return False
    try:
        normalize_join_target(value)
        return True
    except ValueError:
        return False


def _is_joinable_url(value: str | None, category_is_telegram_group: bool = False) -> bool:
    if not value:
        return False
    try:
        _, target_type, _ = normalize_join_target(value)
    except ValueError:
        return False
    if target_type == 'invite':
        return True
    return category_is_telegram_group


def sync_join_targets_with_monitored_chats() -> dict:
    updated = 0
    now = datetime.utcnow()
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat)).scalars().all()
        chats_by_username = {
            chat.username.lower(): chat for chat in chats if chat.username
        }
        chats_by_telegram_id = {chat.telegram_id: chat for chat in chats}
        targets = db.execute(select(TelegramJoinTarget)).scalars().all()
        for target in targets:
            chat = None
            if target.resolved_telegram_id:
                chat = chats_by_telegram_id.get(target.resolved_telegram_id)
            if not chat and target.normalized_key.startswith('username:'):
                username = target.normalized_key.split(':', 1)[1].lower()
                chat = chats_by_username.get(username)
            if not chat:
                continue
            if target.monitored_chat_id != chat.id or target.status not in {'joined', 'already_joined'}:
                target.monitored_chat_id = chat.id
                target.resolved_telegram_id = chat.telegram_id
                target.title = chat.title
                target.status = 'already_joined'
                target.joined_at = target.joined_at or now
                target.last_error = None
                chat.is_active = True
                updated += 1
    return {'updated': updated}

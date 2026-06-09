from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import select, desc, func

from . import analysis
from .analysis import dashboard_metrics, chat_statistics, system_status
from .ai_service import PROVIDER_CONFIGS, get_ai_provider_config, get_url_classification_prompt, run_key_lead_analysis_once, run_summary_now, run_url_classification_once
from .collector import collector
from .config import settings, BASE_DIR
from .db import init_database, session_scope
from .models import MonitoredChat, Message, TelegramUser, SyncRun, AppSetting, AiSummary, AiUrl, AiUrlCategory, AiUrlClassificationRun, AiProduct, AiContact, AiKeyLead, AiKeyLeadRun, AlertRule, AlertMatch
from .telegram_client import telegram_session_manager

# ==================== i18n ====================

_I18N_DIR = BASE_DIR / 'app' / 'i18n'
_I18N_DATA: dict[str, dict] = {}


def _load_i18n() -> None:
    for lang_file in _I18N_DIR.glob('*.json'):
        lang_code = lang_file.stem
        with lang_file.open(encoding='utf-8') as f:
            _I18N_DATA[lang_code] = json.load(f)


def _get_lang(request: Request) -> str:
    lang = request.cookies.get('lang', 'zh')
    return lang if lang in _I18N_DATA else 'zh'


def _t(request: Request, key: str, **kwargs) -> str:
    lang = _get_lang(request)
    data = _I18N_DATA.get(lang, _I18N_DATA.get('zh', {}))
    value = data
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break
    if value is None:
        # Fallback to Chinese
        value = _I18N_DATA.get('zh', {})
        for part in key.split('.'):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
    if value is None:
        return key
    if kwargs:
        for k, v in kwargs.items():
            value = value.replace('{{' + k + '}}', str(v))
    return value

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)
_instance_lock_file = None


def redirect_with_message(path: str, message: str, level: str = 'info') -> RedirectResponse:
    return RedirectResponse(f'{path}?msg={message}&level={level}', status_code=303)


def _env_bool(value: bool) -> str:
    return 'true' if value else 'false'


# ==================== Authentication ====================

_AUTH_SECRET = secrets.token_hex(32)


def _make_auth_token() -> str:
    return hashlib.sha256(f"{settings.auth_password}:{_AUTH_SECRET}".encode()).hexdigest()


def _check_auth_cookie(request: Request) -> bool:
    token = request.cookies.get('auth_token')
    if not token:
        return False
    expected = _make_auth_token()
    return secrets.compare_digest(token, expected)


def _check_api_sk(request: Request) -> bool:
    sk = request.headers.get('x-api-key', '')
    if not sk:
        return False
    return secrets.compare_digest(sk.strip(), settings.api_sk)


_PUBLIC_PATHS = {'/login', '/logout'}


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware for page-level cookie auth and API key auth."""

    async def dispatch(self, request: Request, call_next):
        from urllib.parse import quote
        path = request.url.path

        # Static/media/lang paths — always allow
        if path.startswith(('/static/', '/media/', '/lang/')):
            return await call_next(request)

        # Login/logout paths — always allow
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        # API routes — check X-API-Key header
        if path.startswith('/api/'):
            if _check_api_sk(request):
                return await call_next(request)
            return JSONResponse(
                {'error': 'Unauthorized', 'message': 'Missing or invalid API key (X-API-Key header)'},
                status_code=401,
            )

        # All other routes — check auth cookie
        # Also skip auth for the language-switch redirect (it's handled below)
        if _check_auth_cookie(request):
            return await call_next(request)

        # Not authenticated — redirect to login
        # For POST requests, try Referer header for next, fallback to /
        if request.method == 'POST':
            ref = request.headers.get('referer', '')
            if ref.startswith('/'):
                next_url = ref
            else:
                # Only keep path portion from absolute URLs
                from urllib.parse import urlparse
                parsed = urlparse(ref)
                next_url = parsed.path if parsed.path.startswith('/') else '/'
            return RedirectResponse(f'/login?next={quote(next_url)}', status_code=303)

        # For GET requests, preserve the original URL as next
        next_url = str(request.url.path)
        if request.url.query:
            next_url += '?' + request.url.query
        return RedirectResponse(f'/login?next={quote(next_url)}', status_code=303)


# ==================== i18n ====================


def _update_env_file(updates: dict[str, str]) -> None:
    env_path = BASE_DIR / '.env'
    lock_path = BASE_DIR / 'data' / '.env.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        existing = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
        written: set[str] = set()
        lines: list[str] = []
        for line in existing:
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
                lines.append(line)
                continue
            key = line.split('=', 1)[0].strip()
            if key in updates:
                lines.append(f'{key}={updates[key]}')
                written.add(key)
            else:
                lines.append(line)
        for key, value in updates.items():
            if key not in written:
                lines.append(f'{key}={value}')
        env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _apply_settings(updates: dict[str, object]) -> None:
    for key, value in updates.items():
        setattr(settings, key, value)


def _iso_dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=' ') if value else None


def _normalize_pagination(page: int, page_size: int, max_page_size: int = 200) -> tuple[int, int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), max_page_size)
    return page, page_size


def _parse_optional_int(value: int | str | None) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pagination_meta(total: int, page: int, page_size: int) -> dict:
    total_pages = max((total + page_size - 1) // page_size, 1)
    return {
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': total_pages,
        'has_prev': page > 1,
        'has_next': page < total_pages,
    }


def _query_total(db, query) -> int:
    return db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()


def _chat_payload(chat: MonitoredChat | None) -> dict | None:
    if not chat:
        return None
    return {
        'id': chat.id,
        'telegram_id': chat.telegram_id,
        'title': chat.title,
        'username': chat.username,
        'chat_type': chat.chat_type,
        'is_active': chat.is_active,
    }


def _sender_payload(sender: TelegramUser | None) -> dict | None:
    if not sender:
        return None
    return {
        'id': sender.id,
        'telegram_id': sender.telegram_id,
        'username': sender.username,
        'first_name': sender.first_name,
        'last_name': sender.last_name,
        'is_bot': sender.is_bot,
    }


async def _run_url_classification_background(batch_size: int, include_classified: bool) -> None:
    try:
        result = await run_url_classification_once(batch_size=batch_size, include_classified=include_classified)
        logger.info('background URL classification finished: %s', result)
    except Exception:
        logger.exception('background URL classification failed')


async def _run_summary_now_background(chat_id: int, source: str) -> None:
    try:
        summary_id = await run_summary_now(chat_id)
        logger.info('background AI summary finished source=%s chat=%s summary=%s', source, chat_id, summary_id)
    except Exception:
        logger.exception('background AI summary failed source=%s chat=%s', source, chat_id)


async def _run_key_lead_analysis_background(batch_size: int | None = None) -> None:
    try:
        result = await run_key_lead_analysis_once(batch_size=batch_size)
        logger.info('background key lead analysis finished: %s', result)
    except Exception:
        logger.exception('background key lead analysis failed')


def _acquire_instance_lock() -> None:
    global _instance_lock_file
    lock_path = BASE_DIR / 'data' / 'tg-monitor-platform.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open('w')
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(f'another tg-monitor-platform process is already running: {lock_path}') from exc
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _instance_lock_file = lock_file


def _release_instance_lock() -> None:
    global _instance_lock_file
    if not _instance_lock_file:
        return
    try:
        fcntl.flock(_instance_lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        _instance_lock_file.close()
        _instance_lock_file = None

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    _acquire_instance_lock()
    try:
        init_database()
        _migrate_existing_urls()
        await collector.start()
    except Exception:
        _release_instance_lock()
        raise
    yield
    await collector.stop()
    _release_instance_lock()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(AuthMiddleware)
settings.resolved_media_storage_path.mkdir(parents=True, exist_ok=True)
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'app' / 'static')), name='static')
app.mount('/media', StaticFiles(directory=str(settings.resolved_media_storage_path)), name='media')
templates = Jinja2Templates(directory=str(BASE_DIR / 'app' / 'templates'))

_load_i18n()


@app.get('/lang/{code}')
def switch_language(code: str, request: Request):
    # Support ?next= for explicit redirect target (login page etc.)
    next_url = request.query_params.get('next', '')
    if next_url and next_url.startswith('/'):
        target = next_url
    else:
        target = request.headers.get('referer', '/')
    resp = RedirectResponse(target, status_code=303)
    if code in _I18N_DATA:
        resp.set_cookie('lang', code, max_age=86400 * 365)
    return resp


# ==================== Login / Logout ====================


@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    # If already authenticated, redirect to home
    if _check_auth_cookie(request):
        return RedirectResponse('/', status_code=303)

    error = request.query_params.get('error', '')
    error_msg = _t(request, 'login.error_invalid') if error == 'invalid' else ''
    return templates.TemplateResponse(
        request=request,
        name='login.html',
        context={
            'request': request,
            'error': error_msg,
        },
    )


@app.post('/login')
def login_submit(request: Request, password: str = Form(default='')):
    if secrets.compare_digest(password.strip(), settings.auth_password):
        # Validate next is a safe relative path (prevent open redirect)
        next_url = request.query_params.get('next', '/')
        if not next_url.startswith('/'):
            next_url = '/'
        resp = RedirectResponse(next_url, status_code=303)
        # Use secure=True when the request arrived over HTTPS
        is_secure = request.url.scheme == 'https'
        resp.set_cookie(
            'auth_token', _make_auth_token(),
            max_age=86400 * 30,
            httponly=True,
            samesite='lax',
            secure=is_secure,
        )
        return resp
    return RedirectResponse('/login?error=invalid', status_code=303)


@app.get('/logout')
def logout():
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie('auth_token', path='/')
    return resp


def _to_beijing(dt: datetime | None, fmt: str = '%m-%d %H:%M') -> str:
    if dt is None:
        return '-'
    from datetime import timedelta, timezone
    bj = dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return bj.strftime(fmt)


templates.env.filters['to_beijing'] = _to_beijing


def _safe_url(url: str | None) -> str:
    """Validate URL is safe for href attribute (http/https only)."""
    if not url:
        return '#'
    url = url.strip()
    if url.startswith(('http://', 'https://')):
        return url
    return '#'


templates.env.filters['safe_url'] = _safe_url

from jinja2 import pass_context


@pass_context
def _jinja2_t(context, key: str, **kwargs) -> str:
    request = context.get('request')
    if request is None:
        return key
    return _t(request, key, **kwargs)


templates.env.globals['t'] = _jinja2_t
templates.env.globals['get_lang'] = _get_lang


def _migrate_existing_urls() -> None:
    import hashlib
    from sqlalchemy import select
    with session_scope() as db:
        if db.execute(select(func.count(AiUrl.id))).scalar_one():
            return
        existing = db.execute(select(AiSummary).where(
            AiSummary.extracted_urls.isnot(None),
            AiSummary.status == 'success',
        )).scalars().all()
        seen_hashes: set[str] = set()
        to_insert: list[AiUrl] = []
        category_map = {
            'relay_urls': 'relay',
            'seller_urls': 'seller',
            'other_urls': 'other',
        }
        for s in existing:
            urls = s.extracted_urls or {}
            for json_key, category in category_map.items():
                url_list = urls.get(json_key, [])
                if not isinstance(url_list, list):
                    continue
                for url in url_list:
                    if not isinstance(url, str) or not url.strip():
                        continue
                    h = hashlib.sha256(url.encode('utf-8')).hexdigest()
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)
                    row = db.execute(select(AiUrl).where(AiUrl.url_hash == h)).scalar_one_or_none()
                    if row:
                        row.last_seen_at = max(row.last_seen_at, s.completed_at or s.triggered_at)
                    else:
                        to_insert.append(AiUrl(
                            url=url,
                            url_hash=h,
                            category=category,
                            first_seen_at=s.completed_at or s.triggered_at,
                            last_seen_at=s.completed_at or s.triggered_at,
                        ))
        for obj in to_insert:
            db.add(obj)
        if to_insert:
            logger.info('migrated %d existing URLs to ai_urls table', len(to_insert))


@app.get('/', response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as db:
        metrics = dashboard_metrics(db)
        recent_runs = db.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(10)).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name='dashboard.html',
        context={
            'request': request,
            'metrics': metrics,
            'recent_runs': recent_runs,
            'session_mode': settings.telegram_session_mode,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.get('/chats', response_class=HTMLResponse)
def chats_page(request: Request):
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.updated_at.desc())).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name='chats.html',
        context={
            'request': request,
            'chats': chats,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/chats/{chat_id}/toggle')
def toggle_chat(request: Request, chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
        if not chat:
            return redirect_with_message('/chats', _t(request, 'chats.flash_chat_not_found'), 'error')
        chat.is_active = not chat.is_active
    return redirect_with_message('/chats', _t(request, 'chats.flash_status_updated'), 'success')


@app.post('/chats/{chat_id}/backfill')
async def backfill_chat(request: Request, chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
        telegram_id = chat.telegram_id if chat else None
    if not telegram_id:
        return redirect_with_message('/chats', _t(request, 'chats.flash_chat_not_found'), 'error')
    try:
        total = await collector._backfill_one(telegram_id)
        return redirect_with_message('/chats', _t(request, 'chats.flash_backfill_done', count=total), 'success')
    except Exception as exc:
        logger.exception('backfill failed')
        return redirect_with_message('/chats', _t(request, 'chats.flash_backfill_failed', error=exc), 'error')


@app.post('/chats/batch-toggle')
def batch_toggle_chats(request: Request, chat_ids: list[int] = Form(default=[]), action: str = Form(...)):
    if action not in ('enable', 'disable'):
        return redirect_with_message('/chats', _t(request, 'chats.flash_invalid_action'), 'error')
    target = action == 'enable'
    with session_scope() as db:
        updated = db.execute(
            select(MonitoredChat).where(MonitoredChat.id.in_(chat_ids))
        ).scalars().all()
        for chat in updated:
            chat.is_active = target
    action_label = _t(request, 'chats.flash_enable') if target else _t(request, 'chats.flash_disable')
    return redirect_with_message('/chats', _t(request, 'chats.flash_batch_done', action=action_label, count=len(updated)), 'success')


@app.post('/chats/toggle-all')
def toggle_all_chats(request: Request, action: str = Form(...)):
    if action not in ('enable', 'disable'):
        return redirect_with_message('/chats', _t(request, 'chats.flash_invalid_action'), 'error')
    target = action == 'enable'
    with session_scope() as db:
        updated = db.execute(select(MonitoredChat)).scalars().all()
        for chat in updated:
            chat.is_active = target
    action_label = _t(request, 'chats.flash_enable') if target else _t(request, 'chats.flash_disable')
    return redirect_with_message('/chats', _t(request, 'chats.flash_all_done', action=action_label, count=len(updated)), 'success')


@app.post('/sync/dialogs')
async def sync_dialogs(request: Request):
    try:
        count = await collector.sync_dialogs()
        return redirect_with_message('/chats', _t(request, 'chats.flash_sync_done', count=count), 'success')
    except Exception as exc:
        logger.exception('sync dialogs failed')
        return redirect_with_message('/chats', _t(request, 'chats.flash_sync_failed', error=exc), 'error')


@app.get('/messages', response_class=HTMLResponse)
def messages_page(request: Request, chat_id: str | None = None, keyword: str | None = None, media_only: bool = False, sender: str | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        query = select(Message)
        if selected_chat_id:
            query = query.where(Message.chat_id == selected_chat_id)
        if keyword:
            query = query.where(Message.normalized_text.like(f'%{keyword}%'))
        if media_only:
            query = query.where(Message.has_media.is_(True))
        if sender:
            sender_user = db.execute(select(TelegramUser).where(
                (TelegramUser.username == sender) | (TelegramUser.first_name == sender)
            )).scalar_one_or_none()
            if sender_user:
                query = query.where(Message.sender_user_id == sender_user.id)

        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        messages = db.execute(
            query.order_by(Message.message_date.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        senders = {
            sender.id: sender for sender in db.execute(select(TelegramUser).where(TelegramUser.id.in_([m.sender_user_id for m in messages if m.sender_user_id]))).scalars().all()
        }
        chat_titles = {chat.id: chat.title for chat in chats}
        grouped_rows: dict[str, dict] = {}
        ordered_group_keys: list[str] = []
        for item in messages:
            meta = item.meta_json or {}
            grouped_id = meta.get('grouped_id')
            group_key = f'g:{item.chat_id}:{grouped_id}' if grouped_id else f'm:{item.id}'
            media_path = meta.get('media_path')
            media_item = {
                'media_url': f"/media/{media_path}" if media_path else '',
                'media_name': meta.get('media_name', ''),
                'media_is_image': bool(meta.get('media_is_image')),
                'media_is_video': bool(meta.get('media_is_video')),
                'media_type': item.media_type or '',
            }
            if group_key not in grouped_rows:
                grouped_rows[group_key] = {
                    'message': item,
                    'chat_title': chat_titles.get(item.chat_id, str(item.chat_id)),
                    'sender_name': (
                        senders[item.sender_user_id].username or senders[item.sender_user_id].first_name or str(senders[item.sender_user_id].telegram_id)
                    ) if item.sender_user_id and item.sender_user_id in senders else 'Unknown',
                    'display_text': item.raw_text or '',
                    'media_items': [media_item] if media_item['media_url'] or media_item['media_type'] else [],
                    'grouped_id': grouped_id,
                }
                ordered_group_keys.append(group_key)
            else:
                row = grouped_rows[group_key]
                if item.raw_text and not row['display_text']:
                    row['display_text'] = item.raw_text
                    row['message'] = item
                if media_item['media_url'] or media_item['media_type']:
                    row['media_items'].append(media_item)
        message_rows = [grouped_rows[key] for key in ordered_group_keys]
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='messages.html',
        context={
            'request': request,
            'messages': message_rows,
            'chats': chats,
            'selected_chat_id': selected_chat_id,
            'keyword': keyword or '',
            'media_only': media_only,
            'sender': sender or '',
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.get('/settings', response_class=HTMLResponse)
async def settings_page(request: Request):
    session_metadata = telegram_session_manager.get_session_metadata()
    me = session_metadata.get('account') if session_metadata else None

    # Show opentele fallback credentials as available on the page
    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if not api_id or not api_hash:
        try:
            from opentele.api import API
            api = API.TelegramDesktop()
            api_id = api.api_id
            api_hash = api.api_hash
        except Exception:
            pass

    with session_scope() as db:
        ai_provider_row = db.execute(select(AppSetting).where(AppSetting.key == 'ai_provider')).scalar_one_or_none()
        ai_key_row = db.execute(select(AppSetting).where(AppSetting.key == 'ai_api_key')).scalar_one_or_none()
        ai_base_url_row = db.execute(select(AppSetting).where(AppSetting.key == 'ai_base_url')).scalar_one_or_none()
        ai_model_row = db.execute(select(AppSetting).where(AppSetting.key == 'ai_model')).scalar_one_or_none()
        url_classification_prompt = get_url_classification_prompt(db)
        # Legacy migration: read old keys if new ones don't exist
        if not ai_key_row:
            old_key = db.execute(select(AppSetting).where(AppSetting.key == 'deepseek_api_key')).scalar_one_or_none()
            if old_key and old_key.value:
                db.add(AppSetting(key='ai_api_key', value=old_key.value))
                ai_key_row = old_key
        if not ai_model_row:
            old_model = db.execute(select(AppSetting).where(AppSetting.key == 'deepseek_model')).scalar_one_or_none()
            if old_model and old_model.value:
                db.add(AppSetting(key='ai_model', value=old_model.value))
                ai_model_row = old_model

    ai_provider = ai_provider_row.value if ai_provider_row else 'deepseek'
    provider_config = PROVIDER_CONFIGS.get(ai_provider, PROVIDER_CONFIGS['deepseek'])
    ai_base_url = ai_base_url_row.value if ai_base_url_row else provider_config.get('base_url', '')

    return templates.TemplateResponse(
        request=request,
        name='settings.html',
        context={
            'request': request,
            'app_name': settings.app_name,
            'app_host': settings.app_host,
            'app_port': settings.app_port,
            'app_debug': settings.app_debug,
            'session_mode': settings.telegram_session_mode,
            'desktop_import_enabled': settings.telegram_desktop_import_enabled,
            'desktop_import_mode': settings.telegram_desktop_import_mode,
            'live_listener_enabled': settings.telegram_live_listener_enabled,
            'background_collection_enabled': settings.telegram_background_collection_enabled,
            'download_media_enabled': settings.telegram_download_media_enabled,
            'fetch_user_about_enabled': settings.telegram_fetch_user_about_enabled,
            'sync_interval_minutes': settings.sync_interval_minutes,
            'sync_batch_size': settings.sync_batch_size,
            'sync_lookback_messages': settings.sync_lookback_messages,
            'ai_summary_batch_size': settings.ai_summary_batch_size,
            'ai_summary_running_timeout_minutes': settings.ai_summary_running_timeout_minutes,
            'url_classification_enabled': settings.url_classification_enabled,
            'url_classification_interval_minutes': settings.url_classification_interval_minutes,
            'url_classification_batch_size': settings.url_classification_batch_size,
            'key_lead_analysis_enabled': settings.key_lead_analysis_enabled,
            'key_lead_analysis_interval_minutes': settings.key_lead_analysis_interval_minutes,
            'key_lead_analysis_batch_size': settings.key_lead_analysis_batch_size,
            'analysis_top_keywords': settings.analysis_top_keywords,
            'media_storage_path': settings.media_storage_path,
            'stopwords_extra': settings.stopwords_extra,
            'database_host': settings.database_host,
            'database_port': settings.database_port,
            'database_name': settings.database_name,
            'tdata_path': str(settings.resolved_tdata_path),
            'session_path': str(settings.resolved_session_path),
            'telegram_connected': bool(settings.resolved_session_path.exists() and session_metadata),
            'me': me,
            'pending_manual_login_phone': telegram_session_manager.pending_manual_login_phone,
            'api_id_present': bool(api_id),
            'api_hash_present': bool(api_hash),
            'ai_api_key_set': bool(ai_key_row and ai_key_row.value),
            'ai_provider': ai_provider,
            'ai_providers': PROVIDER_CONFIGS,
            'ai_model': ai_model_row.value if ai_model_row else provider_config.get('default_model', ''),
            'ai_base_url': ai_base_url,
            'url_classification_prompt': url_classification_prompt,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/settings/manual-login')
async def manual_login(request: Request, phone: str = Form(default=''), code: str = Form(default=''), password: str = Form(default='')):
    try:
        phone = phone.strip()
        code = code.strip()
        if not code:
            if not phone:
                return redirect_with_message('/settings', _t(request, 'settings.enter_phone'), 'error')
            await telegram_session_manager.start_manual_login(phone=phone)
            return redirect_with_message('/settings', _t(request, 'settings.code_sent'), 'success')
        await telegram_session_manager.complete_manual_login(code=code, password=password or None)
        return redirect_with_message('/settings', _t(request, 'settings.login_success'), 'success')
    except Exception as exc:
        logger.exception('manual login failed')
        return redirect_with_message('/settings', _t(request, 'settings.login_failed', error=exc), 'error')


@app.post('/settings/connect')
async def connect_session(request: Request):
    if not settings.telegram_desktop_import_enabled:
        return redirect_with_message('/settings', _t(request, 'settings.desktop_import_disabled'), 'error')
    try:
        client = await telegram_session_manager.connect(allow_desktop_import=True)
        if client:
            return redirect_with_message('/settings', _t(request, 'settings.session_connected'), 'success')
        return redirect_with_message('/settings', _t(request, 'settings.session_connect_failed'), 'error')
    except Exception as exc:
        logger.exception('connect session failed')
        return redirect_with_message('/settings', _t(request, 'settings.connect_failed', error=exc), 'error')


@app.post('/settings/ai-config')
def save_ai_config(
    request: Request,
    ai_provider: str = Form(default='deepseek'),
    ai_api_key: str = Form(default=''),
    ai_base_url: str = Form(default=''),
    ai_model: str = Form(default=''),
    url_classification_prompt: str = Form(default=''),
):
    if ai_provider not in PROVIDER_CONFIGS:
        ai_provider = 'deepseek'
    provider_default = PROVIDER_CONFIGS[ai_provider]
    if not ai_model.strip():
        ai_model = provider_default.get('default_model', '')

    def _upsert_setting(db, key: str, value: str):
        row = db.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))

    with session_scope() as db:
        _upsert_setting(db, 'ai_provider', ai_provider)
        if ai_api_key.strip() and ai_api_key.strip() != '••••••••':
            _upsert_setting(db, 'ai_api_key', ai_api_key.strip())
        _upsert_setting(db, 'ai_base_url', ai_base_url.strip())
        _upsert_setting(db, 'ai_model', ai_model.strip())
        _upsert_setting(db, 'url_classification_prompt', url_classification_prompt.strip())
    return redirect_with_message('/settings', _t(request, 'settings.ai_saved'), 'success')


@app.post('/settings/runtime-config')
async def save_runtime_config(
    request: Request,
    desktop_import_mode: str = Form(default='create_new'),
    background_collection_enabled: bool = Form(default=False),
    live_listener_enabled: bool = Form(default=False),
    download_media_enabled: bool = Form(default=False),
    fetch_user_about_enabled: bool = Form(default=False),
    sync_interval_minutes: int = Form(default=5),
    sync_batch_size: int = Form(default=200),
    sync_lookback_messages: int = Form(default=1000),
    ai_summary_batch_size: int = Form(default=100),
    ai_summary_running_timeout_minutes: int = Form(default=30),
    url_classification_enabled: bool = Form(default=False),
    url_classification_interval_minutes: int = Form(default=30),
    url_classification_batch_size: int = Form(default=50),
    key_lead_analysis_enabled: bool = Form(default=False),
    key_lead_analysis_interval_minutes: int = Form(default=30),
    key_lead_analysis_batch_size: int = Form(default=200),
):
    if desktop_import_mode != 'create_new':
        return redirect_with_message('/settings', _t(request, 'settings.invalid_import_mode'), 'error')

    sync_interval_minutes = min(max(sync_interval_minutes, 1), 1440)
    sync_batch_size = min(max(sync_batch_size, 20), 1000)
    sync_lookback_messages = min(max(sync_lookback_messages, 100), 10000)
    ai_summary_batch_size = min(max(ai_summary_batch_size, 20), 1000)
    ai_summary_running_timeout_minutes = min(max(ai_summary_running_timeout_minutes, 5), 1440)
    url_classification_interval_minutes = min(max(url_classification_interval_minutes, 1), 1440)
    url_classification_batch_size = min(max(url_classification_batch_size, 1), 200)
    key_lead_analysis_interval_minutes = min(max(key_lead_analysis_interval_minutes, 1), 1440)
    key_lead_analysis_batch_size = min(max(key_lead_analysis_batch_size, 1), 500)

    _apply_settings({
        'telegram_desktop_import_mode': desktop_import_mode,
        'telegram_background_collection_enabled': background_collection_enabled,
        'telegram_live_listener_enabled': live_listener_enabled,
        'telegram_download_media_enabled': download_media_enabled,
        'telegram_fetch_user_about_enabled': fetch_user_about_enabled,
        'sync_interval_minutes': sync_interval_minutes,
        'sync_batch_size': sync_batch_size,
        'sync_lookback_messages': sync_lookback_messages,
        'ai_summary_batch_size': ai_summary_batch_size,
        'ai_summary_running_timeout_minutes': ai_summary_running_timeout_minutes,
        'url_classification_enabled': url_classification_enabled,
        'url_classification_interval_minutes': url_classification_interval_minutes,
        'url_classification_batch_size': url_classification_batch_size,
        'key_lead_analysis_enabled': key_lead_analysis_enabled,
        'key_lead_analysis_interval_minutes': key_lead_analysis_interval_minutes,
        'key_lead_analysis_batch_size': key_lead_analysis_batch_size,
    })
    _update_env_file({
        'TELEGRAM_DESKTOP_IMPORT_MODE': desktop_import_mode,
        'TELEGRAM_BACKGROUND_COLLECTION_ENABLED': _env_bool(background_collection_enabled),
        'TELEGRAM_LIVE_LISTENER_ENABLED': _env_bool(live_listener_enabled),
        'TELEGRAM_DOWNLOAD_MEDIA_ENABLED': _env_bool(download_media_enabled),
        'TELEGRAM_FETCH_USER_ABOUT_ENABLED': _env_bool(fetch_user_about_enabled),
        'SYNC_INTERVAL_MINUTES': str(sync_interval_minutes),
        'SYNC_BATCH_SIZE': str(sync_batch_size),
        'SYNC_LOOKBACK_MESSAGES': str(sync_lookback_messages),
        'AI_SUMMARY_BATCH_SIZE': str(ai_summary_batch_size),
        'AI_SUMMARY_RUNNING_TIMEOUT_MINUTES': str(ai_summary_running_timeout_minutes),
        'URL_CLASSIFICATION_ENABLED': _env_bool(url_classification_enabled),
        'URL_CLASSIFICATION_INTERVAL_MINUTES': str(url_classification_interval_minutes),
        'URL_CLASSIFICATION_BATCH_SIZE': str(url_classification_batch_size),
        'KEY_LEAD_ANALYSIS_ENABLED': _env_bool(key_lead_analysis_enabled),
        'KEY_LEAD_ANALYSIS_INTERVAL_MINUTES': str(key_lead_analysis_interval_minutes),
        'KEY_LEAD_ANALYSIS_BATCH_SIZE': str(key_lead_analysis_batch_size),
    })
    await collector.apply_runtime_config()
    return redirect_with_message('/settings', _t(request, 'settings.collection_saved'), 'success')


@app.post('/settings/app-config')
def save_app_config(
    request: Request,
    app_name: str = Form(default='TG Monitor Platform'),
    app_host: str = Form(default='127.0.0.1'),
    app_port: int = Form(default=8098),
    analysis_top_keywords: int = Form(default=30),
    media_storage_path: str = Form(default='./data/media'),
    stopwords_extra: str = Form(default=''),
):
    app_port = min(max(app_port, 1), 65535)
    analysis_top_keywords = min(max(analysis_top_keywords, 5), 100)

    _apply_settings({
        'app_name': app_name.strip() or 'TG Monitor Platform',
        'app_host': app_host.strip() or '127.0.0.1',
        'app_port': app_port,
        'analysis_top_keywords': analysis_top_keywords,
        'media_storage_path': media_storage_path.strip() or './data/media',
        'stopwords_extra': stopwords_extra.strip(),
    })
    _update_env_file({
        'APP_NAME': app_name.strip() or 'TG Monitor Platform',
        'APP_HOST': app_host.strip() or '127.0.0.1',
        'APP_PORT': str(app_port),
        'ANALYSIS_TOP_KEYWORDS': str(analysis_top_keywords),
        'MEDIA_STORAGE_PATH': media_storage_path.strip() or './data/media',
        'STOPWORDS_EXTRA': stopwords_extra.strip(),
    })
    return redirect_with_message('/settings', _t(request, 'settings.app_saved'), 'success')


@app.get('/summaries', response_class=HTMLResponse)
def summaries_page(request: Request, chat_id: str | None = None, page: int = 1):
    page_size = 20
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        query = select(AiSummary)
        if selected_chat_id:
            query = query.where(AiSummary.chat_id == selected_chat_id)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        summaries = db.execute(
            query.order_by(AiSummary.id.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        chat_map = {c.id: c.title for c in chats}
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='summaries.html',
        context={
            'request': request,
            'summaries': summaries,
            'chats': chats,
            'selected_chat_id': selected_chat_id,
            'chat_map': chat_map,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


CATEGORY_KEYS = {'relay': 'urls.relay', 'seller': 'urls.seller', 'other': 'urls.other'}
CATEGORY_ORDER = ['relay', 'seller', 'other']


@app.get('/urls', response_class=HTMLResponse)
def urls_page(
    request: Request,
    category: str | None = None,
    ai_category_id: str | None = None,
    classification_status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
):
    page_size = 50
    page = max(page, 1)
    selected_ai_category_id = _parse_optional_int(ai_category_id)
    valid_statuses = {'pending', 'running', 'classified', 'failed'}
    with session_scope() as db:
        query = select(AiUrl).order_by(AiUrl.last_seen_at.desc())
        if category in CATEGORY_KEYS:
            query = query.where(AiUrl.category == category)
        if selected_ai_category_id:
            query = query.where(AiUrl.primary_category_id == selected_ai_category_id)
        if classification_status in valid_statuses:
            query = query.where(AiUrl.classification_status == classification_status)
        if keyword and keyword.strip():
            like = f'%{keyword.strip()}%'
            query = query.where((AiUrl.url.like(like)) | (AiUrl.domain.like(like)))
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        urls = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        counts = {}
        for cat in CATEGORY_ORDER:
            counts[cat] = db.execute(
                select(func.count(AiUrl.id)).where(AiUrl.category == cat)
            ).scalar_one()
        domain_stats = analysis.domain_frequency_stats(db, limit=10)
        reputation = analysis.url_reputation_summary(db)
        cross_chat = analysis.cross_chat_urls(db, limit=10)
        ai_categories = db.execute(
            select(AiUrlCategory).where(AiUrlCategory.is_active.is_(True)).order_by(AiUrlCategory.name.asc())
        ).scalars().all()
        ai_category_map = {c.id: c for c in ai_categories}
        ai_category_counts = {
            cid: count for cid, count in db.execute(
                select(AiUrl.primary_category_id, func.count(AiUrl.id))
                .where(AiUrl.primary_category_id.isnot(None))
                .group_by(AiUrl.primary_category_id)
            ).all()
        }
        status_counts = {
            status or 'pending': count for status, count in db.execute(
                select(AiUrl.classification_status, func.count(AiUrl.id))
                .group_by(AiUrl.classification_status)
            ).all()
        }
        recent_classification_runs = db.execute(
            select(AiUrlClassificationRun).order_by(AiUrlClassificationRun.id.desc()).limit(5)
        ).scalars().all()
        pending_classification_count = db.execute(
            select(func.count(AiUrl.id)).where(
                (AiUrl.classification_status.is_(None)) |
                (AiUrl.classification_status.in_(('pending', 'failed')))
            )
        ).scalar_one()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='urls.html',
        context={
            'request': request,
            'urls': urls,
            'selected_category': category,
            'selected_ai_category_id': selected_ai_category_id,
            'selected_classification_status': classification_status,
            'keyword': keyword or '',
            'counts': counts,
            'labels': {k: _t(request, v) for k, v in CATEGORY_KEYS.items()},
            'order': CATEGORY_ORDER,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'domain_stats': domain_stats,
            'reputation': reputation,
            'cross_chat_urls': cross_chat,
            'ai_categories': ai_categories,
            'ai_category_map': ai_category_map,
            'ai_category_counts': ai_category_counts,
            'status_counts': status_counts,
            'recent_classification_runs': recent_classification_runs,
            'pending_classification_count': pending_classification_count,
            'url_classification_batch_size': settings.url_classification_batch_size,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/urls/classify-now')
async def classify_urls_now(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_size: int = Form(default=50),
    include_classified: bool = Form(default=False),
):
    batch_size = min(max(batch_size, 1), 200)
    background_tasks.add_task(_run_url_classification_background, batch_size, include_classified)
    return redirect_with_message('/urls', f'URL 分类任务已提交，后台将处理最多 {batch_size} 条', 'info')


@app.get('/chats/{chat_id}', response_class=HTMLResponse)
def chat_detail_page(request: Request, chat_id: int, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
        if not chat:
            return redirect_with_message('/chats', _t(request, 'chat_detail.flash_chat_not_found'), 'error')
        stats = chat_statistics(db, chat_id)
        query = select(Message).where(Message.chat_id == chat_id)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        messages = db.execute(
            query.order_by(Message.message_date.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        senders = {
            s.id: s for s in db.execute(
                select(TelegramUser).where(TelegramUser.id.in_([m.sender_user_id for m in messages if m.sender_user_id]))
            ).scalars().all()
        }
        summaries = db.execute(
            select(AiSummary).where(AiSummary.chat_id == chat_id).order_by(AiSummary.id.desc()).limit(5)
        ).scalars().all()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    message_rows = []
    for item in messages:
        sender_name = 'Unknown'
        if item.sender_user_id and item.sender_user_id in senders:
            s = senders[item.sender_user_id]
            sender_name = s.username or s.first_name or str(s.telegram_id)
        message_rows.append({'message': item, 'sender_name': sender_name})
    return templates.TemplateResponse(
        request=request,
        name='chat_detail.html',
        context={
            'request': request,
            'chat': chat,
            'stats': stats,
            'messages': message_rows,
            'summaries': summaries,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/chats/{chat_id}/sync')
async def manual_sync_chat(request: Request, chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
        telegram_id = chat.telegram_id if chat else None
    if not telegram_id:
        return redirect_with_message(f'/chats/{chat_id}', _t(request, 'chat_detail.flash_chat_not_found'), 'error')
    try:
        total = await collector._backfill_one(telegram_id)
        return redirect_with_message(f'/chats/{chat_id}', _t(request, 'chat_detail.flash_sync_done', count=total), 'success')
    except Exception as exc:
        logger.exception('manual sync failed for chat %s', chat_id)
        return redirect_with_message(f'/chats/{chat_id}', _t(request, 'chat_detail.flash_sync_failed', error=exc), 'error')


@app.post('/chats/{chat_id}/summarize')
async def trigger_chat_summary(request: Request, background_tasks: BackgroundTasks, chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
    if not chat:
        return redirect_with_message('/chats', _t(request, 'chat_detail.flash_chat_not_found'), 'error')
    background_tasks.add_task(_run_summary_now_background, chat_id, 'chat_detail')
    return redirect_with_message(f'/chats/{chat_id}', 'AI 总结任务已提交，后台处理中', 'info')


@app.post('/summaries/{summary_id}/delete')
def delete_summary(request: Request, summary_id: int):
    with session_scope() as db:
        summary = db.get(AiSummary, summary_id)
        if summary:
            db.delete(summary)
    return redirect_with_message('/summaries', _t(request, 'summaries.flash_deleted'), 'success')


@app.post('/summaries/{summary_id}/rerun')
async def rerun_summary(request: Request, background_tasks: BackgroundTasks, summary_id: int):
    with session_scope() as db:
        summary = db.get(AiSummary, summary_id)
        if not summary:
            return redirect_with_message('/summaries', _t(request, 'summaries.flash_not_found'), 'error')
        chat_id = summary.chat_id
    background_tasks.add_task(_run_summary_now_background, chat_id, 'summaries_rerun')
    return redirect_with_message('/summaries', 'AI 总结重跑任务已提交，后台处理中', 'info')


@app.get('/status', response_class=HTMLResponse)
def status_page(request: Request):
    with session_scope() as db:
        status = system_status(db)
    return templates.TemplateResponse(
        request=request,
        name='status.html',
        context={
            'request': request,
            'status': status,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.get('/api/dashboard')
def dashboard_api():
    with session_scope() as db:
        return JSONResponse(dashboard_metrics(db))


# ==================== Products Page ====================

PRODUCT_STATUS_KEYS = {'available': 'common.available', 'sold': 'common.sold', 'reserved': 'common.reserved'}
PRODUCT_STATUS_ORDER = ['available', 'sold', 'reserved']


@app.get('/products', response_class=HTMLResponse)
def products_page(request: Request, status: str | None = None, chat_id: str | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        query = select(AiProduct).order_by(AiProduct.last_seen_at.desc())
        if status in PRODUCT_STATUS_KEYS:
            query = query.where(AiProduct.status == status)
        if selected_chat_id:
            query = query.where(AiProduct.chat_id == selected_chat_id)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        products = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        counts = {}
        for s in PRODUCT_STATUS_ORDER:
            counts[s] = db.execute(
                select(func.count(AiProduct.id)).where(AiProduct.status == s)
            ).scalar_one()
        chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='products.html',
        context={
            'request': request,
            'products': products,
            'selected_status': status,
            'selected_chat_id': selected_chat_id,
            'counts': counts,
            'labels': {k: _t(request, v) for k, v in PRODUCT_STATUS_KEYS.items()},
            'order': PRODUCT_STATUS_ORDER,
            'chats': chats,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


# ==================== Contacts Page ====================

CONTACT_TYPE_KEYS = {
    'tg_user': 'contacts.tg_user', 'tg_group': 'contacts.tg_group',
    'email': 'contacts.email', 'phone': 'contacts.phone', 'other': 'contacts.other',
}
CONTACT_TYPE_ORDER = ['tg_user', 'tg_group', 'email', 'phone', 'other']


@app.get('/api/urls')
def api_urls(
    category: str | None = None,
    ai_category_id: str | None = None,
    ai_category_slug: str | None = None,
    classification_status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    page, page_size = _normalize_pagination(page, page_size)
    parsed_ai_category_id = _parse_optional_int(ai_category_id)
    valid_statuses = {'pending', 'running', 'classified', 'failed'}
    with session_scope() as db:
        resolved_ai_category_id = parsed_ai_category_id
        if ai_category_slug and not resolved_ai_category_id:
            category_row = db.execute(
                select(AiUrlCategory).where(AiUrlCategory.slug == ai_category_slug)
            ).scalar_one_or_none()
            resolved_ai_category_id = category_row.id if category_row else -1

        query = select(AiUrl).order_by(AiUrl.last_seen_at.desc())
        if category in CATEGORY_KEYS:
            query = query.where(AiUrl.category == category)
        if resolved_ai_category_id:
            query = query.where(AiUrl.primary_category_id == resolved_ai_category_id)
        if classification_status in valid_statuses:
            query = query.where(AiUrl.classification_status == classification_status)
        if keyword and keyword.strip():
            like = f'%{keyword.strip()}%'
            query = query.where((AiUrl.url.like(like)) | (AiUrl.domain.like(like)))

        total = _query_total(db, query)
        urls = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        category_ids = {u.primary_category_id for u in urls if u.primary_category_id}
        ai_categories = {}
        if category_ids:
            ai_categories = {
                c.id: c for c in db.execute(
                    select(AiUrlCategory).where(AiUrlCategory.id.in_(category_ids))
                ).scalars().all()
            }

        items = []
        for url in urls:
            primary_category = ai_categories.get(url.primary_category_id)
            items.append({
                'id': url.id,
                'url': url.url,
                'domain': url.domain,
                'category': url.category,
                'appearance_count': url.appearance_count,
                'chat_ids_seen': url.chat_ids_seen,
                'reputation_score': url.reputation_score,
                'classification_status': url.classification_status or 'pending',
                'primary_category': {
                    'id': primary_category.id,
                    'slug': primary_category.slug,
                    'name': primary_category.name,
                    'description': primary_category.description,
                    'source': primary_category.source,
                } if primary_category else None,
                'classification_run_id': url.classification_run_id,
                'classified_at': _iso_dt(url.classified_at),
                'classification_error': url.classification_error,
                'first_seen_at': _iso_dt(url.first_seen_at),
                'last_seen_at': _iso_dt(url.last_seen_at),
            })

    return {'pagination': _pagination_meta(total, page, page_size), 'items': items}


@app.get('/api/messages')
def api_messages(
    chat_id: str | None = None,
    keyword: str | None = None,
    media_only: bool = False,
    sender: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    page, page_size = _normalize_pagination(page, page_size)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        query = select(Message).order_by(Message.message_date.desc())
        if selected_chat_id:
            query = query.where(Message.chat_id == selected_chat_id)
        if keyword and keyword.strip():
            query = query.where(Message.normalized_text.like(f'%{keyword.strip()}%'))
        if media_only:
            query = query.where(Message.has_media.is_(True))
        if sender and sender.strip():
            sender_value = sender.strip()
            sender_user = db.execute(select(TelegramUser).where(
                (TelegramUser.username == sender_value) | (TelegramUser.first_name == sender_value)
            )).scalar_one_or_none()
            query = query.where(Message.sender_user_id == (sender_user.id if sender_user else -1))

        total = _query_total(db, query)
        messages = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        chat_ids = {m.chat_id for m in messages}
        sender_ids = {m.sender_user_id for m in messages if m.sender_user_id}
        chats = {
            c.id: c for c in db.execute(select(MonitoredChat).where(MonitoredChat.id.in_(chat_ids))).scalars().all()
        } if chat_ids else {}
        senders = {
            s.id: s for s in db.execute(select(TelegramUser).where(TelegramUser.id.in_(sender_ids))).scalars().all()
        } if sender_ids else {}

        items = [{
            'id': message.id,
            'chat': _chat_payload(chats.get(message.chat_id)),
            'sender': _sender_payload(senders.get(message.sender_user_id)),
            'telegram_message_id': message.telegram_message_id,
            'message_date': _iso_dt(message.message_date),
            'edit_date': _iso_dt(message.edit_date),
            'raw_text': message.raw_text,
            'normalized_text': message.normalized_text,
            'reply_to_msg_id': message.reply_to_msg_id,
            'views': message.views,
            'forwards': message.forwards,
            'has_media': message.has_media,
            'media_type': message.media_type,
            'meta_json': message.meta_json,
            'created_at': _iso_dt(message.created_at),
        } for message in messages]

    return {'pagination': _pagination_meta(total, page, page_size), 'items': items}


@app.get('/api/products')
def api_products(
    status: str | None = None,
    chat_id: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    page, page_size = _normalize_pagination(page, page_size)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        query = select(AiProduct).order_by(AiProduct.last_seen_at.desc())
        if status in PRODUCT_STATUS_KEYS:
            query = query.where(AiProduct.status == status)
        if selected_chat_id:
            query = query.where(AiProduct.chat_id == selected_chat_id)
        if keyword and keyword.strip():
            like = f'%{keyword.strip()}%'
            query = query.where((AiProduct.product_name.like(like)) | (AiProduct.seller_contact.like(like)))

        total = _query_total(db, query)
        products = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        chat_ids = {p.chat_id for p in products}
        chats = {
            c.id: c for c in db.execute(select(MonitoredChat).where(MonitoredChat.id.in_(chat_ids))).scalars().all()
        } if chat_ids else {}

        items = [{
            'id': product.id,
            'chat': _chat_payload(chats.get(product.chat_id)),
            'summary_id': product.summary_id,
            'product_name': product.product_name,
            'price_amount': product.price_amount,
            'price_currency': product.price_currency,
            'seller_contact': product.seller_contact,
            'status': product.status,
            'first_seen_at': _iso_dt(product.first_seen_at),
            'last_seen_at': _iso_dt(product.last_seen_at),
        } for product in products]

    return {'pagination': _pagination_meta(total, page, page_size), 'items': items}


@app.get('/api/contacts')
def api_contacts(
    contact_type: str | None = None,
    chat_id: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    page, page_size = _normalize_pagination(page, page_size)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        query = select(AiContact).order_by(AiContact.last_seen_at.desc())
        if contact_type in CONTACT_TYPE_KEYS:
            query = query.where(AiContact.contact_type == contact_type)
        if selected_chat_id:
            query = query.where(AiContact.chat_id == selected_chat_id)
        if keyword and keyword.strip():
            like = f'%{keyword.strip()}%'
            query = query.where((AiContact.contact_value.like(like)) | (AiContact.context.like(like)))

        total = _query_total(db, query)
        contacts = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        chat_ids = {c.chat_id for c in contacts}
        chats = {
            c.id: c for c in db.execute(select(MonitoredChat).where(MonitoredChat.id.in_(chat_ids))).scalars().all()
        } if chat_ids else {}

        items = [{
            'id': contact.id,
            'chat': _chat_payload(chats.get(contact.chat_id)),
            'summary_id': contact.summary_id,
            'contact_type': contact.contact_type,
            'contact_value': contact.contact_value,
            'context': contact.context,
            'first_seen_at': _iso_dt(contact.first_seen_at),
            'last_seen_at': _iso_dt(contact.last_seen_at),
        } for contact in contacts]

    return {'pagination': _pagination_meta(total, page, page_size), 'items': items}


@app.get('/contacts', response_class=HTMLResponse)
def contacts_page(request: Request, contact_type: str | None = None, chat_id: str | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    with session_scope() as db:
        query = select(AiContact).order_by(AiContact.last_seen_at.desc())
        if contact_type in CONTACT_TYPE_KEYS:
            query = query.where(AiContact.contact_type == contact_type)
        if selected_chat_id:
            query = query.where(AiContact.chat_id == selected_chat_id)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        contacts = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        counts = {}
        for t in CONTACT_TYPE_ORDER:
            counts[t] = db.execute(
                select(func.count(AiContact.id)).where(AiContact.contact_type == t)
            ).scalar_one()
        chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='contacts.html',
        context={
            'request': request,
            'contacts': contacts,
            'selected_type': contact_type,
            'selected_chat_id': selected_chat_id,
            'counts': counts,
            'labels': {k: _t(request, v) for k, v in CONTACT_TYPE_KEYS.items()},
            'order': CONTACT_TYPE_ORDER,
            'chats': chats,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.get('/key-leads', response_class=HTMLResponse)
def key_leads_page(
    request: Request,
    keyword: str | None = None,
    provider: str | None = None,
    lead_type: str | None = None,
    chat_id: str | None = None,
    page: int = 1,
):
    page_size = 50
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    valid_types = {'api_key', 'free_credit_account'}
    with session_scope() as db:
        query = select(AiKeyLead).order_by(AiKeyLead.last_seen_at.desc())
        if provider and provider.strip():
            query = query.where(AiKeyLead.provider == provider.strip())
        if lead_type in valid_types:
            query = query.where(AiKeyLead.lead_type == lead_type)
        if selected_chat_id:
            query = query.where(AiKeyLead.chat_id == selected_chat_id)
        if keyword and keyword.strip():
            like = f'%{keyword.strip()}%'
            query = query.where(
                (AiKeyLead.product_name.like(like)) |
                (AiKeyLead.offer_text.like(like)) |
                (AiKeyLead.seller_contact.like(like)) |
                (AiKeyLead.source_text.like(like)) |
                (AiKeyLead.reason.like(like))
            )

        total_count = _query_total(db, query)
        leads = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        provider_counts = {
            key or 'other': count for key, count in db.execute(
                select(AiKeyLead.provider, func.count(AiKeyLead.id)).group_by(AiKeyLead.provider)
            ).all()
        }
        type_counts = {
            key: count for key, count in db.execute(
                select(AiKeyLead.lead_type, func.count(AiKeyLead.id)).group_by(AiKeyLead.lead_type)
            ).all()
        }
        recent_runs = db.execute(select(AiKeyLeadRun).order_by(AiKeyLeadRun.id.desc()).limit(5)).scalars().all()
        chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True)).order_by(MonitoredChat.title.asc())).scalars().all()
        chat_ids = {lead.chat_id for lead in leads}
        sender_ids = {lead.sender_user_id for lead in leads if lead.sender_user_id}
        chat_map = {
            chat.id: chat for chat in db.execute(select(MonitoredChat).where(MonitoredChat.id.in_(chat_ids))).scalars().all()
        } if chat_ids else {}
        sender_map = {
            sender.id: sender for sender in db.execute(select(TelegramUser).where(TelegramUser.id.in_(sender_ids))).scalars().all()
        } if sender_ids else {}
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='key_leads.html',
        context={
            'request': request,
            'leads': leads,
            'provider_counts': provider_counts,
            'type_counts': type_counts,
            'recent_runs': recent_runs,
            'chats': chats,
            'chat_map': chat_map,
            'sender_map': sender_map,
            'keyword': keyword or '',
            'selected_provider': provider or '',
            'selected_type': lead_type or '',
            'selected_chat_id': selected_chat_id,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/key-leads/analyze-now')
async def analyze_key_leads_now(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_size: int = Form(default=200),
):
    batch_size = min(max(batch_size, 1), 500)
    background_tasks.add_task(_run_key_lead_analysis_background, batch_size)
    return redirect_with_message('/key-leads', f'Key 商线索分析任务已提交，后台将扫描最多 {batch_size} 条消息', 'info')


# ==================== Alerts Page ====================

@app.get('/alerts', response_class=HTMLResponse)
def alerts_page(request: Request, rule_id: int | None = None, chat_id: int | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        rules = db.execute(select(AlertRule).order_by(AlertRule.created_at.desc())).scalars().all()
        query = select(AlertMatch).order_by(AlertMatch.matched_at.desc())
        if rule_id:
            query = query.where(AlertMatch.rule_id == rule_id)
        if chat_id:
            query = query.where(AlertMatch.chat_id == chat_id)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        matches = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
        unread_count = db.execute(
            select(func.count(AlertMatch.id)).where(AlertMatch.is_read.is_(False))
        ).scalar_one()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='alerts.html',
        context={
            'request': request,
            'rules': rules,
            'matches': matches,
            'selected_rule_id': rule_id,
            'selected_chat_id': chat_id,
            'chats': chats,
            'unread_count': unread_count,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/alerts/rules/create')
def create_alert_rule(
    request: Request,
    name: str = Form(...),
    pattern: str = Form(...),
    pattern_type: str = Form(default='keyword'),
    chat_ids_filter: str = Form(default=''),
    notify_web: bool = Form(default=True),
    notify_telegram: bool = Form(default=False),
):
    if pattern_type not in ('keyword', 'regex'):
        pattern_type = 'keyword'
    filter_list = None
    if chat_ids_filter.strip():
        try:
            filter_list = [int(x.strip()) for x in chat_ids_filter.split(',') if x.strip()]
        except ValueError:
            pass
    with session_scope() as db:
        rule = AlertRule(
            name=name.strip(),
            pattern=pattern.strip(),
            pattern_type=pattern_type,
            notify_web=notify_web,
            notify_telegram=notify_telegram,
            chat_ids_filter=filter_list,
        )
        db.add(rule)
    return redirect_with_message('/alerts', _t(request, 'alerts.flash_rule_created'), 'success')


@app.post('/alerts/rules/{rule_id}/toggle')
def toggle_alert_rule(request: Request, rule_id: int):
    with session_scope() as db:
        rule = db.get(AlertRule, rule_id)
        if rule:
            rule.is_active = not rule.is_active
    return redirect_with_message('/alerts', _t(request, 'alerts.flash_rule_updated'), 'success')


@app.post('/alerts/rules/{rule_id}/delete')
def delete_alert_rule(request: Request, rule_id: int):
    with session_scope() as db:
        rule = db.get(AlertRule, rule_id)
        if rule:
            db.delete(rule)
    return redirect_with_message('/alerts', _t(request, 'alerts.flash_rule_deleted'), 'success')


@app.post('/alerts/matches/{match_id}/read')
def mark_alert_read(request: Request, match_id: int):
    with session_scope() as db:
        match = db.get(AlertMatch, match_id)
        if match:
            match.is_read = True
    return redirect_with_message('/alerts', _t(request, 'alerts.flash_marked_read'), 'success')


@app.post('/alerts/matches/read-all')
def mark_all_alerts_read(request: Request):
    with session_scope() as db:
        db.query(AlertMatch).filter(AlertMatch.is_read.is_(False)).update({AlertMatch.is_read: True})
    return redirect_with_message('/alerts', _t(request, 'alerts.flash_all_read'), 'success')


@app.get('/api/alerts/unread')
def unread_alerts_api():
    with session_scope() as db:
        count = db.execute(
            select(func.count(AlertMatch.id)).where(AlertMatch.is_read.is_(False))
        ).scalar_one()
        recent = db.execute(
            select(AlertMatch).where(AlertMatch.is_read.is_(False))
            .order_by(AlertMatch.matched_at.desc()).limit(10)
        ).scalars().all()
        items = []
        for m in recent:
            items.append({
                'id': m.id,
                'rule_id': m.rule_id,
                'chat_id': m.chat_id,
                'matched_text': (m.matched_text or '')[:100],
                'matched_at': m.matched_at.isoformat() if m.matched_at else None,
            })
    return JSONResponse({'count': count, 'items': items})


# ==================== URL Stats API ====================

@app.get('/api/url-stats')
def url_stats_api():
    with session_scope() as db:
        domains = analysis.domain_frequency_stats(db)
        categories_raw = db.execute(
            select(AiUrl.category, func.count(AiUrl.id).label('count'))
            .group_by(AiUrl.category)
        ).all()
        cross_chat = analysis.cross_chat_urls(db, limit=10)
        reputation = analysis.url_reputation_summary(db)
        trend = analysis.url_trend_data(db, days=30)
    return JSONResponse({
        'domains': domains,
        'categories': [{'category': c, 'count': n} for c, n in categories_raw],
        'cross_chat_urls': cross_chat,
        'reputation': reputation,
        'trend': trend,
    })

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import BackgroundTasks, FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import select, desc, func, or_, case

from . import analysis
from . import analysis_advanced
from .analysis import dashboard_metrics, chat_statistics, system_status
from .dashboard import (
    dashboard_activity_cached,
    dashboard_ai_stats_cached,
    dashboard_overview_cached,
    dashboard_sync_status_cached,
    dashboard_top_chats_cached,
    dashboard_top_keywords_cached,
    dashboard_top_senders_cached,
)
from .ai_chat import (
    get_chat_history,
    get_or_create_chat_session,
    get_recent_sessions,
    run_chat_turn,
    stream_chat_answer,
)
from .ai_service import PROVIDER_CONFIGS, deduplicate_existing_urls, get_ai_provider_config, get_url_classification_prompt, normalize_url_for_dedup, run_key_lead_analysis_once, run_summary_now, run_url_classification_once, url_duplicate_stats
from .collector import collector
from .config import settings, BASE_DIR
from .db import init_database, session_scope, SessionLocal
from .join_targets import JOIN_STATUS_KEYS, discover_join_targets_from_collected_data, enqueue_join_targets, sync_join_targets_with_monitored_chats
from .market_brief import generate_daily_brief
from .models import (
    MonitoredChat, Message, TelegramUser, SyncRun, AppSetting, AiSummary,
    AiUrl, AiUrlCategory, AiUrlClassificationRun, AiProduct, AiContact,
    AiKeyLead, AiKeyLeadRun, AlertRule, AlertMatch, TelegramJoinTarget,
    SystemEvent, DailyMarketBrief, ChatSession, ChatMessage,
)
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
_CSRF_COOKIE_NAME = 'csrf_token'
_CSRF_FORM_FIELD = 'csrf_token'
_UNSAFE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def _auth_password_configured() -> bool:
    return bool(settings.auth_password and settings.auth_password.strip())


def _make_auth_token() -> str:
    if not _auth_password_configured():
        return ''
    return hashlib.sha256(f"{settings.auth_password}:{_AUTH_SECRET}".encode()).hexdigest()


def _check_auth_cookie(request: Request) -> bool:
    if not _auth_password_configured():
        return True
    token = request.cookies.get('auth_token')
    if not token:
        return False
    expected = _make_auth_token()
    return secrets.compare_digest(token, expected)


def _check_api_sk(request: Request) -> bool:
    expected = settings.api_sk.strip() if settings.api_sk else ''
    if not expected:
        return False
    sk = request.headers.get('x-api-key', '')
    sk = sk.strip()
    if not sk:
        return False
    return secrets.compare_digest(sk, expected)


def _csrf_token(request: Request) -> str:
    existing = getattr(request.state, 'csrf_token', '')
    if existing:
        return existing
    token = request.cookies.get(_CSRF_COOKIE_NAME, '')
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
    request.state.csrf_token = token
    return token


def _set_csrf_cookie(response, request: Request) -> None:
    response.set_cookie(
        _CSRF_COOKIE_NAME,
        _csrf_token(request),
        max_age=86400 * 30,
        httponly=False,
        samesite='lax',
        secure=request.url.scheme == 'https',
    )


async def _check_csrf(request: Request) -> bool:
    cookie_token = request.cookies.get(_CSRF_COOKIE_NAME, '')
    if not cookie_token:
        return False
    submitted_token = request.headers.get('x-csrf-token', '')
    if submitted_token and secrets.compare_digest(cookie_token, submitted_token):
        return True

    try:
        # BaseHTTPMiddleware passes a wrapped receive channel downstream. Calling
        # request.form() directly consumes that stream without caching the body,
        # so FastAPI Form(...) parameters later see an empty payload and fall
        # back to defaults. Cache the body first so Starlette can replay it.
        await request.body()
        form = await request.form()
        submitted_token = str(form.get(_CSRF_FORM_FIELD, '')) or submitted_token
    except Exception:
        pass
    return bool(submitted_token) and secrets.compare_digest(cookie_token, submitted_token)


_PUBLIC_PATHS = {'/login', '/logout'}


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware for page-level cookie auth and API key auth."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Static/media/lang paths — always allow
        if path.startswith(('/static/', '/media/', '/lang/')):
            return await call_next(request)

        # Login/logout paths — always allow
        if path in _PUBLIC_PATHS:
            response = await call_next(request)
            if request.method == 'GET' and not request.cookies.get(_CSRF_COOKIE_NAME):
                _set_csrf_cookie(response, request)
            return response

        # Web UI API routes — used by the browser, allow cookie auth + CSRF.
        if path.startswith('/api/chat/') or path.startswith('/api/dashboard/'):
            if _check_api_sk(request):
                return await call_next(request)
            if _check_auth_cookie(request):
                if request.method in _UNSAFE_METHODS and not await _check_csrf(request):
                    return JSONResponse({'error': 'CSRF validation failed'}, status_code=403)
                response = await call_next(request)
                if not request.cookies.get(_CSRF_COOKIE_NAME):
                    _set_csrf_cookie(response, request)
                return response
            return JSONResponse(
                {'error': 'Unauthorized', 'message': 'Missing or invalid authentication'},
                status_code=401,
            )

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
            if request.method in _UNSAFE_METHODS and not await _check_csrf(request):
                return JSONResponse({'error': 'CSRF validation failed'}, status_code=403)
            response = await call_next(request)
            if not request.cookies.get(_CSRF_COOKIE_NAME):
                _set_csrf_cookie(response, request)
            return response

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
                lines.append(f'{key}={_env_value(updates[key])}')
                written.add(key)
            else:
                lines.append(line)
        for key, value in updates.items():
            if key not in written:
                lines.append(f'{key}={_env_value(value)}')
        env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _env_value(value: str) -> str:
    return str(value).replace('\r', ' ').replace('\n', ' ').strip()


def _apply_settings(updates: dict[str, object]) -> None:
    for key, value in updates.items():
        setattr(settings, key, value)


def _iso_dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=' ') if value else None


def _daily_brief_payload(brief: DailyMarketBrief | None) -> dict | None:
    if not brief:
        return None
    return {
        'id': brief.id,
        'brief_date': brief.brief_date.isoformat() if brief.brief_date else None,
        'title': brief.title,
        'content': brief.content,
        'signals_json': brief.signals_json if isinstance(brief.signals_json, list) else [],
        'hot_topics_json': brief.hot_topics_json if isinstance(brief.hot_topics_json, list) else [],
        'risk_level': brief.risk_level,
        'price_moves_json': brief.price_moves_json if isinstance(brief.price_moves_json, list) else [],
        'created_at': brief.created_at.isoformat() if brief.created_at else None,
    }


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


MARKET_SIGNAL_TYPE_KEYS = {
    'market': 'market_intel.type_market',
    'risk': 'market_intel.type_risk',
    'price': 'market_intel.type_price',
    'legal': 'market_intel.type_legal',
    'hotspot': 'market_intel.type_hotspot',
    'gossip': 'market_intel.type_gossip',
}


def _market_intelligence_payload(summary: AiSummary) -> dict:
    payload = summary.extracted_urls or {}
    intel = payload.get('market_intelligence') if isinstance(payload, dict) else {}
    if not isinstance(intel, dict):
        intel = {}
    risk_level = str(intel.get('risk_level') or 'low').strip().lower()
    if risk_level not in {'low', 'medium', 'high'}:
        risk_level = 'low'
    return {
        'market_trend': str(intel.get('market_trend') or '').strip(),
        'risk_level': risk_level,
        'risk_signals': _json_string_list(intel.get('risk_signals')),
        'price_changes': _json_string_list(intel.get('price_changes')),
        'legal_risks': _json_string_list(intel.get('legal_risks')),
        'hot_topics': _json_string_list(intel.get('hot_topics')),
        'gossip_signals': _json_string_list(intel.get('gossip_signals')),
        'industries': _json_string_list(intel.get('industries')),
        'signal_types': [v.lower() for v in _json_string_list(intel.get('signal_types')) if v.lower() in MARKET_SIGNAL_TYPE_KEYS],
        'key_people': _json_string_list(intel.get('key_people')),
        'timeline_points': _json_string_list(intel.get('timeline_points')),
    }


def _json_string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
    return items


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
    if _auth_password_configured() and secrets.compare_digest(password.strip(), settings.auth_password.strip()):
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
        resp.set_cookie(
            _CSRF_COOKIE_NAME,
            _csrf_token(request),
            max_age=86400 * 30,
            httponly=False,
            samesite='lax',
            secure=is_secure,
        )
        return resp
    return RedirectResponse('/login?error=invalid', status_code=303)


@app.get('/logout')
def logout():
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie('auth_token', path='/')
    resp.delete_cookie(_CSRF_COOKIE_NAME, path='/')
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
templates.env.globals['csrf_field_name'] = _CSRF_FORM_FIELD
templates.env.globals['csrf_token'] = _csrf_token


def _migrate_existing_urls() -> None:
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
                    canonical_url = normalize_url_for_dedup(url)
                    h = hashlib.sha256(canonical_url.encode('utf-8')).hexdigest()
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)
                    row = db.execute(select(AiUrl).where(AiUrl.url_hash == h)).scalar_one_or_none()
                    if row:
                        row.last_seen_at = max(row.last_seen_at, s.completed_at or s.triggered_at)
                    else:
                        parsed = urlsplit(canonical_url)
                        to_insert.append(AiUrl(
                            url=canonical_url,
                            url_hash=h,
                            category=category,
                            domain=parsed.netloc or None,
                            first_seen_at=s.completed_at or s.triggered_at,
                            last_seen_at=s.completed_at or s.triggered_at,
                        ))
        for obj in to_insert:
            db.add(obj)
        if to_insert:
            logger.info('migrated %d existing URLs to ai_urls table', len(to_insert))


@app.get('/', response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name='dashboard.html',
        context={
            'request': request,
            'session_mode': settings.telegram_session_mode,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.get('/chats', response_class=HTMLResponse)
def chats_page(request: Request):
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.updated_at.desc())).scalars().all()
        if chats:
            chat_ids = [c.id for c in chats]
            last_24h = datetime.utcnow() - timedelta(hours=24)
            stats_rows = db.execute(
                select(
                    Message.chat_id,
                    func.count(Message.id).label('message_count'),
                    func.sum(case((Message.message_date >= last_24h, 1), else_=0)).label('messages_24h'),
                    func.count(func.distinct(Message.sender_user_id)).label('unique_senders'),
                    func.max(Message.message_date).label('last_msg'),
                ).where(Message.chat_id.in_(chat_ids)).group_by(Message.chat_id)
            ).all()
            stats_map = {row.chat_id: row for row in stats_rows}
            for chat in chats:
                row = stats_map.get(chat.id)
                chat.message_count = int(row.message_count) if row else 0
                chat.messages_24h = int(row.messages_24h) if row else 0
                chat.unique_senders = int(row.unique_senders) if row else 0
                if row and row.last_msg and not chat.last_message_at:
                    chat.last_message_at = row.last_msg
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
def toggle_all_chats(request: Request, action: str = Form(...), chat_ids: list[int] = Form(default=[])):
    if action not in ('enable', 'disable'):
        return redirect_with_message('/chats', _t(request, 'chats.flash_invalid_action'), 'error')
    target = action == 'enable'
    with session_scope() as db:
        query = select(MonitoredChat)
        if chat_ids:
            query = query.where(MonitoredChat.id.in_(chat_ids))
        updated = db.execute(query).scalars().all()
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


@app.get('/join-targets', response_class=HTMLResponse)
def join_targets_page(request: Request, status: str | None = None, keyword: str | None = None, page: int = 1):
    page_size = 30
    page = max(page, 1)
    selected_status = (status or '').strip()
    if selected_status not in JOIN_STATUS_KEYS:
        selected_status = ''
    keyword = (keyword or '').strip()

    with session_scope() as db:
        count_rows = db.execute(
            select(TelegramJoinTarget.status, func.count(TelegramJoinTarget.id))
            .group_by(TelegramJoinTarget.status)
        ).all()
        status_counts = {row[0]: row[1] for row in count_rows}
        total_targets = sum(status_counts.values())
        source_counts = {
            'auto': db.execute(
                select(func.count(TelegramJoinTarget.id)).where(TelegramJoinTarget.title.like('auto:%'))
            ).scalar_one(),
            'manual': db.execute(
                select(func.count(TelegramJoinTarget.id)).where(
                    or_(TelegramJoinTarget.title.is_(None), ~TelegramJoinTarget.title.like('auto:%'))
                )
            ).scalar_one(),
        }

        query = select(TelegramJoinTarget).outerjoin(TelegramJoinTarget.monitored_chat)
        if selected_status:
            query = query.where(TelegramJoinTarget.status == selected_status)
        if keyword:
            like = f'%{keyword}%'
            query = query.where(or_(
                TelegramJoinTarget.source.like(like),
                TelegramJoinTarget.normalized_key.like(like),
                TelegramJoinTarget.title.like(like),
                MonitoredChat.title.like(like),
                MonitoredChat.username.like(like),
            ))

        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        targets = db.execute(
            query.order_by(TelegramJoinTarget.created_at.desc(), TelegramJoinTarget.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars().unique().all()
        for target in targets:
            if target.monitored_chat:
                target.monitored_chat.title

    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page_params = {}
    if selected_status:
        page_params['status'] = selected_status
    if keyword:
        page_params['keyword'] = keyword
    return templates.TemplateResponse(
        request=request,
        name='join_targets.html',
        context={
            'request': request,
            'targets': targets,
            'status_counts': status_counts,
            'source_counts': source_counts,
            'total_targets': total_targets,
            'selected_status': selected_status,
            'keyword': keyword,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'page_base_params': urlencode(page_params),
            'join_interval_minutes': settings.telegram_join_interval_minutes,
            'join_queue_enabled': settings.telegram_join_queue_enabled,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/join-targets/add')
def add_join_targets(request: Request, targets: str = Form(default='')):
    result = enqueue_join_targets(targets)
    invalid_count = len(result['invalid'])
    return redirect_with_message(
        '/join-targets',
        _t(
            request,
            'join_targets.flash_added',
            inserted=result['inserted'],
            existing=result['existing'],
            invalid=invalid_count,
        ),
        'success' if result['inserted'] else 'info',
    )


@app.post('/join-targets/discover-sync')
async def discover_and_sync_join_targets(request: Request):
    try:
        discover_result = discover_join_targets_from_collected_data()
        dialog_count = await collector.sync_dialogs()
        sync_result = sync_join_targets_with_monitored_chats()
        return redirect_with_message(
            '/join-targets',
            _t(
                request,
                'join_targets.flash_discovered',
                scanned=discover_result['scanned'],
                inserted=discover_result['inserted'],
                existing=discover_result['existing'],
                dialogs=dialog_count,
                updated=sync_result['updated'],
            ),
            'success',
        )
    except Exception as exc:
        logger.exception('sync join targets failed')
        return redirect_with_message('/join-targets', _t(request, 'join_targets.flash_sync_failed', error=exc), 'error')


@app.post('/join-targets/sync-current')
async def sync_join_targets(request: Request):
    return await discover_and_sync_join_targets(request)


@app.post('/join-targets/run-once')
async def run_join_targets_once(request: Request):
    try:
        result = await collector.run_join_queue_once()
        status = result.get('status', 'unknown')
        return redirect_with_message('/join-targets', _t(request, 'join_targets.flash_run_once', status=status), 'success')
    except Exception as exc:
        logger.exception('run join target once failed')
        return redirect_with_message('/join-targets', _t(request, 'join_targets.flash_run_failed', error=exc), 'error')


@app.post('/join-targets/{target_id}/retry')
def retry_join_target(request: Request, target_id: int):
    with session_scope() as db:
        target = db.get(TelegramJoinTarget, target_id)
        if not target:
            return redirect_with_message('/join-targets', _t(request, 'join_targets.flash_not_found'), 'error')
        if target.status in {'joined', 'already_joined'}:
            return redirect_with_message('/join-targets', _t(request, 'join_targets.flash_already_joined'), 'info')
        target.status = 'pending'
        target.last_error = None
        target.next_attempt_at = None
    return redirect_with_message('/join-targets', _t(request, 'join_targets.flash_retry'), 'success')


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
                sender_obj = senders.get(item.sender_user_id) if item.sender_user_id else None
                grouped_rows[group_key] = {
                    'message': item,
                    'chat_title': chat_titles.get(item.chat_id, str(item.chat_id)),
                    'sender_name': (
                        sender_obj.username or sender_obj.first_name or str(sender_obj.telegram_id)
                    ) if sender_obj else 'Unknown',
                    'sender_user_id': item.sender_user_id,
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
            'telegram_join_queue_enabled': settings.telegram_join_queue_enabled,
            'telegram_join_interval_minutes': settings.telegram_join_interval_minutes,
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
    telegram_join_queue_enabled: bool = Form(default=False),
    telegram_join_interval_minutes: int = Form(default=10),
    sync_interval_minutes: int = Form(default=5),
    sync_batch_size: int = Form(default=200),
    sync_lookback_messages: int = Form(default=1000),
    ai_summary_batch_size: int = Form(default=1000),
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
    telegram_join_interval_minutes = min(max(telegram_join_interval_minutes, 10), 1440)
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
        'telegram_join_queue_enabled': telegram_join_queue_enabled,
        'telegram_join_interval_minutes': telegram_join_interval_minutes,
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
        'TELEGRAM_JOIN_QUEUE_ENABLED': _env_bool(telegram_join_queue_enabled),
        'TELEGRAM_JOIN_INTERVAL_MINUTES': str(telegram_join_interval_minutes),
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
            'ai_summary_batch_size': settings.ai_summary_batch_size,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.get('/market-intel', response_class=HTMLResponse)
def market_intel_page(
    request: Request,
    chat_id: str | None = None,
    industry: str | None = None,
    signal_type: str | None = None,
    page: int = 1,
):
    page_size = 20
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    selected_industry = (industry or '').strip()
    selected_signal_type = (signal_type or '').strip().lower()
    if selected_signal_type not in MARKET_SIGNAL_TYPE_KEYS:
        selected_signal_type = ''

    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        query = select(AiSummary).where(AiSummary.status == 'success')
        if selected_chat_id:
            query = query.where(AiSummary.chat_id == selected_chat_id)
        summaries = db.execute(
            query.order_by(AiSummary.completed_at.desc(), AiSummary.id.desc())
        ).scalars().all()
        chat_map = {c.id: c.title for c in chats}

    industry_counts: dict[str, int] = {}
    signal_type_counts: dict[str, int] = {key: 0 for key in MARKET_SIGNAL_TYPE_KEYS}
    rows = []
    for summary in summaries:
        intel = _market_intelligence_payload(summary)
        extracted = summary.extracted_urls or {}

        for item in intel['industries']:
            industry_counts[item] = industry_counts.get(item, 0) + 1
        for item in intel['signal_types']:
            signal_type_counts[item] = signal_type_counts.get(item, 0) + 1

        if selected_industry and selected_industry not in intel['industries']:
            continue
        if selected_signal_type and selected_signal_type not in intel['signal_types']:
            continue

        people = []
        for value in intel['key_people'] + _json_string_list(extracted.get('top_senders') if isinstance(extracted, dict) else []):
            if value not in people:
                people.append(value)

        rows.append({
            'summary': summary,
            'chat_title': chat_map.get(summary.chat_id, str(summary.chat_id)),
            'intel': intel,
            'people': people[:8],
        })

    total_count = len(rows)
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page_rows = rows[(page - 1) * page_size:page * page_size]
    page_params = {}
    if selected_chat_id:
        page_params['chat_id'] = str(selected_chat_id)
    if selected_industry:
        page_params['industry'] = selected_industry
    if selected_signal_type:
        page_params['signal_type'] = selected_signal_type
    encoded_params = urlencode(page_params)
    page_url_prefix = f'/market-intel?{encoded_params}&page=' if encoded_params else '/market-intel?page='

    return templates.TemplateResponse(
        request=request,
        name='market_intel.html',
        context={
            'request': request,
            'rows': page_rows,
            'chats': chats,
            'selected_chat_id': selected_chat_id,
            'selected_industry': selected_industry,
            'selected_signal_type': selected_signal_type,
            'industry_counts': sorted(industry_counts.items(), key=lambda item: (-item[1], item[0])),
            'signal_type_counts': signal_type_counts,
            'signal_type_labels': {k: _t(request, v) for k, v in MARKET_SIGNAL_TYPE_KEYS.items()},
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'page_url_prefix': page_url_prefix,
            'ai_summary_batch_size': settings.ai_summary_batch_size,
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
        duplicate_stats = url_duplicate_stats(limit=8)
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
            'duplicate_stats': duplicate_stats,
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


@app.post('/urls/deduplicate')
def deduplicate_urls_now(request: Request):
    try:
        result = deduplicate_existing_urls()
        return redirect_with_message(
            '/urls',
            f"URL 去重完成：合并 {result['groups_merged']} 组，删除 {result['rows_removed']} 条重复，规范化 {result['rows_normalized']} 条",
            'success',
        )
    except Exception as exc:
        logger.exception('URL deduplication failed')
        return redirect_with_message('/urls', f'URL 去重失败：{exc}', 'error')


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


@app.get('/api/dashboard/overview')
def dashboard_overview_api():
    with session_scope() as db:
        return JSONResponse(dashboard_overview_cached(db))


@app.get('/api/dashboard/activity')
def dashboard_activity_api():
    with session_scope() as db:
        return JSONResponse(dashboard_activity_cached(db))


@app.get('/api/dashboard/top-chats')
def dashboard_top_chats_api():
    with session_scope() as db:
        return JSONResponse(dashboard_top_chats_cached(db))


@app.get('/api/dashboard/top-senders')
def dashboard_top_senders_api():
    with session_scope() as db:
        return JSONResponse(dashboard_top_senders_cached(db))


@app.get('/api/dashboard/top-keywords')
def dashboard_top_keywords_api():
    with session_scope() as db:
        return JSONResponse(dashboard_top_keywords_cached(db))


@app.get('/api/dashboard/ai-stats')
def dashboard_ai_stats_api():
    with session_scope() as db:
        return JSONResponse(dashboard_ai_stats_cached(db))


@app.get('/api/dashboard/sync-status')
def dashboard_sync_status_api():
    with session_scope() as db:
        return JSONResponse(dashboard_sync_status_cached(db))


# ==================== Products Page ====================

PRODUCT_STATUS_KEYS = {'available': 'common.available', 'sold': 'common.sold', 'reserved': 'common.reserved'}
PRODUCT_STATUS_ORDER = ['available', 'sold', 'reserved']
PRODUCT_CATEGORY_KEYS = {
    'ai_api': 'products.category_ai_api',
    'account': 'products.category_account',
    'relay': 'products.category_relay',
    'cloud_drive': 'products.category_cloud_drive',
    'payment': 'products.category_payment',
    'other': 'products.category_other',
}
PRODUCT_CATEGORY_ORDER = ['ai_api', 'account', 'relay', 'cloud_drive', 'payment', 'other']
PRODUCT_CATEGORY_TERMS = {
    'ai_api': ['api', 'key', '额度', 'credits', 'openai', 'claude', 'gemini', 'grok', 'groq', 'openrouter'],
    'account': ['账号', '号', '实名', '接码', '号码', 'tg', 'telegram', '会员', '成品号'],
    'relay': ['中转', '节点', '代理', 'vpn', 'vps', 'relay', '转发', '流量'],
    'cloud_drive': ['网盘', '夸克', '百度云', '阿里云盘', 'drive', '资料', '教程'],
    'payment': ['支付', '店铺', '充值', '卡密', '直充', '收款', 'payment'],
}


def _product_category(product: AiProduct) -> str:
    text = f'{product.product_name or ""} {product.seller_contact or ""}'.lower()
    for category in PRODUCT_CATEGORY_ORDER:
        if category == 'other':
            continue
        if any(term.lower() in text for term in PRODUCT_CATEGORY_TERMS[category]):
            return category
    return 'other'


def _product_category_clause(category: str):
    terms = PRODUCT_CATEGORY_TERMS.get(category)
    if category == 'other':
        all_terms = [term for category_terms in PRODUCT_CATEGORY_TERMS.values() for term in category_terms]
        positive_clauses = []
        for term in all_terms:
            like = f'%{term}%'
            positive_clauses.append(AiProduct.product_name.like(like))
            positive_clauses.append(func.coalesce(AiProduct.seller_contact, '').like(like))
        return ~or_(*positive_clauses)
    if not terms:
        return None
    clauses = []
    for term in terms:
        like = f'%{term}%'
        clauses.append(AiProduct.product_name.like(like))
        clauses.append(func.coalesce(AiProduct.seller_contact, '').like(like))
    return or_(*clauses)


def _product_group_key(name: str) -> str:
    return re.sub(r'\s+', ' ', (name or '').strip().lower())


def _product_aggregates(products: list[AiProduct]) -> dict:
    groups: dict[str, dict] = {}
    sellers: set[str] = set()
    known_price_count = 0
    for product in products:
        if product.seller_contact:
            sellers.add(product.seller_contact.lower())
        if product.price_amount is not None:
            known_price_count += 1
        key = _product_group_key(product.product_name)
        if not key:
            continue
        group = groups.setdefault(key, {
            'name': product.product_name,
            'count': 0,
            'prices': [],
            'currencies': set(),
            'sellers': set(),
            'chat_ids': set(),
            'last_seen_at': product.last_seen_at,
        })
        group['count'] += 1
        group['chat_ids'].add(product.chat_id)
        if product.price_currency:
            group['currencies'].add(product.price_currency)
        if product.seller_contact:
            group['sellers'].add(product.seller_contact)
        if product.price_amount is not None:
            group['prices'].append(float(product.price_amount))
        if product.last_seen_at and (not group['last_seen_at'] or product.last_seen_at > group['last_seen_at']):
            group['last_seen_at'] = product.last_seen_at

    comparisons = []
    for group in groups.values():
        prices = group['prices']
        if not prices:
            continue
        comparisons.append({
            'name': group['name'],
            'count': group['count'],
            'min_price': min(prices),
            'max_price': max(prices),
            'avg_price': round(sum(prices) / len(prices), 2),
            'currency': ', '.join(sorted(group['currencies'])) or 'CNY',
            'seller_count': len(group['sellers']),
            'chat_count': len(group['chat_ids']),
            'last_seen_at': group['last_seen_at'],
        })
    comparisons.sort(key=lambda item: (item['max_price'] - item['min_price'], item['count']), reverse=True)
    return {
        'total': len(products),
        'group_count': len(groups),
        'known_price_count': known_price_count,
        'seller_count': len(sellers),
        'comparisons': comparisons[:12],
    }


@app.get('/products', response_class=HTMLResponse)
def products_page(
    request: Request,
    status: str | None = None,
    category: str | None = None,
    chat_id: str | None = None,
    keyword: str | None = None,
    page: int = 1,
):
    page_size = 50
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    selected_category = category if category in PRODUCT_CATEGORY_KEYS else None
    keyword = (keyword or '').strip()
    with session_scope() as db:
        filtered_base_query = select(AiProduct)
        if selected_chat_id:
            filtered_base_query = filtered_base_query.where(AiProduct.chat_id == selected_chat_id)
        if keyword:
            like = f'%{keyword}%'
            filtered_base_query = filtered_base_query.where(
                (AiProduct.product_name.like(like)) |
                (func.coalesce(AiProduct.seller_contact, '').like(like))
            )
        base_query = filtered_base_query
        category_clause = _product_category_clause(selected_category) if selected_category else None
        if category_clause is not None:
            base_query = base_query.where(category_clause)

        status_counts = {}
        for s in PRODUCT_STATUS_ORDER:
            status_counts[s] = db.execute(
                select(func.count()).select_from(base_query.where(AiProduct.status == s).subquery())
            ).scalar_one()

        category_counts = {}
        for cat in PRODUCT_CATEGORY_ORDER:
            clause = _product_category_clause(cat)
            count_query = filtered_base_query if clause is None else filtered_base_query.where(clause)
            if status in PRODUCT_STATUS_KEYS:
                count_query = count_query.where(AiProduct.status == status)
            category_counts[cat] = db.execute(select(func.count()).select_from(count_query.subquery())).scalar_one()

        query = base_query.order_by(AiProduct.last_seen_at.desc())
        if status in PRODUCT_STATUS_KEYS:
            query = query.where(AiProduct.status == status)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        products = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        aggregate_products = db.execute(query.order_by(None)).scalars().all()
        product_rows = [{'product': product, 'category': _product_category(product)} for product in products]
        aggregates = _product_aggregates(aggregate_products)
        chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page_params = {}
    if status in PRODUCT_STATUS_KEYS:
        page_params['status'] = status
    if selected_category:
        page_params['category'] = selected_category
    if selected_chat_id:
        page_params['chat_id'] = str(selected_chat_id)
    if keyword:
        page_params['keyword'] = keyword
    encoded_params = urlencode(page_params)
    page_url_prefix = f'/products?{encoded_params}&page=' if encoded_params else '/products?page='
    return templates.TemplateResponse(
        request=request,
        name='products.html',
        context={
            'request': request,
            'products': product_rows,
            'selected_status': status,
            'selected_category': selected_category,
            'selected_chat_id': selected_chat_id,
            'keyword': keyword,
            'counts': status_counts,
            'category_counts': category_counts,
            'labels': {k: _t(request, v) for k, v in PRODUCT_STATUS_KEYS.items()},
            'category_labels': {k: _t(request, v) for k, v in PRODUCT_CATEGORY_KEYS.items()},
            'order': PRODUCT_STATUS_ORDER,
            'category_order': PRODUCT_CATEGORY_ORDER,
            'aggregates': aggregates,
            'chats': chats,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'page_url_prefix': page_url_prefix,
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


def _contact_aggregates(contacts: list[AiContact]) -> dict:
    groups: dict[str, dict] = {}
    for contact in contacts:
        key = f'{contact.contact_type}:{(contact.contact_value or "").strip().lower()}'
        group = groups.setdefault(key, {
            'contact_type': contact.contact_type,
            'contact_value': contact.contact_value,
            'count': 0,
            'chat_ids': set(),
            'first_seen_at': contact.first_seen_at,
            'last_seen_at': contact.last_seen_at,
        })
        group['count'] += 1
        group['chat_ids'].add(contact.chat_id)
        if contact.first_seen_at and (not group['first_seen_at'] or contact.first_seen_at < group['first_seen_at']):
            group['first_seen_at'] = contact.first_seen_at
        if contact.last_seen_at and (not group['last_seen_at'] or contact.last_seen_at > group['last_seen_at']):
            group['last_seen_at'] = contact.last_seen_at

    repeated = []
    for group in groups.values():
        repeated.append({
            'contact_type': group['contact_type'],
            'contact_value': group['contact_value'],
            'count': group['count'],
            'chat_count': len(group['chat_ids']),
            'first_seen_at': group['first_seen_at'],
            'last_seen_at': group['last_seen_at'],
        })
    repeated.sort(key=lambda item: (item['chat_count'], item['count'], item['last_seen_at'] or datetime.min), reverse=True)
    return {
        'total': len(contacts),
        'unique_count': len(groups),
        'multi_chat_count': len([item for item in repeated if item['chat_count'] > 1]),
        'repeated': repeated[:12],
    }


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
def contacts_page(
    request: Request,
    contact_type: str | None = None,
    chat_id: str | None = None,
    keyword: str | None = None,
    page: int = 1,
):
    page_size = 50
    page = max(page, 1)
    selected_chat_id = _parse_optional_int(chat_id)
    keyword = (keyword or '').strip()
    with session_scope() as db:
        base_query = select(AiContact)
        if selected_chat_id:
            base_query = base_query.where(AiContact.chat_id == selected_chat_id)
        if keyword:
            like = f'%{keyword}%'
            base_query = base_query.where(
                (AiContact.contact_value.like(like)) |
                (func.coalesce(AiContact.context, '').like(like))
            )
        counts = {}
        for t in CONTACT_TYPE_ORDER:
            counts[t] = db.execute(
                select(func.count()).select_from(base_query.where(AiContact.contact_type == t).subquery())
            ).scalar_one()

        query = base_query.order_by(AiContact.last_seen_at.desc())
        if contact_type in CONTACT_TYPE_KEYS:
            query = query.where(AiContact.contact_type == contact_type)
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_count = db.execute(count_query).scalar_one()
        contacts = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        aggregate_contacts = db.execute(query.order_by(None)).scalars().all()
        aggregates = _contact_aggregates(aggregate_contacts)
        chats = db.execute(select(MonitoredChat).where(MonitoredChat.is_active.is_(True))).scalars().all()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page_params = {}
    if contact_type in CONTACT_TYPE_KEYS:
        page_params['contact_type'] = contact_type
    if selected_chat_id:
        page_params['chat_id'] = str(selected_chat_id)
    if keyword:
        page_params['keyword'] = keyword
    encoded_params = urlencode(page_params)
    page_url_prefix = f'/contacts?{encoded_params}&page=' if encoded_params else '/contacts?page='
    return templates.TemplateResponse(
        request=request,
        name='contacts.html',
        context={
            'request': request,
            'contacts': contacts,
            'selected_type': contact_type,
            'selected_chat_id': selected_chat_id,
            'keyword': keyword,
            'counts': counts,
            'labels': {k: _t(request, v) for k, v in CONTACT_TYPE_KEYS.items()},
            'order': CONTACT_TYPE_ORDER,
            'aggregates': aggregates,
            'chats': chats,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'page_url_prefix': page_url_prefix,
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


# ==================== Advanced Analytics Pages ====================

@app.get('/price-trends', response_class=HTMLResponse)
def price_trends_page(request: Request, chat_id: int | None = None, days: int = 30):
    days = min(max(days, 1), 90)
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        trends = analysis_advanced.get_price_trends(db, chat_id=chat_id, days=days)
        seller_comparison = analysis_advanced.aggregate_seller_prices(db, days=days)
    return templates.TemplateResponse(
        request=request,
        name='price_trends.html',
        context={
            'request': request,
            'chats': chats,
            'selected_chat_id': chat_id,
            'days': days,
            'trends': trends,
            'seller_comparison': seller_comparison,
        },
    )


@app.get('/system-events', response_class=HTMLResponse)
def system_events_page(request: Request, severity: str | None = None, unread_only: bool = False):
    with session_scope() as db:
        query = db.query(SystemEvent).order_by(SystemEvent.created_at.desc())
        if severity:
            query = query.filter(SystemEvent.severity == severity)
        if unread_only:
            query = query.filter(SystemEvent.is_read.is_(False))
        events = query.limit(100).all()
    return templates.TemplateResponse(
        request=request,
        name='system_events.html',
        context={
            'request': request,
            'events': events,
            'selected_severity': severity or '',
            'unread_only': unread_only,
        },
    )


@app.get('/daily-briefs', response_class=HTMLResponse)
def daily_briefs_page(request: Request, date: str | None = None):
    from datetime import datetime as _dt
    with session_scope() as db:
        latest = None
        if date:
            try:
                parsed = _dt.strptime(date, '%Y-%m-%d').date()
                latest = db.query(DailyMarketBrief).filter(
                    func.date(DailyMarketBrief.brief_date) == parsed,
                ).first()
            except ValueError:
                pass
        if not latest:
            latest = db.query(DailyMarketBrief).order_by(DailyMarketBrief.brief_date.desc()).first()
        briefs = db.query(DailyMarketBrief).order_by(DailyMarketBrief.brief_date.desc()).limit(30).all()
    latest_payload = _daily_brief_payload(latest)
    briefs_payload = [_daily_brief_payload(brief) for brief in briefs]
    return templates.TemplateResponse(
        request=request,
        name='daily_briefs.html',
        context={
            'request': request,
            'latest': latest,
            'briefs': briefs,
            'latest_payload': latest_payload,
            'briefs_payload': briefs_payload,
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


async def _generate_daily_brief_background() -> None:
    try:
        await generate_daily_brief()
    except Exception:
        logger.exception('background daily brief generation failed')


@app.post('/daily-briefs/generate')
def daily_briefs_generate(request: Request, background_tasks: BackgroundTasks):
    background_tasks.add_task(_generate_daily_brief_background)
    return redirect_with_message('/daily-briefs', _t(request, 'daily_briefs.flash_generating'), 'info')


@app.get('/url-propagation', response_class=HTMLResponse)
def url_propagation_page(request: Request, hours: int = 24):
    hours = min(max(hours, 1), 168)
    with session_scope() as db:
        results = analysis_advanced.aggregate_url_propagation(db, hours=hours, limit=100)
    return templates.TemplateResponse(
        request=request,
        name='url_propagation.html',
        context={
            'request': request,
            'hours': hours,
            'results': results,
        },
    )


@app.get('/duplicate-messages', response_class=HTMLResponse)
def duplicate_messages_page(request: Request):
    with session_scope() as db:
        duplicates = analysis_advanced.get_duplicate_message_groups(db, limit=100)
    return templates.TemplateResponse(
        request=request,
        name='duplicate_messages.html',
        context={
            'request': request,
            'duplicates': duplicates,
        },
    )


@app.get('/user-profile/{user_id}', response_class=HTMLResponse)
def user_profile_page(request: Request, user_id: int):
    with session_scope() as db:
        profile = analysis_advanced.build_user_profile(db, user_id)
    return templates.TemplateResponse(
        request=request,
        name='user_profile.html',
        context={
            'request': request,
            'profile': profile,
        },
    )


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


# ==================== Advanced Analytics API ====================

@app.get('/api/price-trends')
def price_trends_api(chat_id: int | None = None, days: int = 30):
    with session_scope() as db:
        trends = analysis_advanced.get_price_trends(db, chat_id=chat_id, days=days)
    return JSONResponse({'trends': trends})


@app.get('/api/seller-price-comparison')
def seller_price_comparison_api(product_name: str | None = None, days: int = 30):
    with session_scope() as db:
        results = analysis_advanced.aggregate_seller_prices(db, product_name=product_name, days=days)
    return JSONResponse({'results': results})


@app.get('/api/market-intelligence')
def market_intelligence_api(hours: int = 24):
    with session_scope() as db:
        data = analysis_advanced.aggregate_market_intelligence(db, hours=hours)
    return JSONResponse(data)


@app.get('/api/url-propagation')
def url_propagation_api(hours: int = 24, limit: int = 20):
    with session_scope() as db:
        results = analysis_advanced.aggregate_url_propagation(db, hours=hours, limit=limit)
    return JSONResponse({'results': results})


@app.get('/api/user-profile/{user_id}')
def user_profile_api(user_id: int):
    with session_scope() as db:
        profile = analysis_advanced.build_user_profile(db, user_id)
    if not profile:
        raise HTTPException(status_code=404, detail='User not found')
    return JSONResponse(profile)


@app.get('/api/system-events')
def system_events_api(severity: str | None = None, unread_only: bool = False, limit: int = 50):
    with session_scope() as db:
        query = db.query(SystemEvent)
        if severity:
            query = query.filter(SystemEvent.severity == severity)
        if unread_only:
            query = query.filter(SystemEvent.is_read.is_(False))
        rows = query.order_by(SystemEvent.created_at.desc()).limit(limit).all()
    return JSONResponse({'events': [{
        'id': r.id,
        'event_type': r.event_type,
        'severity': r.severity,
        'chat_id': r.chat_id,
        'title': r.title,
        'detail': r.detail,
        'metric_value': r.metric_value,
        'is_read': r.is_read,
        'created_at': r.created_at.isoformat() if r.created_at else None,
    } for r in rows]})


@app.post('/api/system-events/{event_id}/read')
def mark_system_event_read(event_id: int):
    with session_scope() as db:
        event = db.get(SystemEvent, event_id)
        if event:
            event.is_read = True
    return JSONResponse({'ok': True})


@app.post('/api/system-events/read-all')
def mark_all_system_events_read():
    with session_scope() as db:
        db.query(SystemEvent).filter(SystemEvent.is_read.is_(False)).update({SystemEvent.is_read: True})
    return JSONResponse({'ok': True})


@app.get('/api/duplicate-messages')
def duplicate_messages_api(limit: int = 50):
    with session_scope() as db:
        results = analysis_advanced.get_duplicate_message_groups(db, limit=limit)
    return JSONResponse({'duplicates': results})


@app.post('/api/analyze/daily-stats')
def analyze_daily_stats():
    with session_scope() as db:
        chat_count = analysis_advanced.compute_daily_chat_stats(db)
        user_count = analysis_advanced.compute_user_daily_aggregates(db)
    return JSONResponse({'chat_stats_updated': chat_count, 'user_stats_updated': user_count})


@app.post('/api/analyze/anomalies')
def analyze_anomalies():
    with session_scope() as db:
        events = analysis_advanced.detect_chat_anomalies(db)
    return JSONResponse({'events': events})


@app.get('/api/daily-briefs')
def daily_briefs_api(limit: int = 30):
    with session_scope() as db:
        rows = db.query(DailyMarketBrief).order_by(DailyMarketBrief.brief_date.desc()).limit(limit).all()
    return JSONResponse({'briefs': [{
        'id': r.id,
        'brief_date': r.brief_date.isoformat() if r.brief_date else None,
        'title': r.title,
        'risk_level': r.risk_level,
        'created_at': r.created_at.isoformat() if r.created_at else None,
    } for r in rows]})


@app.get('/api/daily-briefs/latest')
def daily_brief_latest_api():
    with session_scope() as db:
        row = db.query(DailyMarketBrief).order_by(DailyMarketBrief.brief_date.desc()).first()
    if not row:
        raise HTTPException(status_code=404, detail='No brief found')
    return JSONResponse({
        'id': row.id,
        'brief_date': row.brief_date.isoformat() if row.brief_date else None,
        'title': row.title,
        'content': row.content,
        'signals_json': row.signals_json,
        'hot_topics_json': row.hot_topics_json,
        'risk_level': row.risk_level,
        'price_moves_json': row.price_moves_json,
    })



# ==================== AI Chat ====================


@app.get('/chat', response_class=HTMLResponse)
def chat_page(request: Request, session_id: int | None = None):
    user_id = request.cookies.get('auth_token', 'anonymous')[:64]
    with session_scope() as db:
        session = get_or_create_chat_session(db, session_id, user_id)
        sessions = get_recent_sessions(db, user_id)
        history = get_chat_history(db, session.id)
    return templates.TemplateResponse(
        request=request,
        name='chat.html',
        context={
            'request': request,
            'session': session,
            'sessions': sessions,
            'history': history,
        },
    )


@app.get('/api/chat/sessions')
def chat_sessions_api(request: Request):
    user_id = request.cookies.get('auth_token', 'anonymous')[:64]
    with session_scope() as db:
        sessions = get_recent_sessions(db, user_id)
    return JSONResponse({'sessions': [
        {
            'id': s.id,
            'title': s.title,
            'created_at': s.created_at.isoformat() if s.created_at else None,
            'updated_at': s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in sessions
    ]})


@app.post('/api/chat')
async def chat_api(request: Request):
    """Non-streaming chat endpoint (fallback)."""
    data = await request.json()
    question = (data.get('question') or '').strip()
    session_id = data.get('session_id')
    if not question:
        return JSONResponse({'error': '问题不能为空'}, status_code=400)
    if len(question) > 2000:
        return JSONResponse({'error': '问题长度不能超过 2000 个字符'}, status_code=400)

    user_id = request.cookies.get('auth_token', 'anonymous')[:64]
    db = SessionLocal()
    try:
        session = get_or_create_chat_session(db, session_id, user_id)
        history = get_chat_history(db, session.id)
        content, tool_calls = await run_chat_turn(db, session.id, question, history)
        return JSONResponse({
            'session_id': session.id,
            'answer': content,
            'tools': tool_calls,
        })
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RuntimeError as exc:
        logger.warning('chat_api runtime error: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)
    except Exception as exc:
        logger.exception('chat_api failed')
        return JSONResponse({'error': 'AI 处理失败，请稍后重试'}, status_code=500)
    finally:
        db.close()


@app.post('/api/chat/stream')
async def chat_stream_api(request: Request):
    """Server-Sent Events streaming chat endpoint."""
    data = await request.json()
    question = (data.get('question') or '').strip()
    session_id = data.get('session_id')
    if not question:
        return JSONResponse({'error': '问题不能为空'}, status_code=400)
    if len(question) > 2000:
        return JSONResponse({'error': '问题长度不能超过 2000 个字符'}, status_code=400)

    user_id = request.cookies.get('auth_token', 'anonymous')[:64]

    async def event_generator():
        try:
            async for chunk in stream_chat_answer(user_id, session_id, question):
                yield chunk
        except Exception as exc:
            logger.exception('chat_stream_api event generator failed')
            yield f'event: error\ndata: {json.dumps({"message": "AI 处理失败，请稍后重试"}, ensure_ascii=False)}\n\n'
            yield f'event: done\ndata: {json.dumps({"ok": False}, ensure_ascii=False)}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )

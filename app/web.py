from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc, func

from . import analysis
from .analysis import dashboard_metrics, chat_statistics, system_status
from .ai_service import PROVIDER_CONFIGS, get_ai_provider_config, run_summary_now
from .collector import collector
from .config import settings, BASE_DIR
from .db import init_database, session_scope
from .models import MonitoredChat, Message, TelegramUser, SyncRun, AppSetting, AiSummary, AiUrl, AiProduct, AiContact, AlertRule, AlertMatch
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
settings.resolved_media_storage_path.mkdir(parents=True, exist_ok=True)
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'app' / 'static')), name='static')
app.mount('/media', StaticFiles(directory=str(settings.resolved_media_storage_path)), name='media')
templates = Jinja2Templates(directory=str(BASE_DIR / 'app' / 'templates'))

_load_i18n()


@app.get('/lang/{code}')
def switch_language(code: str, request: Request):
    referer = request.headers.get('referer', '/')
    resp = RedirectResponse(referer, status_code=303)
    if code in _I18N_DATA:
        resp.set_cookie('lang', code, max_age=86400 * 365)
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
def messages_page(request: Request, chat_id: int | None = None, keyword: str | None = None, media_only: bool = False, sender: str | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        query = select(Message)
        if chat_id:
            query = query.where(Message.chat_id == chat_id)
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
            'selected_chat_id': chat_id,
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
        if ai_api_key.strip():
            _upsert_setting(db, 'ai_api_key', ai_api_key.strip())
        _upsert_setting(db, 'ai_base_url', ai_base_url.strip())
        _upsert_setting(db, 'ai_model', ai_model.strip())
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
):
    if desktop_import_mode != 'create_new':
        return redirect_with_message('/settings', _t(request, 'settings.invalid_import_mode'), 'error')

    sync_interval_minutes = min(max(sync_interval_minutes, 1), 1440)
    sync_batch_size = min(max(sync_batch_size, 20), 1000)
    sync_lookback_messages = min(max(sync_lookback_messages, 100), 10000)
    ai_summary_batch_size = min(max(ai_summary_batch_size, 20), 1000)
    ai_summary_running_timeout_minutes = min(max(ai_summary_running_timeout_minutes, 5), 1440)

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
def summaries_page(request: Request, chat_id: int | None = None, page: int = 1):
    page_size = 20
    page = max(page, 1)
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        query = select(AiSummary)
        if chat_id:
            query = query.where(AiSummary.chat_id == chat_id)
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
            'selected_chat_id': chat_id,
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
def urls_page(request: Request, category: str | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        query = select(AiUrl).order_by(AiUrl.last_seen_at.desc())
        if category in CATEGORY_KEYS:
            query = query.where(AiUrl.category == category)
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
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    return templates.TemplateResponse(
        request=request,
        name='urls.html',
        context={
            'request': request,
            'urls': urls,
            'selected_category': category,
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
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


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
async def trigger_chat_summary(request: Request, chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
    if not chat:
        return redirect_with_message('/chats', _t(request, 'chat_detail.flash_chat_not_found'), 'error')
    try:
        summary_id = await run_summary_now(chat_id)
        return redirect_with_message(f'/chats/{chat_id}', _t(request, 'chat_detail.flash_ai_done', id=summary_id), 'success')
    except RuntimeError as exc:
        return redirect_with_message(f'/chats/{chat_id}', str(exc), 'error')
    except Exception as exc:
        logger.exception('manual summarize failed for chat %s', chat_id)
        return redirect_with_message(f'/chats/{chat_id}', _t(request, 'chat_detail.flash_ai_failed', error=exc), 'error')


@app.post('/summaries/{summary_id}/delete')
def delete_summary(request: Request, summary_id: int):
    with session_scope() as db:
        summary = db.get(AiSummary, summary_id)
        if summary:
            db.delete(summary)
    return redirect_with_message('/summaries', _t(request, 'summaries.flash_deleted'), 'success')


@app.post('/summaries/{summary_id}/rerun')
async def rerun_summary(request: Request, summary_id: int):
    with session_scope() as db:
        summary = db.get(AiSummary, summary_id)
        if not summary:
            return redirect_with_message('/summaries', _t(request, 'summaries.flash_not_found'), 'error')
        chat_id = summary.chat_id
    try:
        new_id = await run_summary_now(chat_id)
        return redirect_with_message('/summaries', _t(request, 'summaries.flash_rerun_done', id=new_id), 'success')
    except RuntimeError as exc:
        return redirect_with_message('/summaries', str(exc), 'error')
    except Exception as exc:
        logger.exception('rerun summary failed')
        return redirect_with_message('/summaries', _t(request, 'summaries.flash_rerun_failed', error=exc), 'error')


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
def products_page(request: Request, status: str | None = None, chat_id: int | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        query = select(AiProduct).order_by(AiProduct.last_seen_at.desc())
        if status in PRODUCT_STATUS_KEYS:
            query = query.where(AiProduct.status == status)
        if chat_id:
            query = query.where(AiProduct.chat_id == chat_id)
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
            'selected_chat_id': chat_id,
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


@app.get('/contacts', response_class=HTMLResponse)
def contacts_page(request: Request, contact_type: str | None = None, chat_id: int | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        query = select(AiContact).order_by(AiContact.last_seen_at.desc())
        if contact_type in CONTACT_TYPE_KEYS:
            query = query.where(AiContact.contact_type == contact_type)
        if chat_id:
            query = query.where(AiContact.chat_id == chat_id)
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
            'selected_chat_id': chat_id,
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

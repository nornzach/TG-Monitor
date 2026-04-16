from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc, func

from .analysis import dashboard_metrics
from .collector import collector
from .config import settings, BASE_DIR
from .db import init_database, session_scope
from .models import MonitoredChat, Message, TelegramUser, SyncRun
from .telegram_client import telegram_session_manager

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def redirect_with_message(path: str, message: str, level: str = 'info') -> RedirectResponse:
    return RedirectResponse(f'{path}?msg={message}&level={level}', status_code=303)

app = FastAPI(title=settings.app_name)
settings.resolved_media_storage_path.mkdir(parents=True, exist_ok=True)
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'app' / 'static')), name='static')
app.mount('/media', StaticFiles(directory=str(settings.resolved_media_storage_path)), name='media')
templates = Jinja2Templates(directory=str(BASE_DIR / 'app' / 'templates'))


@app.on_event('startup')
async def on_startup() -> None:
    init_database()
    await collector.start()


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


@app.get('/chats/{chat_id}/toggle')
def toggle_chat(chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
        if not chat:
            return redirect_with_message('/chats', '未找到目标群组', 'error')
        chat.is_active = not chat.is_active
    return redirect_with_message('/chats', '群组状态已更新', 'success')


@app.get('/chats/{chat_id}/backfill')
async def backfill_chat(chat_id: int):
    with session_scope() as db:
        chat = db.get(MonitoredChat, chat_id)
        telegram_id = chat.telegram_id if chat else None
    if not telegram_id:
        return redirect_with_message('/chats', '未找到目标群组', 'error')
    try:
        total = await collector.backfill_chat(telegram_id)
        return redirect_with_message('/chats', f'回填完成，处理 {total} 条消息', 'success')
    except Exception as exc:
        logger.exception('backfill failed')
        return redirect_with_message('/chats', f'回填失败：{exc}', 'error')


@app.get('/sync/dialogs')
async def sync_dialogs():
    try:
        count = await collector.sync_dialogs()
        return redirect_with_message('/chats', f'已同步 {count} 个新对话', 'success')
    except Exception as exc:
        logger.exception('sync dialogs failed')
        return redirect_with_message('/chats', f'同步失败：{exc}', 'error')


@app.get('/messages', response_class=HTMLResponse)
def messages_page(request: Request, chat_id: int | None = None, keyword: str | None = None, page: int = 1):
    page_size = 50
    page = max(page, 1)
    with session_scope() as db:
        chats = db.execute(select(MonitoredChat).order_by(MonitoredChat.title.asc())).scalars().all()
        query = select(Message)
        if chat_id:
            query = query.where(Message.chat_id == chat_id)
        if keyword:
            query = query.where(Message.normalized_text.like(f'%{keyword}%'))

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
            group_key = f'g:{grouped_id}' if grouped_id else f'm:{item.id}'
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
    client = await telegram_session_manager.get_client()
    me = await client.get_me() if client else None
    return templates.TemplateResponse(
        request=request,
        name='settings.html',
        context={
            'request': request,
            'session_mode': settings.telegram_session_mode,
            'tdata_path': str(settings.resolved_tdata_path),
            'session_path': str(settings.resolved_session_path),
            'telegram_connected': bool(me),
            'me': me,
            'api_id_present': bool(settings.telegram_api_id),
            'api_hash_present': bool(settings.telegram_api_hash),
            'msg': request.query_params.get('msg', ''),
            'level': request.query_params.get('level', 'info'),
        },
    )


@app.post('/settings/manual-login')
async def manual_login(phone: str = Form(...), code: str = Form(...), password: str = Form(default='')):
    async def code_callback():
        return code
    try:
        await telegram_session_manager.manual_login(phone=phone, code_callback=code_callback, password=password or None)
        return redirect_with_message('/settings', '手动登录成功', 'success')
    except Exception as exc:
        logger.exception('manual login failed')
        return redirect_with_message('/settings', f'手动登录失败：{exc}', 'error')


@app.get('/settings/connect')
async def connect_session():
    try:
        client = await telegram_session_manager.connect()
        if client:
            return redirect_with_message('/settings', 'Telegram 会话已连接', 'success')
        return redirect_with_message('/settings', '未能连接当前 Telegram 会话，请检查设置页说明', 'error')
    except Exception as exc:
        logger.exception('connect session failed')
        return redirect_with_message('/settings', f'连接失败：{exc}', 'error')


@app.get('/api/dashboard')
def dashboard_api():
    with session_scope() as db:
        return JSONResponse(dashboard_metrics(db))

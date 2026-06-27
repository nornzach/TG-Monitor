"""AI data-query chat agent with real-time streaming loop engine."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic_ai import _agent_graph, Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.orm import Session

from .ai_chat_prompts import build_system_prompt
from .ai_chat_tools import (
    get_chat_list,
    get_database_stats,
    run_sql,
    tool_result_to_text,
)
from .ai_service import get_ai_provider_config, get_ai_setting
from .db import SessionLocal
from .models import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

# Maximum number of tool calls within one user turn to prevent runaway loops.
MAX_TOOL_CALLS_PER_TURN = 8
# Maximum length of a user question.
MAX_QUESTION_LENGTH = 2000
# Overall timeout for one streaming turn (seconds).
STREAM_TURN_TIMEOUT = 180.0

# Cache for the OpenAI-compatible model instance to reuse HTTP connections.
_model_cache: dict[tuple[str, str, str], OpenAIModel] = {}


@dataclass
class AgentDeps:
    db: Session
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


def _build_agent_model(db: Session) -> OpenAIModel:
    """Build (or reuse cached) LLM model from app_settings."""
    api_key = get_ai_setting(db, 'ai_api_key')
    if not api_key:
        raise RuntimeError('未配置 AI API Key，请在设置页面配置')

    provider_config = get_ai_provider_config(db)
    base_url = provider_config.get('base_url') or 'https://api.deepseek.com/v1'
    model_name = provider_config.get('default_model') or 'deepseek-chat'

    cache_key = (base_url, model_name, api_key)
    if cache_key not in _model_cache:
        from openai import AsyncOpenAI
        openai_client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        _model_cache[cache_key] = OpenAIModel(
            model_name,
            provider=OpenAIProvider(openai_client=openai_client),
        )
    return _model_cache[cache_key]


def _make_agent(db: Session) -> Agent[AgentDeps, str]:
    model = _build_agent_model(db)
    agent = Agent(
        model,
        deps_type=AgentDeps,
        result_type=str,
        system_prompt=build_system_prompt(),
    )

    @agent.tool(name='run_sql')
    async def run_sql_tool(ctx: RunContext[AgentDeps], sql: str) -> str:
        """Execute a read-only SQL query and return results."""
        await ctx.deps.queue.put({
            'type': 'tool_call',
            'payload': {'tool_name': 'run_sql', 'tool_input': sql},
        })
        result = run_sql(ctx.deps.db, sql)
        ctx.deps.tool_calls.append({
            'tool_name': 'run_sql',
            'tool_input': sql,
            'tool_output': result,
        })
        await ctx.deps.queue.put({
            'type': 'tool_result',
            'payload': {'tool_name': 'run_sql', 'tool_output': result},
        })
        return tool_result_to_text(result)

    @agent.tool(name='get_chat_list')
    async def get_chat_list_tool(ctx: RunContext[AgentDeps], keyword: str = '') -> str:
        """Return the list of monitored chats (id, title, username)."""
        await ctx.deps.queue.put({
            'type': 'tool_call',
            'payload': {'tool_name': 'get_chat_list', 'tool_input': {'keyword': keyword}},
        })
        chats = get_chat_list(ctx.deps.db, keyword=keyword or None, limit=50)
        ctx.deps.tool_calls.append({
            'tool_name': 'get_chat_list',
            'tool_input': {'keyword': keyword},
            'tool_output': chats,
        })
        await ctx.deps.queue.put({
            'type': 'tool_result',
            'payload': {'tool_name': 'get_chat_list', 'tool_output': chats},
        })
        return tool_result_to_text(chats)

    @agent.tool(name='get_database_stats')
    async def get_database_stats_tool(ctx: RunContext[AgentDeps]) -> str:
        """Return approximate row counts for each table."""
        await ctx.deps.queue.put({
            'type': 'tool_call',
            'payload': {'tool_name': 'get_database_stats', 'tool_input': ''},
        })
        stats = get_database_stats(ctx.deps.db)
        ctx.deps.tool_calls.append({
            'tool_name': 'get_database_stats',
            'tool_input': '',
            'tool_output': stats,
        })
        await ctx.deps.queue.put({
            'type': 'tool_result',
            'payload': {'tool_name': 'get_database_stats', 'tool_output': stats},
        })
        return tool_result_to_text(stats)

    return agent


def _reconstruct_tool_args(tool_name: str, tool_input: str | Any) -> dict[str, Any]:
    """Reconstruct pydantic-ai tool arguments from persisted tool_input."""
    if tool_name == 'run_sql':
        return {'sql': tool_input}
    if tool_name == 'get_chat_list':
        if isinstance(tool_input, dict):
            return {'keyword': tool_input.get('keyword', '')}
        try:
            parsed = json.loads(tool_input or '{}')
            return {'keyword': parsed.get('keyword', '')}
        except Exception:
            return {'keyword': ''}
    if tool_name == 'get_database_stats':
        return {}
    return {}


def _history_to_pydantic(messages: list[ChatMessage]) -> list[ModelMessage]:
    """Convert persisted chat messages to pydantic-ai message history."""
    history: list[ModelMessage] = []
    for msg in messages:
        if msg.role == 'user':
            history.append(ModelRequest(parts=[UserPromptPart(content=msg.content or '')]))
        elif msg.role == 'assistant':
            history.append(ModelResponse(parts=[TextPart(content=msg.content or '')]))
        elif msg.role == 'tool':
            try:
                payload = json.loads(msg.content or '{}')
            except Exception:
                payload = {}
            tool_name = msg.tool_name or payload.get('tool_name') or 'unknown'
            tool_input = msg.tool_input or payload.get('tool_input') or ''
            tool_output = msg.tool_output or payload.get('tool_output') or ''
            tool_call_id = payload.get('tool_call_id') or f'tc_{msg.id}'
            tool_args = _reconstruct_tool_args(tool_name, tool_input)
            history.append(ModelResponse(parts=[ToolCallPart(
                tool_name=tool_name,
                args=tool_args,
                tool_call_id=tool_call_id,
            )]))
            history.append(ModelRequest(parts=[ToolReturnPart(
                tool_name=tool_name,
                content=tool_output,
                tool_call_id=tool_call_id,
            )]))
    return history


def _create_session(db: Session, user_id: str, title: str) -> ChatSession:
    session = ChatSession(user_id=user_id, title=title)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _save_user_message(db: Session, session_id: int, content: str) -> ChatMessage:
    msg = ChatMessage(session_id=session_id, role='user', content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _save_tool_messages(db: Session, session_id: int, tool_calls: list[dict[str, Any]]) -> None:
    for idx, call in enumerate(tool_calls):
        payload = {
            'tool_call_id': f'tc_{session_id}_{int(datetime.utcnow().timestamp() * 1000)}_{idx}',
            **call,
        }
        msg = ChatMessage(
            session_id=session_id,
            role='tool',
            content=json.dumps(payload, ensure_ascii=False, default=str),
            tool_name=call.get('tool_name'),
            tool_input=call.get('tool_input') if isinstance(call.get('tool_input'), str) else json.dumps(call.get('tool_input'), ensure_ascii=False, default=str),
            tool_output=call.get('tool_output') if isinstance(call.get('tool_output'), str) else json.dumps(call.get('tool_output'), ensure_ascii=False, default=str),
        )
        db.add(msg)
    db.commit()


def _save_assistant_message(db: Session, session_id: int, question: str, content: str, tool_calls: list[dict[str, Any]]) -> ChatMessage:
    # Persist tool calls/results first so chronological order in history is:
    # user -> tools -> assistant final answer.
    _save_tool_messages(db, session_id, tool_calls)
    msg = ChatMessage(session_id=session_id, role='assistant', content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    session = db.get(ChatSession, session_id)
    if session and (not session.title or session.title == '新对话'):
        title = question[:40].replace('\n', ' ').strip()
        session.title = title or '新对话'
        db.commit()
    return msg


async def _run_agent_stream(
    session_id: int,
    question: str,
    history: list[ModelMessage],
    event_queue: asyncio.Queue[dict[str, Any]],
    db: Session | None = None,
) -> None:
    """Run the agent with full streaming and push all events to the queue.

    Uses `agent.iter()` so the entire tool loop is handled while text and tool
    events are emitted in real time.

    If `db` is provided, it is used and not closed. Otherwise a new SessionLocal
    is created and closed at the end.
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        agent = _make_agent(db)
        deps = AgentDeps(db=db, tool_calls=[], queue=event_queue)

        full_text = ''
        try:
            async with agent.iter(question, deps=deps, message_history=history) as agent_run:
                async for node in agent_run:
                    if isinstance(node, _agent_graph.ModelRequestNode):
                        async with node.stream(agent_run.ctx) as agent_stream:
                            async for event in agent_stream:
                                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                    full_text += event.part.content
                                    await event_queue.put({'type': 'token', 'text': event.part.content})
                                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                                    full_text += event.delta.content_delta
                                    await event_queue.put({'type': 'token', 'text': event.delta.content_delta})
                    elif isinstance(node, _agent_graph.CallToolsNode):
                        # Tool execution is handled by the registered tool functions, which already
                        # emit tool_call/tool_result events. Just drive the node to completion.
                        await node.run(agent_run.ctx)
        except Exception as exc:
            logger.exception('AI chat stream failed')
            await event_queue.put({'type': 'error', 'message': f'AI 调用失败: {exc}'})
            return

        if not full_text.strip():
            full_text = '（AI 没有返回可读的答案）'
        if len(deps.tool_calls) > MAX_TOOL_CALLS_PER_TURN:
            full_text += '\n\n[系统提示：本次查询次数过多，结果可能不完整。]'

        _save_assistant_message(db, session_id, question, full_text, deps.tool_calls)
        await event_queue.put({'type': 'done'})
    finally:
        if own_db:
            db.close()


async def stream_chat_answer(
    user_id: str,
    session_id: int | None,
    question: str,
) -> AsyncIterator[str]:
    """Stream the AI chat response as SSE events with real-time tool usage.

    Events:
    - session: {session_id}
    - thinking: text explaining what the AI is about to do
    - tool_call: {tool_name, tool_input}
    - tool_result: {tool_name, tool_output}
    - token: streamed answer text chunk
    - error: {message}
    - done: {ok}
    """
    question = (question or '').strip()
    if not question:
        yield _sse_event('error', json.dumps({'message': '问题不能为空'}, ensure_ascii=False))
        return
    if len(question) > MAX_QUESTION_LENGTH:
        yield _sse_event('error', json.dumps({'message': f'问题长度不能超过 {MAX_QUESTION_LENGTH} 个字符'}, ensure_ascii=False))
        return

    db = SessionLocal()
    try:
        session = get_or_create_chat_session(db, session_id, user_id)
        history = get_chat_history(db, session.id)
        pydantic_history = _history_to_pydantic(history)
        _save_user_message(db, session.id, question)
        current_session_id = session.id
    finally:
        db.close()

    yield _sse_event('session', json.dumps({'session_id': current_session_id}, ensure_ascii=False))

    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_task = asyncio.create_task(
        _run_agent_stream(current_session_id, question, pydantic_history, event_queue, db=None)
    )

    try:
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=STREAM_TURN_TIMEOUT)
            except asyncio.TimeoutError:
                yield _sse_event('error', json.dumps({'message': 'AI 响应超时，请稍后重试'}, ensure_ascii=False))
                break
            event_type = event.get('type')
            if event_type == 'done':
                yield _sse_event('done', json.dumps({'ok': True}, ensure_ascii=False))
                break
            elif event_type == 'error':
                yield _sse_event('error', json.dumps({'message': event.get('message')}, ensure_ascii=False))
                break
            elif event_type == 'token':
                yield _sse_event('token', event.get('text', ''))
            elif event_type == 'tool_call':
                yield _sse_event('tool_call', json.dumps(event.get('payload', {}), ensure_ascii=False, default=str))
            elif event_type == 'tool_result':
                yield _sse_event('tool_result', json.dumps(event.get('payload', {}), ensure_ascii=False, default=str))
    finally:
        runner_task.cancel()
        try:
            await runner_task
        except asyncio.CancelledError:
            pass


def _sse_event(event: str, data: str) -> str:
    """Format a Server-Sent Events line.

    Multiline payload values are split into multiple `data:` fields so clients
    can reconstruct them correctly.
    """
    safe_data = data.replace('\n', '\ndata: ')
    return f'event: {event}\ndata: {safe_data}\n\n'


def get_or_create_chat_session(
    db: Session,
    session_id: int | None,
    user_id: str,
) -> ChatSession:
    if session_id:
        session = db.get(ChatSession, session_id)
        if session and session.user_id == user_id:
            return session
    return _create_session(db, user_id, '新对话')


def get_chat_history(db: Session, session_id: int) -> list[ChatMessage]:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )


def get_recent_sessions(db: Session, user_id: str, limit: int = 20) -> list[ChatSession]:
    return (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user_id)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit)
        .all()
    )


async def run_chat_turn(
    db: Session,
    session_id: int,
    question: str,
    history: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]]]:
    """Non-streaming fallback: run one user turn and return the full answer."""
    question = (question or '').strip()
    if not question:
        raise ValueError('问题不能为空')
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValueError(f'问题长度不能超过 {MAX_QUESTION_LENGTH} 个字符')

    pydantic_history = _history_to_pydantic(history)
    _save_user_message(db, session_id, question)

    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await _run_agent_stream(session_id, question, pydantic_history, event_queue, db=db)

    full_text_parts: list[str] = []
    turn_tool_calls: list[dict[str, Any]] = []
    while True:
        event = await event_queue.get()
        if event.get('type') == 'done':
            break
        if event.get('type') == 'token':
            full_text_parts.append(event.get('text', ''))
        if event.get('type') == 'tool_call':
            turn_tool_calls.append(event.get('payload', {}))
        if event.get('type') == 'error':
            raise RuntimeError(event.get('message', 'AI 调用失败'))

    return ''.join(full_text_parts), turn_tool_calls

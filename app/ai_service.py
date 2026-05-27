from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import AiSummary, AppSetting, Message, AiUrl, AiUrlAppearance, AiProduct, AiContact
from .config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个 Telegram 频道/群聊监控分析助手。你的任务是分析一段聊天记录，输出结构化 JSON。

## 输出 JSON Schema
你必须严格按以下格式返回，字段不可缺失：
{
  "summary": "string, 100-200字中文总结，概括这批消息的核心内容和讨论趋势",
  "relay_urls": ["string, 以 http:// 或 https:// 开头的完整 URL"],
  "seller_urls": ["string, 以 http:// 或 https:// 开头的完整 URL"],
  "other_urls": ["string, 以 http:// 或 https:// 开头的完整 URL"],
  "top_senders": ["string, 按发言频率排序的前3个发送者名称"],
  "media_summary": "string, 这批消息中图片/视频/文件的简要描述",
  "products": [
    {
      "name": "string, 商品或服务名称",
      "price": "number|null, 价格数字，无法解析则为 null",
      "currency": "string, 货币单位如 CNY/USD/USDT，默认 CNY",
      "seller": "string|null, 卖家联系方式（@username 或其他）",
      "status": "string, available|sold|reserved"
    }
  ],
  "contacts": [
    {
      "type": "string, tg_user|tg_group|email|phone|other",
      "value": "string, 联系方式内容"
    }
  ]
}

## 分类规则
- relay_urls: 节点销售、VPS/服务器销售、VPN 订阅、流量转发类网站
- seller_urls: Telegram 账号/号码销售、接码平台、实名账号交易类网站
- other_urls: 既不属于中转站也不属于号商的链接（普通推广、社交链接等）
- 非 http(s) 开头的不要收录（如 @username、tg://）

## 商品提取规则
- products: 从消息中提取有明确价格的商品/服务信息
  - name: 商品或服务的名称
  - price: 数字价格，无法解析则为 null
  - currency: 货币单位，默认 CNY
  - seller: 卖家的联系方式
  - status: 在售(available)/已售(sold)/预订(reserved)
- 如果没有商品信息，返回空数组 []

## 联系方式提取规则
- contacts: 从消息中提取所有联系方式
  - tg_user: Telegram 用户名（@开头）
  - tg_group: Telegram 群组/频道链接（t.me/xxx）
  - email: 邮箱地址
  - phone: 手机号码（含国际区号）
  - other: 其他社交账号（微信、QQ、Signal 等）
- 去重：相同联系方式只保留一条
- 如果没有联系方式，返回空数组 []

## 注意事项
- 如果某类数据不存在，返回空数组 []，不要省略字段
- summary 必须包含这批消息的主要话题、活跃程度、是否有价值信息
- 所有文本内容必须用中文"""

PROVIDER_CONFIGS = {
    'deepseek': {
        'name': 'DeepSeek',
        'base_url': 'https://api.deepseek.com/v1',
        'default_model': 'deepseek-chat',
        'supports_json_mode': True,
        'api_type': 'openai_compatible',
    },
    'openai': {
        'name': 'OpenAI',
        'base_url': 'https://api.openai.com/v1',
        'default_model': 'gpt-4o-mini',
        'supports_json_mode': True,
        'api_type': 'openai_compatible',
    },
    'claude': {
        'name': 'Claude (Anthropic)',
        'base_url': 'https://api.anthropic.com',
        'default_model': 'claude-sonnet-4-20250514',
        'supports_json_mode': False,
        'api_type': 'anthropic',
    },
    'mimo': {
        'name': 'Mimo AI',
        'base_url': '',
        'default_model': 'mimo-v2.5-pro',
        'supports_json_mode': True,
        'api_type': 'openai_compatible',
    },
    'custom': {
        'name': '自定义 (OpenAI 兼容)',
        'base_url': '',
        'default_model': '',
        'supports_json_mode': True,
        'api_type': 'openai_compatible',
    },
}


def get_ai_setting(db: Session, key: str) -> str | None:
    setting = db.query(AppSetting).filter(AppSetting.key == key).first()
    return setting.value if setting else None


def get_ai_provider_config(db: Session) -> dict:
    provider = get_ai_setting(db, 'ai_provider') or 'deepseek'
    if provider not in PROVIDER_CONFIGS:
        provider = 'deepseek'
    config = dict(PROVIDER_CONFIGS[provider])

    base_url = get_ai_setting(db, 'ai_base_url')
    if base_url:
        config['base_url'] = base_url

    model = get_ai_setting(db, 'ai_model')
    if model:
        config['default_model'] = model

    return config


def _build_message_context(msgs: list[Message]) -> str:
    lines = []
    for m in msgs:
        sender_name = f'user#{m.sender_user_id}' if m.sender_user_id else 'unknown'
        about = ''
        if m.sender and m.sender.about:
            about = f' [bio:{m.sender.about}]'
        dt = m.message_date.strftime('%m-%d %H:%M') if m.message_date else '??'
        media_hint = ''
        if m.has_media and m.meta_json:
            if m.meta_json.get('media_is_image'):
                media_hint = ' [图片]'
            elif m.meta_json.get('media_is_video'):
                media_hint = ' [视频]'
        lines.append(f'[{dt}] {sender_name}{about}{media_hint}: {m.raw_text or ""}')
    full = '\n'.join(lines)
    max_chars = 100000
    if len(full) > max_chars:
        full = full[-max_chars:]
        full = '...(前文已截断)...\n' + full
    return full


def _validate_and_normalize(result: dict) -> dict:
    validated = {
        'summary': '',
        'relay_urls': [],
        'seller_urls': [],
        'other_urls': [],
        'top_senders': [],
        'media_summary': '',
        'products': [],
        'contacts': [],
    }
    if isinstance(result.get('summary'), str) and result['summary'].strip():
        validated['summary'] = result['summary'].strip()

    url_pattern = re.compile(r'^https?://\S+')
    for key in ('relay_urls', 'seller_urls', 'other_urls'):
        raw = result.get(key, [])
        if isinstance(raw, list):
            validated[key] = [u.strip().rstrip('.,;，。；）)]') for u in raw if isinstance(u, str) and url_pattern.match(u.strip())]
        elif isinstance(raw, str):
            validated[key] = [u.strip().rstrip('.,;，。；）)]') for u in re.findall(r'https?://\S+', raw) if url_pattern.match(u)]

    if isinstance(result.get('top_senders'), list):
        validated['top_senders'] = [str(s) for s in result['top_senders'] if s][:3]

    if isinstance(result.get('media_summary'), str) and result['media_summary'].strip():
        validated['media_summary'] = result['media_summary'].strip()

    # Validate products
    allowed_status = {'available', 'sold', 'reserved'}
    if isinstance(result.get('products'), list):
        for p in result['products']:
            if not isinstance(p, dict) or not p.get('name'):
                continue
            product = {
                'name': str(p['name']).strip()[:255],
                'price': None,
                'currency': 'CNY',
                'seller': None,
                'status': 'available',
            }
            if p.get('price') is not None:
                try:
                    product['price'] = float(p['price'])
                except (ValueError, TypeError):
                    pass
            if isinstance(p.get('currency'), str) and p['currency'].strip():
                product['currency'] = p['currency'].strip()[:20].upper()
            if isinstance(p.get('seller'), str) and p['seller'].strip():
                product['seller'] = p['seller'].strip()[:255]
            if isinstance(p.get('status'), str) and p['status'].strip().lower() in allowed_status:
                product['status'] = p['status'].strip().lower()
            validated['products'].append(product)

    # Validate contacts
    allowed_contact_types = {'tg_user', 'tg_group', 'email', 'phone', 'other'}
    if isinstance(result.get('contacts'), list):
        seen_contacts = set()
        for c in result['contacts']:
            if not isinstance(c, dict) or not c.get('value'):
                continue
            contact_type = str(c.get('type', 'other')).strip().lower()
            if contact_type not in allowed_contact_types:
                contact_type = 'other'
            contact_value = str(c['value']).strip()[:255]
            # Deduplicate
            dedup_key = f'{contact_type}:{contact_value.lower()}'
            if dedup_key in seen_contacts:
                continue
            seen_contacts.add(dedup_key)
            validated['contacts'].append({
                'type': contact_type,
                'value': contact_value,
            })

    return validated


def _extract_json_from_text(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\{[^{}]*"summary"[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {'summary': text, 'relay_urls': [], 'seller_urls': [], 'other_urls': []}


async def _call_openai_compatible(api_key: str, base_url: str, model: str, full_text: str, supports_json: bool) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    kwargs = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': f'请分析以下聊天记录：\n\n{full_text}'},
        ],
        'temperature': 0.1,
    }
    if supports_json:
        kwargs['response_format'] = {'type': 'json_object'}

    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        logger.error('OpenAI API call failed: %s', exc)
        raise

    # Handle different response formats
    try:
        # Standard OpenAI format
        content = response.choices[0].message.content or '{}'
    except (AttributeError, IndexError, KeyError) as exc:
        logger.warning('Non-standard OpenAI response format: %s', exc)
        # Try alternative response formats
        try:
            # Some APIs return dict-like response
            if isinstance(response, dict):
                if 'choices' in response:
                    content = response['choices'][0]['message']['content'] or '{}'
                elif 'content' in response:
                    content = response['content'] or '{}'
                elif 'text' in response:
                    content = response['text'] or '{}'
                else:
                    logger.error('Unknown response format: %s', response)
                    raise ValueError(f'无法解析API响应格式: {response}')
            else:
                # Try to access as object with different attribute names
                content = getattr(response, 'content', None) or getattr(response, 'text', None) or '{}'
        except Exception as inner_exc:
            logger.error('Failed to parse response: %s', inner_exc)
            raise ValueError(f'API响应格式不兼容: {response}') from inner_exc

    return _extract_json_from_text(content)


async def _call_anthropic(api_key: str, model: str, full_text: str) -> dict:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT + '\n\n请直接返回 JSON，不要包含其他文本或 markdown 标记。',
        messages=[
            {'role': 'user', 'content': f'请分析以下聊天记录：\n\n{full_text}'},
        ],
        temperature=0.1,
    )
    content = response.content[0].text if response.content else '{}'
    return _extract_json_from_text(content)


async def summarize_text(api_key: str, provider_config: dict, full_text: str) -> dict:
    api_type = provider_config.get('api_type', 'openai_compatible')
    base_url = provider_config.get('base_url', '')
    model = provider_config.get('default_model', '')
    supports_json = provider_config.get('supports_json_mode', True)

    if api_type == 'anthropic':
        parsed = await _call_anthropic(api_key, model, full_text)
    else:
        parsed = await _call_openai_compatible(api_key, base_url, model, full_text, supports_json)

    return _validate_and_normalize(parsed)


async def summarize_messages(chat_id: int, api_key: str, provider_config: dict, msgs: list[Message]) -> dict:
    full_text = _build_message_context(msgs)
    return await summarize_text(api_key, provider_config, full_text)


async def run_summary_for_chat(chat_id: int) -> None:
    db = SessionLocal()
    summary_id: int | None = None
    try:
        api_key = get_ai_setting(db, 'ai_api_key')
        if not api_key:
            logger.warning('AI summary skipped: no API key for chat %s', chat_id)
            return

        provider_config = get_ai_provider_config(db)

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
                db.commit()
            else:
                return

        last = db.query(AiSummary).filter(
            AiSummary.chat_id == chat_id,
            AiSummary.status == 'success',
        ).order_by(AiSummary.id.desc()).first()

        last_msg_id = last.end_message_id if last else 0

        msgs = db.query(Message).filter(
            Message.chat_id == chat_id,
            Message.id > last_msg_id,
        ).order_by(Message.id).limit(settings.ai_summary_batch_size).all()

        if len(msgs) < settings.ai_summary_batch_size:
            return

        start_id = msgs[0].id
        end_id = msgs[-1].id
        full_text = _build_message_context(msgs)

        summary = AiSummary(
            chat_id=chat_id,
            message_count=len(msgs),
            start_message_id=start_id,
            end_message_id=end_id,
            status='running',
            triggered_at=datetime.utcnow(),
        )
        db.add(summary)
        db.commit()
        db.refresh(summary)
        summary_id = summary.id

        db.close()
        result = await summarize_text(api_key, provider_config, full_text)
        db = SessionLocal()
        summary = db.get(AiSummary, summary_id)
        if not summary:
            logger.warning('AI summary row disappeared chat=%s summary_id=%s', chat_id, summary_id)
            return

        summary.summary_text = result.get('summary', '')
        extracted = {}
        if result.get('relay_urls'):
            extracted['relay_urls'] = result['relay_urls']
        if result.get('seller_urls'):
            extracted['seller_urls'] = result['seller_urls']
        if result.get('other_urls'):
            extracted['other_urls'] = result['other_urls']
        if result.get('top_senders'):
            extracted['top_senders'] = result['top_senders']
        if result.get('media_summary'):
            extracted['media_summary'] = result['media_summary']
        if result.get('products'):
            extracted['products'] = result['products']
        if result.get('contacts'):
            extracted['contacts'] = result['contacts']
        summary.extracted_urls = extracted
        summary.status = 'success'
        summary.completed_at = datetime.utcnow()
        db.commit()

        logger.info('AI summary done chat=%s msgs=%d urls=%d products=%d contacts=%d',
                     chat_id, len(msgs),
                     len(extracted.get('relay_urls', [])) + len(extracted.get('seller_urls', [])),
                     len(extracted.get('products', [])),
                     len(extracted.get('contacts', [])))

        _upsert_urls(db, result, chat_id, summary_id)
        _upsert_products(db, result.get('products', []), chat_id, summary_id)
        _upsert_contacts(db, result.get('contacts', []), chat_id, summary_id)

    except Exception as exc:
        logger.exception('AI summary failed chat=%s', chat_id)
        try:
            if summary_id:
                db.close()
                db = SessionLocal()
                summary = db.get(AiSummary, summary_id)
            else:
                summary = None
            if summary is not None:
                summary.status = 'failed'
                summary.error_message = str(exc)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def run_summary_now(chat_id: int, message_count: int = 0) -> int:
    db = SessionLocal()
    try:
        api_key = get_ai_setting(db, 'ai_api_key')
        if not api_key:
            raise RuntimeError('未配置 AI API Key，请在设置页面配置')

        provider_config = get_ai_provider_config(db)

        last = db.query(AiSummary).filter(
            AiSummary.chat_id == chat_id,
            AiSummary.status == 'success',
        ).order_by(AiSummary.id.desc()).first()

        last_msg_id = last.end_message_id if last else 0
        batch = message_count if message_count > 0 else settings.ai_summary_batch_size

        msgs = db.query(Message).filter(
            Message.chat_id == chat_id,
            Message.id > last_msg_id,
        ).order_by(Message.id).limit(batch).all()

        if not msgs:
            raise RuntimeError('没有新消息可分析')

        start_id = msgs[0].id
        end_id = msgs[-1].id
        full_text = _build_message_context(msgs)

        summary = AiSummary(
            chat_id=chat_id,
            message_count=len(msgs),
            start_message_id=start_id,
            end_message_id=end_id,
            status='running',
            triggered_at=datetime.utcnow(),
        )
        db.add(summary)
        db.commit()
        db.refresh(summary)
        summary_id = summary.id

        db.close()
        result = await summarize_text(api_key, provider_config, full_text)
        db = SessionLocal()
        summary = db.get(AiSummary, summary_id)
        if not summary:
            return summary_id

        summary.summary_text = result.get('summary', '')
        extracted = {}
        for key in ('relay_urls', 'seller_urls', 'other_urls', 'top_senders', 'media_summary', 'products', 'contacts'):
            if result.get(key):
                extracted[key] = result[key]
        summary.extracted_urls = extracted
        summary.status = 'success'
        summary.completed_at = datetime.utcnow()
        db.commit()
        _upsert_urls(db, result, chat_id, summary_id)
        _upsert_products(db, result.get('products', []), chat_id, summary_id)
        _upsert_contacts(db, result.get('contacts', []), chat_id, summary_id)
        return summary_id

    except Exception as exc:
        logger.exception('run_summary_now failed chat=%s', chat_id)
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def _extract_domain(url: str) -> str | None:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or None
    except Exception:
        return None


# Well-known trusted domains get a base reputation boost
_TRUSTED_DOMAINS = {
    'github.com', 'gitlab.com', 'bitbucket.org',
    'google.com', 'microsoft.com', 'apple.com',
    'amazon.com', 'aliyun.com', 'tencent.com',
    'baidu.com', 'zhihu.com', 'bilibili.com',
    'telegram.org', 't.me',
}


def _compute_reputation(appearance_count: int, chat_ids_seen: dict | None, domain: str | None) -> float:
    score = 0.3  # base score
    # Appearance boost (logarithmic, capped)
    if appearance_count > 1:
        score += min(0.3, math.log10(appearance_count) * 0.15)
    # Cross-chat boost
    if chat_ids_seen and isinstance(chat_ids_seen, dict):
        chat_count = len(chat_ids_seen)
        if chat_count >= 3:
            score += 0.2
        elif chat_count >= 2:
            score += 0.1
    # Trusted domain boost
    if domain:
        base_domain = '.'.join(domain.split('.')[-2:]) if '.' in domain else domain
        if base_domain in _TRUSTED_DOMAINS:
            score += 0.2
    return round(min(1.0, score), 2)


def _upsert_urls(db: Session, result: dict, chat_id: int | None = None, summary_id: int | None = None) -> None:
    category_map = {
        'relay_urls': 'relay',
        'seller_urls': 'seller',
        'other_urls': 'other',
    }
    now = datetime.utcnow()
    for json_key, category in category_map.items():
        urls = result.get(json_key, [])
        if not isinstance(urls, list):
            continue
        for url in urls:
            if not isinstance(url, str) or not url.strip():
                continue
            h = _url_hash(url)
            domain = _extract_domain(url)
            try:
                existing = db.query(AiUrl).filter(AiUrl.url_hash == h).first()
                if existing:
                    existing.last_seen_at = now
                    existing.appearance_count = (existing.appearance_count or 1) + 1
                    if domain and not existing.domain:
                        existing.domain = domain
                    # Track chat_ids_seen
                    if chat_id:
                        chat_seen = existing.chat_ids_seen or {}
                        if str(chat_id) not in chat_seen:
                            chat_seen[str(chat_id)] = now.isoformat()
                            existing.chat_ids_seen = chat_seen
                    # Update reputation score
                    existing.reputation_score = _compute_reputation(
                        existing.appearance_count, existing.chat_ids_seen, existing.domain
                    )
                else:
                    chat_seen = {str(chat_id): now.isoformat()} if chat_id else None
                    reputation = _compute_reputation(1, chat_seen, domain)
                    db.add(AiUrl(
                        url=url, url_hash=h, category=category, domain=domain,
                        appearance_count=1, chat_ids_seen=chat_seen,
                        reputation_score=reputation,
                        first_seen_at=now, last_seen_at=now
                    ))
                    db.flush()
                # Track appearance
                if chat_id:
                    db.add(AiUrlAppearance(
                        url_id=existing.id if existing else None,
                        chat_id=chat_id, summary_id=summary_id, seen_at=now
                    ))
                    db.flush()
            except Exception as exc:
                db.rollback()
                logger.debug('URL upsert race condition for %s: %s', url, exc)
                try:
                    existing = db.query(AiUrl).filter(AiUrl.url_hash == h).first()
                    if existing:
                        existing.last_seen_at = now
                        existing.appearance_count = (existing.appearance_count or 1) + 1
                        db.flush()
                except Exception:
                    db.rollback()
    db.commit()


def _upsert_products(db: Session, products: list[dict], chat_id: int, summary_id: int | None = None) -> None:
    now = datetime.utcnow()
    for p in products:
        if not p.get('name'):
            continue
        try:
            # Check for duplicate by (chat_id, product_name, price_amount)
            existing = db.query(AiProduct).filter(
                AiProduct.chat_id == chat_id,
                AiProduct.product_name == p['name'],
                AiProduct.price_amount == p.get('price'),
            ).first()
            if existing:
                existing.last_seen_at = now
                if p.get('seller'):
                    existing.seller_contact = p['seller']
                if p.get('status'):
                    existing.status = p['status']
            else:
                db.add(AiProduct(
                    chat_id=chat_id,
                    summary_id=summary_id,
                    product_name=p['name'],
                    price_amount=p.get('price'),
                    price_currency=p.get('currency', 'CNY'),
                    seller_contact=p.get('seller'),
                    status=p.get('status', 'available'),
                    first_seen_at=now,
                    last_seen_at=now,
                ))
                db.flush()
        except Exception as exc:
            db.rollback()
            logger.debug('Product upsert error for %s: %s', p.get('name'), exc)
    db.commit()


def _upsert_contacts(db: Session, contacts: list[dict], chat_id: int, summary_id: int | None = None) -> None:
    now = datetime.utcnow()
    for c in contacts:
        if not c.get('value'):
            continue
        try:
            # Check for duplicate by (chat_id, contact_type, contact_value)
            existing = db.query(AiContact).filter(
                AiContact.chat_id == chat_id,
                AiContact.contact_type == c['type'],
                AiContact.contact_value == c['value'],
            ).first()
            if existing:
                existing.last_seen_at = now
            else:
                db.add(AiContact(
                    chat_id=chat_id,
                    summary_id=summary_id,
                    contact_type=c['type'],
                    contact_value=c['value'],
                    first_seen_at=now,
                    last_seen_at=now,
                ))
                db.flush()
        except Exception as exc:
            db.rollback()
            logger.debug('Contact upsert error for %s: %s', c.get('value'), exc)
    db.commit()

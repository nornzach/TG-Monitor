from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (
    AiSummary,
    AppSetting,
    Message,
    AiUrl,
    AiUrlAppearance,
    AiUrlCategory,
    AiUrlClassificationRun,
    AiUrlClassification,
    AiProduct,
    AiContact,
    AiKeyLead,
    AiKeyLeadRun,
)
from .config import settings

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
URL_TRAILING_PUNCTUATION = '.,;，。；）)]}>'
KEY_LEAD_POSITIVE_PATTERN = re.compile(
    r'(api\s*key|apikey|api-key|\bkey\b|sk-[A-Za-z0-9]|AIza|free\s*credits?|credits?|'
    r'api\s*额度|api\s*余额|免费额度|试用额度|新号额度|绑卡额度|额度号|带额度|'
    r'key商|官站\s*api|官方\s*api|原厂\s*api|openai\s*api|claude\s*api|anthropic\s*api|'
    r'gemini\s*api|google\s*api|grok\s*api|xai\s*api|groq\s*api|openrouter\s*api)',
    re.IGNORECASE,
)
KEY_LEAD_NEGATIVE_PATTERN = re.compile(
    r'(codex|claude\s*code|cursor|windsurf|copilot|chatgpt\s*(plus|pro|team)|claude\s*(pro|max|team)|'
    r'gemini\s*(pro|ultra|advanced)|grok\s*(pro|plus)|会员|成品号|会员号|共享号|独享号|'
    r'资格链接|kyc|卡密|直充|充值|订阅|月卡|年卡|plus|pro|team|max)',
    re.IGNORECASE,
)
KEY_LEAD_STRONG_PATTERN = re.compile(
    r'(api\s*key|apikey|api-key|sk-[A-Za-z0-9]|AIza|官站\s*api|官方\s*api|原厂\s*api|'
    r'api\s*额度|api\s*余额|api\s*free\s*credits?|free\s*credits?|credits?)',
    re.IGNORECASE,
)

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

URL_CLASSIFICATION_SYSTEM_PROMPT = """你是一个 URL 二次分类助手。你的任务是把已经收录的 URL 按业务含义做更细的动态分类。

## 输出 JSON Schema
你必须只返回 JSON 对象，不要输出 markdown，不要输出解释文字：
{
  "items": [
    {
      "url_id": 123,
      "category_slug": "telegram_group",
      "category_name": "Telegram 群组/频道",
      "category_description": "t.me 群组、频道、加群邀请链接",
      "confidence": 0.95,
      "reason": "域名为 t.me，路径指向频道或群组"
    }
  ]
}

## 规则
- 每个输入 URL 必须输出一条 items 记录。
- category_slug 必须是小写英文、数字、下划线，长度不超过 80。
- 优先复用已有分类；如果确实不合适，可以创建新的 category_slug/category_name。
- 分类要比 relay/seller/other 更细，例如 telegram_group、cloud_drive、code_repository、payment_store。
- Telegram 加群/频道链接、夸克网盘、百度网盘、GitHub 项目地址必须分别归到不同细分类。
- 不能删除 URL，不能省略 URL，只做分类和打标。
- confidence 是 0 到 1 之间的小数。
- reason 用中文，简短说明判断依据。"""

DEFAULT_URL_CLASSIFICATION_PROMPT = """请根据 URL 的域名、路径和原始粗分类做细分类。

常见分类偏好：
- t.me、telegram.me、telegram.dog 的群组/频道/邀请链接 => telegram_group
- pan.quark.cn、pan.baidu.com、aliyundrive.com 等网盘分享 => cloud_drive
- github.com、gitlab.com、gitee.com、bitbucket.org 项目地址 => code_repository
- 节点、代理、VPN、VPS、流量转发相关 => relay_service
- Telegram 账号、手机号、接码、实名号交易相关 => account_seller
- 支付、店铺、充值、商品详情页面 => payment_store
- AI 工具、模型 API、提示词、自动化服务 => ai_tool
- 文档、教程、博客文章 => documentation
- 不确定但仍是普通网页 => generic_link"""

KEY_LEAD_SYSTEM_PROMPT = """你是一个 Telegram 聊天记录商机筛选助手。你的任务是从聊天记录中只找出“原厂官方 API key / 原厂官方 API free credits 额度号”供给线索。

## 必须保留的目标
- OpenAI、Anthropic/Claude、Google/Gemini、xAI/Grok、Groq、OpenRouter 等原厂或官方平台的 API key 出售、出租、收购、批发、开车、回收线索。
- 明确提到 API free credits、API 免费额度、API 试用额度、新号 API 额度、绑卡 API 额度、API 余额、credits 号、带 API 额度账号的原厂账号线索。
- 可以包含卖家联系方式、价格、平台、额度、库存、批量供货等信息。

## 必须排除
- 中转站、代理站、转发站、节点、relay、站点余额、第三方聚合 API、非原厂 API 服务。
- Codex 账号、Claude Code 账号、Cursor、Windsurf、Devin、Copilot 等代码工具账号或订阅号。
- ChatGPT Plus/Team/Pro 普通账号、Claude Pro/Max 普通账号、Gemini Pro/Ultra/Advanced/Grok 会员账号、成品号、会员号、共享号、独享号、KYC认证、资格链接、卡密、直充、充值、月卡、年卡。
- 只有“Pro/Plus/会员/成品号/资格链接/KYC/卡密/直充/充值”等词，没有明确出现 API key/key/free credits/API 额度/官站 API 的，一律不要输出。
- 普通 TG 账号、手机号、接码、实名号、店铺链接、网盘链接、教程链接。
- 不要推测“可能是原厂账号”或“可能涉及 API”；必须原文明确说明 API key 或 API credits。

## 输出 JSON Schema
只返回 JSON 对象，不要 markdown，不要解释：
{
  "items": [
    {
      "message_id": 123,
      "lead_type": "api_key",
      "provider": "openai",
      "product_name": "OpenAI API key",
      "offer_text": "string, 对供给内容的简短中文摘要",
      "price": 100,
      "currency": "CNY",
      "confidence": 0.95,
      "reason": "明确出售 OpenAI 原厂 API key"
    }
  ]
}

## 字段规则
- lead_type 只能是 api_key 或 free_credit_account。
- provider 用小写英文，例如 openai、anthropic、google、xai、groq、openrouter、other。
- 不确定是原厂官方 API key/API free credits 的，不要输出。
- 每条命中的消息最多输出 1 条，优先输出最明确的供给线索。
- price 无法识别返回 null；currency 默认 CNY。
- confidence 是 0 到 1 的小数。

## 负例，必须返回空 items
- “Gemini Pro 一年成品号”
- “Gemini Pro 一年资格链接 + Claude KYC认证”
- “Super Grok 2个月卡密直充”
- “GPT Plus / Claude Pro / Gemini Pro 会员账号”
- “中转 API / relay API / 站点余额”"""

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


def get_url_classification_prompt(db: Session) -> str:
    prompt = get_ai_setting(db, 'url_classification_prompt')
    return prompt.strip() if prompt and prompt.strip() else DEFAULT_URL_CLASSIFICATION_PROMPT


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


def _clean_url(raw_url: str) -> str:
    return raw_url.strip().rstrip(URL_TRAILING_PUNCTUATION)


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.findall(text):
        url = _clean_url(match)
        if not url or not URL_PATTERN.match(url):
            continue
        dedup_key = url.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        urls.append(url)
    return urls


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

    for key in ('relay_urls', 'seller_urls', 'other_urls'):
        raw = result.get(key, [])
        if isinstance(raw, list):
            urls: list[str] = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, str):
                    continue
                url = _clean_url(item)
                if not url or not URL_PATTERN.match(url):
                    continue
                dedup_key = url.lower()
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                urls.append(url)
            validated[key] = urls
        elif isinstance(raw, str):
            validated[key] = extract_urls_from_text(raw)

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
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {'summary': text, 'relay_urls': [], 'seller_urls': [], 'other_urls': []}


def _extract_json_object(text: str) -> dict:
    parsed = _extract_json_from_text(text)
    return parsed if isinstance(parsed, dict) else {}


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


async def _call_openai_compatible_json(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    supports_json: bool,
) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    kwargs = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': 0.1,
    }
    if supports_json:
        kwargs['response_format'] = {'type': 'json_object'}
    response = await client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content or '{}'
    return _extract_json_object(content)


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


async def _call_anthropic_json(api_key: str, model: str, system_prompt: str, user_prompt: str) -> dict:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt + '\n\n请直接返回 JSON，不要包含其他文本或 markdown 标记。',
        messages=[
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=0.1,
    )
    content = response.content[0].text if response.content else '{}'
    return _extract_json_object(content)


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


def _normalize_category_slug(value: str | None) -> str:
    slug = re.sub(r'[^a-z0-9_]+', '_', (value or '').strip().lower()).strip('_')
    return slug[:80] or 'other'


def _validate_url_classification_result(result: dict, url_ids: set[int]) -> list[dict]:
    raw_items = result.get('items')
    if raw_items is None:
        raw_items = result.get('classifications')
    if not isinstance(raw_items, list):
        return []

    items = []
    seen: set[int] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            url_id = int(raw.get('url_id'))
        except (TypeError, ValueError):
            continue
        if url_id not in url_ids or url_id in seen:
            continue
        seen.add(url_id)

        slug = _normalize_category_slug(raw.get('category_slug'))
        name = str(raw.get('category_name') or slug.replace('_', ' ')).strip()[:100]
        description = str(raw.get('category_description') or '').strip()[:500] or None
        reason = str(raw.get('reason') or '').strip()[:500] or None
        confidence = raw.get('confidence')
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None

        items.append({
            'url_id': url_id,
            'category_slug': slug,
            'category_name': name,
            'category_description': description,
            'confidence': confidence,
            'reason': reason,
        })
    return items


def _build_key_lead_prompt(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        payload = {
            'message_id': message.id,
            'chat_id': message.chat_id,
            'sender_user_id': message.sender_user_id,
            'time': _iso_for_prompt(message.message_date),
            'text': (message.raw_text or '')[:2000],
        }
        lines.append(json.dumps(payload, ensure_ascii=False))
    return '请逐条筛选下面的聊天消息，每行一个 JSON：\n' + '\n'.join(lines)


def _iso_for_prompt(value: datetime | None) -> str | None:
    return value.isoformat(sep=' ') if value else None


def _normalize_key_provider(value: str | None) -> str:
    provider = re.sub(r'[^a-z0-9_]+', '_', (value or '').strip().lower()).strip('_')
    return provider[:60] or 'other'


def _is_key_lead_candidate(text: str | None) -> bool:
    if not text:
        return False
    if not KEY_LEAD_POSITIVE_PATTERN.search(text):
        return False
    if KEY_LEAD_NEGATIVE_PATTERN.search(text) and not KEY_LEAD_STRONG_PATTERN.search(text):
        return False
    return True


def _is_valid_key_lead_item(text: str | None, item: dict) -> bool:
    if not _is_key_lead_candidate(text):
        return False
    combined = ' '.join([
        text or '',
        str(item.get('product_name') or ''),
        str(item.get('offer_text') or ''),
        str(item.get('reason') or ''),
    ])
    if KEY_LEAD_NEGATIVE_PATTERN.search(combined) and not KEY_LEAD_STRONG_PATTERN.search(combined):
        return False
    return True


def _validate_key_lead_result(result: dict, messages: list[Message]) -> list[dict]:
    message_by_id = {message.id: message for message in messages}
    raw_items = result.get('items')
    if raw_items is None:
        raw_items = result.get('leads')
    if not isinstance(raw_items, list):
        return []

    allowed_types = {'api_key', 'free_credit_account'}
    items = []
    seen_messages: set[int] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            message_id = int(raw.get('message_id'))
        except (TypeError, ValueError):
            continue
        message = message_by_id.get(message_id)
        if not message or message_id in seen_messages:
            continue
        if not _is_valid_key_lead_item(message.raw_text, raw):
            continue
        lead_type = str(raw.get('lead_type') or '').strip().lower()
        if lead_type not in allowed_types:
            continue

        price = raw.get('price')
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        confidence = raw.get('confidence')
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None

        seen_messages.add(message_id)
        items.append({
            'message_id': message_id,
            'lead_type': lead_type,
            'provider': _normalize_key_provider(raw.get('provider')),
            'product_name': str(raw.get('product_name') or '').strip()[:255] or None,
            'offer_text': str(raw.get('offer_text') or '').strip()[:1000] or None,
            'price_amount': price,
            'price_currency': str(raw.get('currency') or 'CNY').strip()[:20].upper(),
            'confidence': confidence,
            'reason': str(raw.get('reason') or '').strip()[:500] or None,
        })
    return items


def _build_url_classification_prompt(urls: list[AiUrl], categories: list[AiUrlCategory], business_prompt: str) -> str:
    category_lines = [
        f'- {c.slug}: {c.name}' + (f'；{c.description}' if c.description else '')
        for c in categories
        if c.is_active
    ]
    url_lines = [
        json.dumps({
            'url_id': u.id,
            'url': u.url,
            'domain': u.domain,
            'legacy_category': u.category,
            'appearance_count': u.appearance_count,
        }, ensure_ascii=False)
        for u in urls
    ]
    return (
        f'{business_prompt}\n\n'
        '已有分类：\n' + '\n'.join(category_lines) + '\n\n'
        '待分类 URL，每行一个 JSON：\n' + '\n'.join(url_lines)
    )


async def classify_urls(api_key: str, provider_config: dict, urls: list[AiUrl], categories: list[AiUrlCategory], business_prompt: str) -> list[dict]:
    api_type = provider_config.get('api_type', 'openai_compatible')
    base_url = provider_config.get('base_url', '')
    model = provider_config.get('default_model', '')
    supports_json = provider_config.get('supports_json_mode', True)
    user_prompt = _build_url_classification_prompt(urls, categories, business_prompt)

    if api_type == 'anthropic':
        parsed = await _call_anthropic_json(api_key, model, URL_CLASSIFICATION_SYSTEM_PROMPT, user_prompt)
    else:
        parsed = await _call_openai_compatible_json(
            api_key, base_url, model, URL_CLASSIFICATION_SYSTEM_PROMPT, user_prompt, supports_json
        )
    return _validate_url_classification_result(parsed, {u.id for u in urls})


async def classify_key_leads(api_key: str, provider_config: dict, messages: list[Message]) -> list[dict]:
    api_type = provider_config.get('api_type', 'openai_compatible')
    base_url = provider_config.get('base_url', '')
    model = provider_config.get('default_model', '')
    supports_json = provider_config.get('supports_json_mode', True)
    user_prompt = _build_key_lead_prompt(messages)

    if api_type == 'anthropic':
        parsed = await _call_anthropic_json(api_key, model, KEY_LEAD_SYSTEM_PROMPT, user_prompt)
    else:
        parsed = await _call_openai_compatible_json(
            api_key, base_url, model, KEY_LEAD_SYSTEM_PROMPT, user_prompt, supports_json
        )
    return _validate_key_lead_result(parsed, messages)


def _apply_url_classifications(db: Session, run: AiUrlClassificationRun, urls: list[AiUrl], items: list[dict]) -> tuple[int, int]:
    now = datetime.utcnow()
    url_by_id = {u.id: u for u in urls}
    category_by_slug = {
        c.slug: c for c in db.query(AiUrlCategory).filter(AiUrlCategory.is_active.is_(True)).all()
    }
    created_categories = 0
    processed = 0

    for item in items:
        url = url_by_id.get(item['url_id'])
        if not url:
            continue
        category = category_by_slug.get(item['category_slug'])
        if not category:
            category = AiUrlCategory(
                slug=item['category_slug'],
                name=item['category_name'],
                description=item['category_description'],
                source='ai',
                is_active=True,
            )
            db.add(category)
            db.flush()
            category_by_slug[category.slug] = category
            created_categories += 1

        db.query(AiUrlClassification).filter(
            AiUrlClassification.url_id == url.id,
            AiUrlClassification.is_primary.is_(True),
        ).update({AiUrlClassification.is_primary: False})

        existing = db.query(AiUrlClassification).filter(
            AiUrlClassification.url_id == url.id,
            AiUrlClassification.category_id == category.id,
        ).first()
        if existing:
            existing.run_id = run.id
            existing.confidence = item['confidence']
            existing.reason = item['reason']
            existing.is_primary = True
            existing.created_at = now
        else:
            db.add(AiUrlClassification(
                url_id=url.id,
                category_id=category.id,
                run_id=run.id,
                confidence=item['confidence'],
                reason=item['reason'],
                is_primary=True,
                created_at=now,
            ))

        url.primary_category_id = category.id
        url.classification_run_id = run.id
        url.classification_status = 'classified'
        url.classified_at = now
        url.classification_error = None
        processed += 1

    classified_ids = {item['url_id'] for item in items}
    for url in urls:
        if url.id in classified_ids:
            continue
        url.classification_status = 'failed'
        url.classification_run_id = run.id
        url.classification_error = 'AI response did not include this URL'

    return processed, created_categories


async def run_url_classification_once(batch_size: int | None = None, include_classified: bool = False) -> dict:
    db = SessionLocal()
    run_id: int | None = None
    urls: list[AiUrl] = []
    try:
        api_key = get_ai_setting(db, 'ai_api_key')
        if not api_key:
            return {'status': 'skipped', 'reason': 'missing_api_key', 'processed': 0}

        running = db.query(AiUrlClassificationRun).filter(
            AiUrlClassificationRun.status == 'running',
        ).first()
        if running:
            timeout_at = datetime.utcnow() - timedelta(minutes=settings.ai_summary_running_timeout_minutes)
            if running.started_at and running.started_at < timeout_at:
                running.status = 'failed'
                running.error_message = 'URL classification timed out and was released for retry'
                running.finished_at = datetime.utcnow()
                db.commit()
            else:
                return {'status': 'skipped', 'reason': 'already_running', 'processed': 0}

        limit = batch_size or settings.url_classification_batch_size
        limit = min(max(limit, 1), 200)
        query = db.query(AiUrl).order_by(AiUrl.id.asc())
        if not include_classified:
            query = query.filter(
                (AiUrl.classification_status.is_(None)) |
                (AiUrl.classification_status.in_(('pending', 'failed')))
            )
        urls = query.limit(limit).all()
        if not urls:
            return {'status': 'skipped', 'reason': 'no_pending_urls', 'processed': 0}

        run = AiUrlClassificationRun(
            status='running',
            batch_size=limit,
            total_urls=len(urls),
            prompt_version='url-classification-v1',
            started_at=datetime.utcnow(),
        )
        db.add(run)
        db.flush()
        run_id = run.id
        for url in urls:
            url.classification_status = 'running'
            url.classification_run_id = run.id
            url.classification_error = None
        categories = db.query(AiUrlCategory).filter(AiUrlCategory.is_active.is_(True)).order_by(AiUrlCategory.slug.asc()).all()
        provider_config = get_ai_provider_config(db)
        business_prompt = get_url_classification_prompt(db)
        db.commit()

        db.expunge_all()
        db.close()
        items = await classify_urls(api_key, provider_config, urls, categories, business_prompt)

        db = SessionLocal()
        run = db.get(AiUrlClassificationRun, run_id)
        urls = db.query(AiUrl).filter(AiUrl.classification_run_id == run_id).all()
        if not run:
            return {'status': 'failed', 'reason': 'run_missing', 'processed': 0}
        processed, created_categories = _apply_url_classifications(db, run, urls, items)
        run.processed_urls = processed
        run.created_categories = created_categories
        run.status = 'success' if processed else 'failed'
        if not processed:
            run.error_message = 'AI response did not classify any URL'
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.info('URL classification run=%s processed=%d created_categories=%d', run.id, processed, created_categories)
        return {
            'status': run.status,
            'run_id': run.id,
            'processed': processed,
            'total': len(urls),
            'created_categories': created_categories,
        }
    except Exception as exc:
        logger.exception('URL classification failed run=%s', run_id)
        try:
            db.rollback()
            if run_id:
                run = db.get(AiUrlClassificationRun, run_id)
                if run:
                    run.status = 'failed'
                    run.error_message = str(exc)
                    run.finished_at = datetime.utcnow()
                for url in db.query(AiUrl).filter(AiUrl.classification_run_id == run_id).all():
                    url.classification_status = 'failed'
                    url.classification_error = str(exc)
                db.commit()
        except Exception:
            pass
        return {'status': 'failed', 'reason': str(exc), 'processed': 0}
    finally:
        db.close()


def _key_lead_hash(message_id: int, item: dict) -> str:
    raw = '|'.join([
        str(message_id),
        item.get('lead_type') or '',
        item.get('provider') or '',
        item.get('product_name') or '',
        item.get('seller_contact') or '',
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _sender_snapshot(message: Message) -> dict:
    sender = message.sender
    if not sender:
        return {'telegram_id': None, 'username': None, 'display_name': None, 'contact': None}

    username = sender.username.strip() if sender.username else None
    display_name = ' '.join(part for part in (sender.first_name, sender.last_name) if part) or username
    contact = f'@{username}' if username else f'tg://user?id={sender.telegram_id}'
    return {
        'telegram_id': sender.telegram_id,
        'username': username,
        'display_name': display_name,
        'contact': contact,
    }


def _apply_key_leads(db: Session, run: AiKeyLeadRun, messages: list[Message], items: list[dict]) -> int:
    now = datetime.utcnow()
    message_by_id = {message.id: message for message in messages}
    processed = 0

    for item in items:
        message = message_by_id.get(item['message_id'])
        if not message:
            continue
        sender = _sender_snapshot(message)
        content_hash = _key_lead_hash(message.id, item)
        existing = db.query(AiKeyLead).filter(AiKeyLead.content_hash == content_hash).first()
        if existing:
            existing.run_id = run.id
            existing.price_amount = item['price_amount']
            existing.price_currency = item['price_currency']
            existing.offer_text = item['offer_text']
            existing.seller_contact = sender['contact']
            existing.seller_telegram_id = sender['telegram_id']
            existing.seller_username = sender['username']
            existing.seller_display_name = sender['display_name']
            existing.confidence = item['confidence']
            existing.reason = item['reason']
            existing.source_text = (message.raw_text or '')[:2000]
            existing.last_seen_at = now
        else:
            db.add(AiKeyLead(
                run_id=run.id,
                message_id=message.id,
                chat_id=message.chat_id,
                sender_user_id=message.sender_user_id,
                lead_type=item['lead_type'],
                provider=item['provider'],
                product_name=item['product_name'],
                offer_text=item['offer_text'],
                price_amount=item['price_amount'],
                price_currency=item['price_currency'],
                seller_contact=sender['contact'],
                seller_telegram_id=sender['telegram_id'],
                seller_username=sender['username'],
                seller_display_name=sender['display_name'],
                confidence=item['confidence'],
                reason=item['reason'],
                source_text=(message.raw_text or '')[:2000],
                content_hash=content_hash,
                first_seen_at=now,
                last_seen_at=now,
            ))
        processed += 1
    return processed


async def run_key_lead_analysis_once(batch_size: int | None = None) -> dict:
    db = SessionLocal()
    run_id: int | None = None
    try:
        api_key = get_ai_setting(db, 'ai_api_key')
        if not api_key:
            return {'status': 'skipped', 'reason': 'missing_api_key', 'processed': 0}

        running = db.query(AiKeyLeadRun).filter(AiKeyLeadRun.status == 'running').first()
        if running:
            timeout_at = datetime.utcnow() - timedelta(minutes=settings.ai_summary_running_timeout_minutes)
            if running.started_at and running.started_at < timeout_at:
                running.status = 'failed'
                running.error_message = 'Key lead analysis timed out and was released for retry'
                running.finished_at = datetime.utcnow()
                db.commit()
            else:
                return {'status': 'skipped', 'reason': 'already_running', 'processed': 0}

        last = db.query(AiKeyLeadRun).filter(AiKeyLeadRun.status == 'success').order_by(AiKeyLeadRun.end_message_id.desc()).first()
        last_message_id = last.end_message_id if last else 0
        limit = min(max(batch_size or settings.key_lead_analysis_batch_size, 1), 500)
        scanned_messages = db.query(Message).filter(
            Message.id > last_message_id,
            Message.raw_text.isnot(None),
            Message.raw_text != '',
        ).order_by(Message.id.asc()).limit(limit).all()
        if not scanned_messages:
            return {'status': 'skipped', 'reason': 'no_new_messages', 'processed': 0}

        messages = [message for message in scanned_messages if _is_key_lead_candidate(message.raw_text)]
        if not messages:
            run = AiKeyLeadRun(
                status='success',
                batch_size=limit,
                total_messages=len(scanned_messages),
                processed_leads=0,
                start_message_id=scanned_messages[0].id,
                end_message_id=scanned_messages[-1].id,
                prompt_version='key-lead-v2',
                started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
            )
            db.add(run)
            db.commit()
            return {'status': 'success', 'run_id': run.id, 'messages': len(scanned_messages), 'processed': 0}

        run = AiKeyLeadRun(
            status='running',
            batch_size=limit,
            total_messages=len(scanned_messages),
            start_message_id=scanned_messages[0].id,
            end_message_id=scanned_messages[-1].id,
            prompt_version='key-lead-v2',
            started_at=datetime.utcnow(),
        )
        db.add(run)
        db.commit()
        run_id = run.id

        provider_config = get_ai_provider_config(db)
        db.expunge_all()
        db.close()
        items = await classify_key_leads(api_key, provider_config, messages)

        db = SessionLocal()
        run = db.get(AiKeyLeadRun, run_id)
        if not run:
            return {'status': 'failed', 'reason': 'run_missing', 'processed': 0}
        stored_messages = db.query(Message).filter(Message.id.in_([m.id for m in messages])).all()
        processed = _apply_key_leads(db, run, stored_messages, items)
        run.processed_leads = processed
        run.status = 'success'
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.info('Key lead analysis run=%s messages=%d leads=%d', run.id, len(messages), processed)
        return {'status': 'success', 'run_id': run.id, 'messages': len(messages), 'processed': processed}
    except Exception as exc:
        logger.exception('Key lead analysis failed run=%s', run_id)
        try:
            db.rollback()
            if run_id:
                run = db.get(AiKeyLeadRun, run_id)
                if run:
                    run.status = 'failed'
                    run.error_message = str(exc)
                    run.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
        return {'status': 'failed', 'reason': str(exc), 'processed': 0}
    finally:
        db.close()


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
            url = _clean_url(url)
            if not URL_PATTERN.match(url):
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
                    existing = AiUrl(
                        url=url, url_hash=h, category=category, domain=domain,
                        appearance_count=1, chat_ids_seen=chat_seen,
                        reputation_score=reputation,
                        first_seen_at=now, last_seen_at=now
                    )
                    db.add(existing)
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


def upsert_discovered_urls(urls: list[str], category: str = 'other', chat_id: int | None = None) -> int:
    category_key = {
        'relay': 'relay_urls',
        'seller': 'seller_urls',
        'other': 'other_urls',
    }.get(category, 'other_urls')
    normalized_urls = extract_urls_from_text('\n'.join(urls))
    if not normalized_urls:
        return 0

    db = SessionLocal()
    try:
        _upsert_urls(db, {category_key: normalized_urls}, chat_id=chat_id)
        return len(normalized_urls)
    finally:
        db.close()


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

"""Daily cross-chat market brief generator."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai_service import get_ai_setting, get_ai_provider_config, _call_openai_compatible_json
from .analysis_advanced import aggregate_market_intelligence, aggregate_seller_prices
from .db import SessionLocal
from .models import DailyMarketBrief, MarketIntelligenceItem, SystemEvent, AiProduct, ProductPriceHistory

logger = logging.getLogger(__name__)

BRIEF_SYSTEM_PROMPT = """你是一个 Telegram 监控平台的市场情报分析师。请根据过去 24 小时的跨群数据，生成一份简洁的中文每日市场简报。

必须输出 JSON，格式如下：
{
  "title": "2024-06-11 TG 市场简报",
  "content": "5-8 段中文分析，包含：整体行情、风险信号、价格波动、热点话题、关键账号/卖家变化。避免编造未出现的信息。",
  "hot_topics": ["话题1", "话题2"],
  "risk_level": "low|medium|high",
  "signals": [
    {"type": "risk|price|hotspot|gossip|market", "content": "简短描述", "severity": "low|medium|high"}
  ],
  "price_moves": [
    {"product_name": "商品名", "direction": "up|down|stable", "note": "价格变化描述"}
  ]
}

注意事项：
- content 必须是完整的中文报告文本，不要只列 bullet points。
- 风险等级根据风险信号数量和严重程度判断。
- 价格变化只写数据里确实出现过的商品。
- 不确定的信息标注为“传闻/需核实”。"""


def _collect_signals(db: Session, hours: int = 24) -> dict:
    """Collect raw signals for the brief prompt."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    # Market intelligence items
    rows = db.query(MarketIntelligenceItem).filter(
        MarketIntelligenceItem.created_at >= cutoff,
    ).order_by(MarketIntelligenceItem.created_at.desc()).limit(200).all()

    signals: dict[str, list[str]] = {
        'market': [], 'risk': [], 'price': [], 'legal': [],
        'hotspot': [], 'gossip': [], 'key_people': [], 'timeline': [],
    }
    for row in rows:
        signals.setdefault(row.item_type, []).append(row.content)

    # Top topics by frequency
    topic_counter: dict[str, int] = {}
    for content in signals.get('hotspot', []) + signals.get('gossip', []):
        topic_counter[content] = topic_counter.get(content, 0) + 1
    top_topics = sorted(topic_counter.items(), key=lambda x: x[1], reverse=True)[:15]

    # Price changes
    price_moves = []
    recent_products = db.query(AiProduct).filter(
        AiProduct.last_seen_at >= cutoff,
    ).limit(100).all()
    for p in recent_products:
        history = db.query(ProductPriceHistory).filter(
            ProductPriceHistory.product_id == p.id,
        ).order_by(ProductPriceHistory.recorded_at.asc()).all()
        if len(history) >= 2:
            old = history[0].price_amount
            new = history[-1].price_amount
            if old is not None and new is not None and old != new:
                direction = 'up' if new > old else 'down'
                price_moves.append({
                    'product_name': p.product_name,
                    'direction': direction,
                    'old_price': old,
                    'new_price': new,
                    'currency': p.price_currency,
                })

    # System events / anomalies
    events = db.query(SystemEvent).filter(
        SystemEvent.created_at >= cutoff,
    ).order_by(SystemEvent.created_at.desc()).limit(50).all()

    return {
        'signals': {k: v[:20] for k, v in signals.items()},
        'top_topics': [{'topic': t, 'count': c} for t, c in top_topics],
        'price_moves': price_moves,
        'events': [{'type': e.event_type, 'severity': e.severity, 'title': e.title} for e in events],
    }


def _build_brief_prompt(raw: dict) -> str:
    return json.dumps(raw, ensure_ascii=False, indent=2)


def _extract_brief_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = __import__('re').search(r'\{.*\}', text, __import__('re').DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return {
        'title': '每日市场简报',
        'content': text,
        'hot_topics': [],
        'risk_level': 'low',
        'signals': [],
        'price_moves': [],
    }


async def generate_daily_brief(brief_date: date | None = None) -> dict:
    """Generate and store a daily market brief."""
    target_date = brief_date or date.today()
    db = SessionLocal()
    try:
        api_key = get_ai_setting(db, 'ai_api_key')
        if not api_key:
            return {'status': 'skipped', 'reason': 'missing_api_key'}

        provider_config = get_ai_provider_config(db)

        # Check if already generated
        existing = db.query(DailyMarketBrief).filter(
            func.date(DailyMarketBrief.brief_date) == target_date,
        ).first()

        raw = _collect_signals(db)
        user_prompt = _build_brief_prompt(raw)

        parsed = await _call_openai_compatible_json(
            api_key,
            provider_config.get('base_url', ''),
            provider_config.get('default_model', ''),
            BRIEF_SYSTEM_PROMPT,
            user_prompt,
            provider_config.get('supports_json_mode', True),
        )
        result = _extract_brief_json(json.dumps(parsed, ensure_ascii=False))

        title = result.get('title') or f'{target_date} TG 市场简报'
        content = result.get('content') or '（AI 未返回正文）'
        hot_topics = result.get('hot_topics', [])
        risk_level = result.get('risk_level', 'low')
        signals = result.get('signals', [])
        price_moves = result.get('price_moves', [])

        if existing:
            existing.title = title
            existing.content = content
            existing.signals_json = signals
            existing.hot_topics_json = hot_topics
            existing.risk_level = risk_level
            existing.price_moves_json = price_moves
        else:
            db.add(DailyMarketBrief(
                brief_date=datetime.combine(target_date, datetime.min.time()),
                title=title,
                content=content,
                signals_json=signals,
                hot_topics_json=hot_topics,
                risk_level=risk_level,
                price_moves_json=price_moves,
            ))
        db.commit()
        logger.info('Daily market brief generated for %s', target_date)
        return {
            'status': 'success',
            'date': target_date.isoformat(),
            'title': title,
            'risk_level': risk_level,
        }
    except Exception as exc:
        logger.exception('Daily brief generation failed')
        return {'status': 'failed', 'reason': str(exc)}
    finally:
        db.close()

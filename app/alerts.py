from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AlertRule, AlertMatch, Message, MonitoredChat

logger = logging.getLogger(__name__)

# ReDoS protection: limit regex pattern complexity
_MAX_REGEX_PATTERN_LENGTH = 200
_SUSPICIOUS_PATTERNS = re.compile(
    r'(\(.+\+){3,}'       # 3+ nested quantifiers with +
    r'|(\(.+\*){3,}'      # 3+ nested quantifiers with *
    r'|(\.\+){3,}'        # 3+ .+ in sequence
    r'|(\.\*){3,}'        # 3+ .* in sequence
    r'|\([^)]*[+*]\)[+*]' # Nested quantifiers: (x+)+ or (x*)*
    r'|\([^)]*[+*]\){2,}' # Quantified group: (x+){2,}
)


def _is_safe_regex(pattern: str) -> bool:
    """Check if regex pattern is safe from ReDoS attacks."""
    if len(pattern) > _MAX_REGEX_PATTERN_LENGTH:
        return False
    if _SUSPICIOUS_PATTERNS.search(pattern):
        return False
    return True


def check_message_alerts(db: Session, message: Message, chat: MonitoredChat) -> list[AlertMatch]:
    """Check a message against all active alert rules and create matches."""
    matches = []
    now = datetime.utcnow()

    rules = db.execute(
        select(AlertRule).where(AlertRule.is_active.is_(True))
    ).scalars().all()

    if not rules:
        return matches

    text = message.normalized_text or message.raw_text or ''
    if not text.strip():
        return matches

    for rule in rules:
        # Check chat filter
        if rule.chat_ids_filter:
            allowed = rule.chat_ids_filter if isinstance(rule.chat_ids_filter, list) else []
            if allowed and chat.telegram_id not in allowed:
                continue

        matched = False
        matched_text = None

        if rule.pattern_type == 'keyword':
            keyword = rule.pattern.strip().lower()
            if keyword in text.lower():
                matched = True
                # Extract surrounding context
                idx = text.lower().index(keyword)
                start = max(0, idx - 30)
                end = min(len(text), idx + len(keyword) + 30)
                matched_text = text[start:end]
        elif rule.pattern_type == 'regex':
            if not _is_safe_regex(rule.pattern):
                logger.warning('Skipping potentially unsafe regex pattern in rule %s: %s', rule.id, rule.pattern[:50])
                continue
            try:
                m = re.search(rule.pattern, text, re.IGNORECASE)
                if m:
                    matched = True
                    matched_text = m.group(0)
            except re.error as exc:
                logger.warning('Invalid regex pattern in rule %s: %s', rule.id, exc)
                continue

        if matched:
            match = AlertMatch(
                rule_id=rule.id,
                message_id=message.id,
                chat_id=chat.id,
                matched_text=matched_text[:500] if matched_text else None,
                matched_at=now,
                is_read=False,
            )
            db.add(match)
            matches.append(match)

    if matches:
        db.flush()
        logger.info('Message %s triggered %d alert(s)', message.id, len(matches))

    return matches

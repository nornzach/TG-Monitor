from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.ai_service import _extract_domain, _url_hash, extract_urls_from_text
from app.db import SessionLocal
from app.models import AiUrl, AiUrlAppearance, AiUrlClassification


def has_non_ascii(value: str) -> bool:
    return any(ord(char) > 127 for char in value)


def merge_chat_seen(left: dict | None, right: dict | None) -> dict | None:
    merged = dict(left or {})
    merged.update(right or {})
    return merged or None


def main() -> None:
    db = SessionLocal()
    repaired = 0
    merged = 0
    skipped = 0
    try:
        dirty_urls = db.query(AiUrl).all()
        for dirty in dirty_urls:
            if not has_non_ascii(dirty.url):
                continue

            candidates = extract_urls_from_text(dirty.url)
            if not candidates:
                skipped += 1
                continue

            clean_url = candidates[0]
            if clean_url == dirty.url:
                skipped += 1
                continue

            clean_hash = _url_hash(clean_url)
            target = db.query(AiUrl).filter(AiUrl.url_hash == clean_hash).first()
            if target and target.id != dirty.id:
                target.appearance_count = (target.appearance_count or 0) + (dirty.appearance_count or 0)
                target.chat_ids_seen = merge_chat_seen(target.chat_ids_seen, dirty.chat_ids_seen)
                target.last_seen_at = max(target.last_seen_at, dirty.last_seen_at)
                target.first_seen_at = min(target.first_seen_at, dirty.first_seen_at)
                db.query(AiUrlAppearance).filter(AiUrlAppearance.url_id == dirty.id).update({
                    AiUrlAppearance.url_id: target.id,
                })
                db.query(AiUrlClassification).filter(AiUrlClassification.url_id == dirty.id).delete()
                db.delete(dirty)
                merged += 1
            else:
                dirty.url = clean_url
                dirty.url_hash = clean_hash
                dirty.domain = _extract_domain(clean_url)
                dirty.classification_status = dirty.classification_status or 'pending'
                repaired += 1

        db.commit()
        print({'repaired': repaired, 'merged': merged, 'skipped': skipped})
    finally:
        db.close()


if __name__ == '__main__':
    main()

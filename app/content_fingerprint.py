"""Content fingerprinting for cross-chat message deduplication using SimHash."""
from __future__ import annotations

import hashlib
import logging
from typing import Iterable

import jieba
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Message, MessageFingerprint

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> list[str]:
    """Extract weighted tokens from text."""
    if not text:
        return []
    tokens: list[str] = []
    # Chinese tokens
    for tok in jieba.lcut(text, cut_all=False):
        tok = tok.strip().lower()
        if len(tok) > 1:
            tokens.append(tok)
    # English words
    import re
    for tok in re.findall(r'[a-z][a-z0-9]{2,}', text.lower()):
        tokens.append(tok)
    return tokens


def _word_hash(word: str, bits: int = 64) -> list[int]:
    """Return a vector of +1/-1 for the word's hash."""
    h = hashlib.sha256(word.encode('utf-8')).hexdigest()
    # Use first 16 hex chars -> 64 bits
    val = int(h[:16], 16)
    vec = []
    for i in range(bits):
        if (val >> i) & 1:
            vec.append(1)
        else:
            vec.append(-1)
    return vec


def simhash(text: str, bits: int = 64) -> int:
    """Compute SimHash of text. Returns integer fingerprint."""
    tokens = _tokenize(text)
    if not tokens:
        return 0
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    vec = [0] * bits
    for word, weight in counts.items():
        wh = _word_hash(word, bits)
        for i in range(bits):
            vec[i] += wh[i] * weight
    fingerprint = 0
    for i in range(bits):
        if vec[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Hamming distance between two 64-bit fingerprints."""
    x = a ^ b
    dist = 0
    while x:
        dist += 1
        x &= x - 1
    return dist


def fingerprint_to_hex(fp: int) -> str:
    return f'{fp:016x}'


def hex_to_fingerprint(h: str) -> int:
    return int(h, 16)


def compute_similarity_hash(fp: int, bands: int = 8) -> str:
    """Compute a locality-sensitive hash for fast candidate lookup.
    Bands=8 => 8 chars per band for 64-bit fingerprint."""
    hx = fingerprint_to_hex(fp)
    # Simple banding: split 16-hex into bands segments
    band_size = len(hx) // bands
    parts = []
    for i in range(bands):
        start = i * band_size
        end = start + band_size
        parts.append(hx[start:end])
    return '|'.join(parts)


def find_similar_fingerprints(db: Session, fp: int, threshold: int = 3) -> list[MessageFingerprint]:
    """Find existing fingerprints within Hamming distance threshold."""
    hx = fingerprint_to_hex(fp)
    # Fast candidate filter: exact match on any band
    bands = compute_similarity_hash(fp).split('|')
    candidates = []
    seen_ids: set[int] = set()
    # Query by exact hex match first (cheap)
    exact = db.execute(
        select(MessageFingerprint).where(MessageFingerprint.fingerprint_hash == hx)
    ).scalars().all()
    for row in exact:
        if row.id not in seen_ids:
            candidates.append(row)
            seen_ids.add(row.id)
    # Query by band similarity (using LIKE for simplicity)
    for band in bands:
        pattern = f'%|{band}|%'
        # Also check start/end of similarity_hash
        rows = db.execute(
            select(MessageFingerprint).where(
                MessageFingerprint.similarity_hash.like(f'%{band}%')
            )
        ).scalars().all()
        for row in rows:
            if row.id not in seen_ids:
                seen_ids.add(row.id)
                # Verify actual hamming distance
                try:
                    other_fp = hex_to_fingerprint(row.fingerprint_hash)
                    if hamming_distance(fp, other_fp) <= threshold:
                        candidates.append(row)
                except Exception:
                    pass
    return candidates


def save_fingerprint(db: Session, message_id: int, text: str, threshold: int = 3) -> MessageFingerprint:
    """Compute and save fingerprint, linking duplicates."""
    fp = simhash(text)
    hx = fingerprint_to_hex(fp)
    sim_hx = compute_similarity_hash(fp)

    existing = db.execute(
        select(MessageFingerprint).where(MessageFingerprint.fingerprint_hash == hx)
    ).scalar_one_or_none()
    if existing:
        existing.duplicate_count = (existing.duplicate_count or 0) + 1
        return existing

    similar = find_similar_fingerprints(db, fp, threshold=threshold)
    canonical_id = None
    if similar:
        # Link to the oldest similar fingerprint's message
        canonical = min(similar, key=lambda r: r.id)
        canonical_id = canonical.message_id
        canonical.duplicate_count = (canonical.duplicate_count or 0) + 1

    row = MessageFingerprint(
        message_id=message_id,
        fingerprint_hash=hx,
        similarity_hash=sim_hx,
        canonical_message_id=canonical_id,
        duplicate_count=0,
    )
    db.add(row)
    db.flush()
    return row


def batch_fingerprint_messages(db: Session, messages: Iterable[Message], threshold: int = 3) -> dict[int, MessageFingerprint]:
    """Process multiple messages and return map of message_id -> fingerprint."""
    results: dict[int, MessageFingerprint] = {}
    for msg in messages:
        text = msg.normalized_text or msg.raw_text or ''
        try:
            fp = save_fingerprint(db, msg.id, text, threshold=threshold)
            results[msg.id] = fp
        except Exception as exc:
            logger.warning('Fingerprint failed for message %s: %s', msg.id, exc)
    return results

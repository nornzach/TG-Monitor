import re
from collections import Counter

import jieba

from .config import settings

BASE_STOPWORDS = {
    'the', 'and', 'for', 'that', 'this', 'with', 'have', 'from', 'you', 'your', 'http', 'https',
    'www', 'com', 'are', 'was', 'will', 'can', 'but', 'not', 'all', 'has', 'had', '一个', '我们', '你们',
    '他们', '自己', '没有', '可以', '就是', '还是', '已经', '因为', '如果', '然后', '这个', '那个', '什么', '一下',
    '一下子', '现在', '今天', '昨天', '明天', '这里', '那里', '以及', '进行', '需要', '感觉', '非常', '比较',
}


def get_stopwords() -> set[str]:
    extra = {item.strip().lower() for item in settings.stopwords_extra.split(',') if item.strip()}
    return BASE_STOPWORDS | extra


def normalize_text(text: str | None) -> str:
    if not text:
        return ''
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'[@#][\w一-鿿_-]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_keywords(text: str | None, limit: int = 12) -> list[tuple[str, int]]:
    clean = normalize_text(text)
    if not clean:
        return []

    stopwords = get_stopwords()
    tokens: list[str] = []
    for token in jieba.lcut(clean, cut_all=False):
        token = token.strip().lower()
        if not token or token in stopwords:
            continue
        if re.fullmatch(r'[\W_]+', token):
            continue
        if len(token) == 1 and not re.search(r'[a-z0-9]', token):
            continue
        tokens.append(token)

    latin_tokens = [
        token.lower() for token in re.findall(r'[A-Za-z][A-Za-z0-9_\-]{1,30}', clean)
        if token.lower() not in stopwords
    ]
    tokens.extend(latin_tokens)

    counter = Counter(tokens)
    return counter.most_common(limit)

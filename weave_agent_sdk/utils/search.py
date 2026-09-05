"""CJK 感知的检索分词。

SQLite/File 的 knowledge 检索用 LIKE 子串匹配；中文无空格，整句会成为一个
token 而失效。此处对 CJK 文本追加字符 bigram，使「客户投诉支付…」能命中
「客户投诉：支付…」这类仅差标点/助词的内容（docs/issues/011）。
"""
from __future__ import annotations


def search_tokens(query: str) -> list[str]:
    """把查询拆成可供 LIKE 匹配的 token 列表（含 CJK bigram 兜底）。

    - 英文/混合：按空白分词，保持原 token。
    - 纯 CJK 长词（>=3 字）：追加字符 bigram，突破无空格导致的整句匹配失效。
    """
    tokens = [t for t in (query or "").split() if t]
    if not tokens:
        return tokens
    for t in list(tokens):
        if len(t) >= 3 and any(_is_cjk(ch) for ch in t):
            for i in range(len(t) - 1):
                tokens.append(t[i:i + 2])
    return tokens


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF      # CJK 统一汉字
        or 0x3400 <= cp <= 0x4DBF   # 扩展 A
        or 0x3040 <= cp <= 0x30FF   # 日文假名
    )

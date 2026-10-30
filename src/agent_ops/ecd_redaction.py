"""Deterministic local pattern redaction; counts are occurrences, not people."""
import json
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

# Precedence prevents a phone number from being counted again as an order number.
PATTERNS = (
    ('email', re.compile(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}',re.IGNORECASE), '[EMAIL]'),
    ('mobile', re.compile(r'(?<!\d)(?:(?:\+?86|0086)[ -]?)?1[3-9]\d{9}(?!\d)'), '[MOBILE]'),
    ('order_labeled', re.compile(r'((?:订单(?:号|编号|号码)?|交易(?:号|编号)|运单号|物流单号|单号)[:：#]?)([A-Za-z0-9][A-Za-z0-9_\-]{5,})'), '[ORDER]'),
    ('long_number', re.compile(r'(?<!\d)\d{12,}(?!\d)'), '[IDENTIFIER]'),
    ('address_labeled',re.compile(r'((?:收货地址|收件地址|寄送地址|退货地址|地址)[:：]?)([\u4e00-\u9fffA-Za-z0-9][^，。！？；,\n\[\]]{1,79})'), '[ADDRESS]'),
    ('address_region',re.compile(r'(?:[\u4e00-\u9fff]{2,8}(?:省|自治区))?[\u4e00-\u9fff]{2,12}(?:市|区|县|镇|乡|村)[^，。！？；,\n\[\]]{0,60}(?:路|街|巷|弄|号|栋|单元|室)[^，。！？；,\n\[\]]{0,20}'), '[ADDRESS]'),
)


def redact_ecd(value: str) -> tuple[str, dict[str,int]]:
    counts: Counter[str] = Counter()
    for name,pattern,placeholder in PATTERNS:
        def replace(match: re.Match[str], pattern_name: str = name, replacement: str = placeholder) -> str:
            counts[pattern_name] += 1
            return (match.group(1) if pattern_name in {'order_labeled','address_labeled'} else '')+replacement
        value=pattern.sub(replace,value)
    return value,dict(counts)


def _text_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for part in value.values():
            yield from _text_values(part)
    elif isinstance(value, list):
        for part in value:
            yield from _text_values(part)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value)


def outbound_is_redacted(messages: list[dict[str,str]]) -> bool:
    for message in messages:
        content = message.get('content', '')
        try:
            value = json.loads(content)
        except ValueError:
            value = content
        # Scan the actual fields, not JSON quotes/commas accidentally extending
        # a short utterance into a false address across structural boundaries.
        if any(redact_ecd(part)[1] for part in _text_values(value)):
            return False
    return True


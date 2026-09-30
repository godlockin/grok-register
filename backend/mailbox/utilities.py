"""邮箱渠道共享的小型解析工具。"""

from __future__ import annotations

import re
import secrets
import string
from email import message_from_string
from email.header import decode_header, make_header
from typing import Any, List, Optional


def generate_username(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(max(3, length)))


# domain^weight, e.g. "123.xyz^3" or just "123.xyz" (weight 1).
_DOMAIN_WEIGHT_RE = re.compile(r"^(?P<domain>[^,，\s^]+?)\s*\^\s*(?P<weight>\d+)?$")
# Reject absurd weights so a typo cannot starve every other domain.
MAX_DOMAIN_WEIGHT = 1000


def parse_weighted_domains(raw: str) -> list[tuple[str, int]]:
    """Parse ``defaultDomains`` into ``(domain, weight)`` pairs.

    Accepts the plain comma/space separated form and an optional ``^N``
    suffix: ``"a.xyz^3, b.xyz"`` weights a.xyz three times as heavily as
    b.xyz. A bare domain means weight 1, and repeating a domain still works as
    before, so existing configurations keep their behaviour.
    """

    pairs: list[tuple[str, int]] = []
    text = str(raw or "")
    # "a.xyz ^ 3" must survive as one token, so remove spaces around a caret
    # before splitting on separators.
    text = re.sub(r"\s*\^\s*", "^", text)
    for chunk in re.split(r"[,，\s]+", text):
        token = chunk.strip()
        if not token:
            continue
        match = _DOMAIN_WEIGHT_RE.match(token)
        if match is None:
            # Not a domain^weight token; treat the whole chunk as a domain.
            pairs.append((token, 1))
            continue
        domain = match.group("domain")
        weight_text = match.group("weight")
        if not domain:
            continue
        try:
            weight = int(weight_text) if weight_text else 1
        except ValueError:
            weight = 1
        pairs.append((domain, max(1, min(weight, MAX_DOMAIN_WEIGHT))))
    return pairs


def weighted_domain_schedule(
    raw: str,
) -> list[str]:
    """Expand weighted domains into a repeating round-robin sequence.

    A deterministic schedule is used instead of sampling so the configured
    ratio actually holds: for ``a^3, b^1`` every window of four picks
    contains three ``a`` and one ``b``, rather than converging on the ratio only
    on average. The caller walks the sequence with a cursor and wraps it, so the
    weights apply across however many registrations are run.
    """

    pairs = parse_weighted_domains(raw)
    if not pairs:
        return []
    # Merge duplicates so repeating a domain adds its weights together.
    merged: dict[str, int] = {}
    order: list[str] = []
    for domain, weight in pairs:
        if domain not in merged:
            merged[domain] = 0
            order.append(domain)
        merged[domain] += weight
    schedule: list[str] = []
    for domain in order:
        schedule.extend([domain] * merged[domain])
    return schedule


def pick_list_payload(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return [item for item in data["results"] if isinstance(item, dict)]
        if isinstance(data.get("hydra:member"), list):
            return [item for item in data["hydra:member"] if isinstance(item, dict)]
        if isinstance(data.get("data"), list):
            return [item for item in data["data"] if isinstance(item, dict)]
        if isinstance(data.get("messages"), list):
            return [item for item in data["messages"] if isinstance(item, dict)]
        if isinstance(data.get("data"), dict):
            nested = data.get("data") or {}
            if isinstance(nested.get("messages"), list):
                return [item for item in nested["messages"] if isinstance(item, dict)]
    return []


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# 验证码形如 I6R-B2W：必须全大写，否则邮件模板里的 CSS 类名（如 sm-w-per-100）会被误判。
_CODE_TOKEN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"
_CODE_WITH_CONTEXT_RE = re.compile(
    r"(?:code|验证码)\s*(?:is|：|:)?\s*\b(" + _CODE_TOKEN + r")\b", re.IGNORECASE
)
_CODE_BARE_RE = re.compile(r"\b(" + _CODE_TOKEN + r")\b")
_NUMERIC_CODE_RES = [
    re.compile(
        r"(?:verification|confirmation|confirm|your|xai|grok)?\s*(?:verification|confirmation|confirm|your)?\s*code\s*(?:is|为|是|：|:|\s)+\s*(\d{3}-\d{3})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:verification|confirmation|confirm|your|xai|grok)?\s*(?:verification|confirmation|confirm|your)?\s*code\s*(?:is|为|是|：|:|\s)+\s*(\d{4,8})",
        re.IGNORECASE,
    ),
    re.compile(r"验证码\s*(?:是|为|：|:)?\s*(\d{3}-\d{3})"),
    re.compile(r"验证码\s*(?:是|为|：|:|\s)+\s*(\d{4,8})"),
    re.compile(r"verification\s+code[:\s]+(\d{4,8})", re.IGNORECASE),
    re.compile(r"your\s+code[:\s]+(\d{4,8})", re.IGNORECASE),
    re.compile(r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
]


def strip_html(html: str) -> str:
    """剥掉 HTML 标签，取纯文本。

    必须先删除 script/style 块与注释：只删尖括号的话，<style> 里的 CSS 正文
    会原样留在结果里，其中的类名（如 .sm-w-per-100）会被验证码正则误命中。
    """
    if not html:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
    cleaned = _COMMENT_RE.sub(" ", cleaned)
    return _TAG_RE.sub(" ", cleaned)


def looks_like_raw_email(value: str) -> bool:
    """粗判是否为 RFC822 原文：开头若干行里出现邮件头即认为是。"""
    if not value:
        return False
    for line in value.lstrip().splitlines()[:12]:
        if not line.strip():
            break
        if re.match(r"^[A-Za-z\-]{2,40}:\s", line):
            return True
    return False


def _decode_mime_header(value: str) -> str:
    """解 RFC2047 编码字（=?UTF-8?B?...?=）；失败时原样返回。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_raw_email(raw: str) -> dict:
    """把 RFC822 原文解成 {"subject", "text"}。

    cloudflare_temp_email 的 /api/mails、/admin/mails 按设计只回原始 MIME，
    不保证带已解析的 subject/text/html。直接对原文跑验证码正则会漏：
    base64 正文整段是乱码，quoted-printable 的软换行（I6R=\\n-B2W）会把
    验证码劈成两半，主题还可能是 =?UTF-8?B?...?= 编码字。
    """
    if not raw:
        return {"subject": "", "text": ""}
    try:
        message = message_from_string(raw)
    except Exception:
        return {"subject": "", "text": raw}

    subject = _decode_mime_header(str(message.get("Subject", "") or ""))
    chunks: List[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            raw_payload = part.get_payload()
            body = raw_payload if isinstance(raw_payload, str) else ""
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body = payload.decode("utf-8", errors="replace")
        if not body.strip():
            continue
        chunks.append(strip_html(body) if content_type == "text/html" else body)

    if not chunks:
        # 没有可识别的 text/* part（如整封就是裸正文），退回原文，避免丢内容。
        return {"subject": subject, "text": raw}
    return {"subject": subject, "text": "\n".join(chunks)}


def _match_code(pattern: re.Pattern, source: str) -> Optional[str]:
    """取第一个含字母的匹配，纯数字串（如 100-200）不是验证码。"""
    for match in pattern.finditer(source):
        token = match.group(1)
        if any(ch.isalpha() for ch in token):
            return token
    return None


def extract_verification_code(text: str, subject: str = "") -> Optional[str]:
    subject = subject or ""
    text = text or ""
    # 渠道适配器通常已清理 HTML；这里再做一次兜底，避免直接传入原始
    # HTML 时把 style/script 中的数字片段误当验证码。
    if "<" in text:
        text = strip_html(text)
    # 主题最干净，优先；正文里带 code 关键字的上下文次之，裸 token 最后。
    for pattern in (_CODE_WITH_CONTEXT_RE, _CODE_BARE_RE):
        for source in (subject, text):
            code = _match_code(pattern, source)
            if code:
                return code
    for pattern in _NUMERIC_CODE_RES:
        match = pattern.search(text) or pattern.search(subject)
        if match:
            return match.group(1)
    return None

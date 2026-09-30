"""haoweichi.com 免费临时邮箱渠道适配器。免认证、免验证码、纯正 .com 域名 (cankaohe.com)。"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Callable, List, Optional, Tuple

from backend.mailbox.utilities import extract_verification_code, strip_html

API_BASE_DEFAULT = "https://www.haoweichi.com"

HttpGet = Callable[..., Any]


def normalize_base(base_url: str = "") -> str:
    base = str(base_url or API_BASE_DEFAULT).strip().rstrip("/")
    return base or API_BASE_DEFAULT


def create_mailbox(
    http_get: HttpGet,
    base_url: str = "",
    max_retries: int = 2,
) -> Tuple[str, str]:
    """生成临时邮箱地址，返回 (email, token)。遇到 429 快速重试后抛出以便无缝触发备用通道。"""
    base = normalize_base(base_url)
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = http_get(
                f"{base}/v1/mail/address",
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=12,
            )
            status_code = getattr(resp, "status_code", None)
            if status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                raise Exception("haoweichi HTTP 429: RATE_LIMITED")
            resp.raise_for_status()
            payload = resp.json() or {}
            if not payload.get("success"):
                if payload.get("error", {}).get("code") == "RATE_LIMITED":
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    raise Exception("haoweichi API RATE_LIMITED")
                raise Exception(f"haoweichi 创建邮箱失败: {payload}")
            data = payload.get("data") or {}
            address = data.get("address")
            if not address:
                raise Exception(f"haoweichi 未返回邮箱地址: {payload}")
            return address, "haoweichi"
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            if "429" in err_str or "rate_limited" in err_str or "too many" in err_str:
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
            raise
    raise last_exc or Exception("haoweichi 创建邮箱触发频控")


def get_messages(http_get: HttpGet, base_url: str, email: str) -> List[dict]:
    """读取收件箱邮件列表。"""
    base = normalize_base(base_url)
    safe_email = urllib.parse.quote(email, safe="@.")
    resp = http_get(
        f"{base}/v1/mail/inbox?address={safe_email}",
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    data = payload.get("data") or {}
    emails = data.get("emails")
    if isinstance(emails, list):
        return [m for m in emails if isinstance(m, dict)]
    return []


def wait_for_code(
    http_get: HttpGet,
    base_url: str,
    email: str,
    *,
    timeout: int = 90,
    poll_interval: int = 5,
    extract_code: Callable[[str, str], Optional[str]] = extract_verification_code,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询收取验证码。"""
    deadline = time.time() + timeout
    seen_ids = set()
    resend_triggered = False
    resend_after = time.time() + min(40, timeout * 0.6)

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)

        if resend_callback and not resend_triggered and time.time() >= resend_after:
            resend_triggered = True
            if log_callback:
                log_callback("[*] Haoweichi 超时未收到验证码，触发重发验证码...")
            try:
                resend_callback()
            except Exception as exc:
                if log_callback:
                    log_callback(f"[!] 触发重发验证码异常: {exc}")

        try:
            messages = get_messages(http_get, base_url, email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Haoweichi 拉取邮件失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = str(msg.get("id") or msg.get("mail_id") or "")
            if msg_id and msg_id in seen_ids:
                continue

            subject = str(msg.get("subject") or "")
            body = str(msg.get("content") or msg.get("body") or msg.get("html") or msg.get("text") or "")
            code_field = str(msg.get("code") or "")

            if msg_id:
                seen_ids.add(msg_id)

            if log_callback:
                log_callback(f"[Debug] Haoweichi 收到邮件: {subject or msg_id}")

            # 1. 检查 API 是否直接解析出验证码
            if code_field:
                extracted = extract_code(code_field, subject)
                if extracted:
                    if log_callback:
                        log_callback(f"[*] Haoweichi 接口直接返回验证码: {extracted}")
                    return extracted

            # 2. 从正文和主题提取
            combined = f"{subject}\n{strip_html(body)}" if body else subject
            code = extract_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Haoweichi 从邮件正文中提取到验证码: {code}")
                return code

        sleep_with_cancel(poll_interval, cancel_callback)

    raise Exception(f"Haoweichi 在 {timeout}s 内未收到验证码邮件")

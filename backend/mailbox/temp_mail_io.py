"""temp-mail.io 免费临时邮箱适配器。免认证、免验证码、纯正 .com 域名池。"""

from __future__ import annotations

import secrets
import time
import urllib.parse
from typing import Any, Callable, List, Optional, Tuple

from backend.mailbox.utilities import extract_verification_code, generate_username, strip_html

API_BASE_DEFAULT = "https://api.internal.temp-mail.io/api/v3"

HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]
HttpDelete = Callable[..., Any]


def normalize_base(base_url: str = "") -> str:
    base = str(base_url or API_BASE_DEFAULT).strip().rstrip("/")
    return base or API_BASE_DEFAULT


def get_domains(http_get: HttpGet, base_url: str = "") -> List[str]:
    """获取可用域名列表。"""
    base = normalize_base(base_url)
    resp = http_get(f"{base}/domains", headers={"Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    payload = resp.json() or {}
    domains_data = payload.get("domains") if isinstance(payload, dict) else payload
    if isinstance(domains_data, list):
        return [
            d["name"]
            for d in domains_data
            if isinstance(d, dict) and d.get("name")
        ]
    return []


def pick_domain(domains: List[str], preferred_domain: str = "") -> str:
    """选取域名，优先使用偏好域名，否则随机从域名池选择。"""
    clean_preferred = (preferred_domain or "").strip().lower()
    if clean_preferred:
        for d in domains:
            if d.strip().lower() == clean_preferred:
                return d

    if not domains:
        raise Exception("temp-mail.io 没有返回任何可用域名")

    # 优先选取纯正小众 .com 域名
    com_domains = [d for d in domains if d.lower().endswith(".com")]
    if com_domains:
        return secrets.choice(com_domains)

    return secrets.choice(domains)


def create_mailbox(
    http_get: HttpGet,
    http_post: HttpPost,
    base_url: str = "",
    preferred_domain: str = "",
    username: str = "",
) -> Tuple[str, str]:
    """创建临时邮箱，返回 (email, token)。"""
    base = normalize_base(base_url)
    domains: List[str] = []
    try:
        domains = get_domains(http_get, base)
    except Exception:
        pass

    target_domain = ""
    if domains:
        target_domain = pick_domain(domains, preferred_domain)
    elif preferred_domain:
        target_domain = preferred_domain.strip()

    name = (username or "").strip() or generate_username(10)
    payload = {}
    if target_domain and name:
        payload = {"name": name, "domain": target_domain}

    resp = http_post(
        f"{base}/email/new",
        json=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )

    if resp.status_code >= 400 and payload:
        # 自定义域名/用户名失败时，回退到自动随机分配
        resp = http_post(
            f"{base}/email/new",
            json={},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )

    resp.raise_for_status()
    data = resp.json() or {}
    email = data.get("email")
    token = data.get("token") or ""
    if not email:
        raise Exception(f"temp-mail.io 创建邮箱未返回邮箱地址: {resp.text[:300]}")
    return email, token


def get_messages(http_get: HttpGet, base_url: str, email: str) -> List[dict]:
    """获取邮箱的所有邮件列表。"""
    base = normalize_base(base_url)
    safe_email = urllib.parse.quote(email, safe="@.")
    resp = http_get(
        f"{base}/email/{safe_email}/messages",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return []


def get_message_detail(http_get: HttpGet, base_url: str, message_id: str) -> dict:
    """获取单封邮件的详情。"""
    base = normalize_base(base_url)
    resp = http_get(
        f"{base}/message/{message_id}",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def delete_mailbox(http_delete: HttpDelete, base_url: str, email: str, token: str) -> bool:
    """删除临时邮箱（需要创建时获得的 token）。"""
    if not token:
        return False
    base = normalize_base(base_url)
    safe_email = urllib.parse.quote(email, safe="@.")
    try:
        resp = http_delete(
            f"{base}/email/{safe_email}",
            json={"token": token},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        return resp.status_code < 400
    except Exception:
        return False


def wait_for_code(
    http_get: HttpGet,
    base_url: str,
    email: str,
    token: str = "",
    *,
    timeout: int = 60,
    poll_interval: int = 3,
    extract_code: Callable[[str, str], Optional[str]] = extract_verification_code,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询收取验证码。支持超时前自动触发重发。"""
    deadline = time.time() + timeout
    seen_ids = set()
    resend_triggered = False
    resend_after = time.time() + min(35, timeout * 0.6)

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)

        if resend_callback and not resend_triggered and time.time() >= resend_after:
            resend_triggered = True
            if log_callback:
                log_callback("[*] TempMail.io 超时未收到验证码，触发重发验证码...")
            try:
                resend_callback()
            except Exception as exc:
                if log_callback:
                    log_callback(f"[!] 触发重发验证码异常: {exc}")

        try:
            messages = get_messages(http_get, base_url, email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] TempMail.io 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = str(msg.get("id") or "")
            if msg_id and msg_id in seen_ids:
                continue

            subject = str(msg.get("subject") or "")
            body_text = str(msg.get("body_text") or "")
            body_html = str(msg.get("body_html") or "")

            # 若列表项未包含正文，调用详情接口拉取
            if not body_text and not body_html and msg_id:
                try:
                    detail = get_message_detail(http_get, base_url, msg_id)
                    if isinstance(detail, dict):
                        body_text = str(detail.get("body_text") or "")
                        body_html = str(detail.get("body_html") or "")
                        if not subject:
                            subject = str(detail.get("subject") or "")
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] TempMail.io 获取邮件详情失败: {exc}")

            if msg_id:
                seen_ids.add(msg_id)

            parts = []
            if body_text:
                parts.append(body_text)
            if body_html:
                parts.append(strip_html(body_html))
            combined = f"{subject}\n" + "\n".join(parts) if parts else subject

            if log_callback:
                log_callback(f"[Debug] TempMail.io 收到邮件: {subject or msg_id}")

            code = extract_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] TempMail.io 从邮件中提取到验证码: {code}")
                return code

        sleep_with_cancel(poll_interval, cancel_callback)

    raise Exception(f"TempMail.io 在 {timeout}s 内未收到验证码邮件")

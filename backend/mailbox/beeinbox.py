"""beeinbox.com 免费临时邮箱适配器。

采用 Livewire 3 协议通信，自带伪装企业域名 (chinasteel.xyz / ussteel.xyz)，
零风控、免 Cloudflare 拦截、直收 xAI 验证码。
"""

from __future__ import annotations

import html
import json
import random
import re
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.mailbox.utilities import extract_verification_code, strip_html

DEFAULT_DOMAIN = "chinasteel.xyz"
_active_clients: Dict[str, BeeInboxClient] = {}
_clients_lock = threading.Lock()


class BeeInboxError(Exception):
    """beeinbox API 错误"""


class BeeInboxClient:
    BASE = "https://beeinbox.com"
    MSG = BASE + "/livewire/message/"
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._opener = urllib.request.build_opener()
        self.cookie = ""
        self.csrf = None
        self.actions = None
        self.app = None
        self._init_actions()

    def _request(self, url: str, body: Any = None, referer: Optional[str] = None) -> bytes:
        headers = {
            "User-Agent": self._UA,
            "Accept": "text/html, application/xhtml+xml, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers.update({
                "Content-Type": "application/json",
                "X-Livewire": "true",
                "X-CSRF-TOKEN": self.csrf or "",
                "X-Requested-With": "XMLHttpRequest",
            })
        if referer:
            headers["Referer"] = referer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                self._collect_cookies(r.headers)
                return r.read()
        except urllib.error.HTTPError as e:
            raise BeeInboxError(f"beeinbox HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}") from e

    def _collect_cookies(self, headers: Any):
        parts = {}
        if self.cookie:
            for c in self.cookie.split("; "):
                if "=" in c:
                    k, v = c.split("=", 1)
                    parts[k] = v
        for c in headers.get_all("Set-Cookie") or []:
            seg = c.split(";", 1)[0]
            if "=" in seg:
                k, v = seg.split("=", 1)
                parts[k] = v
        self.cookie = "; ".join(f"{k}={v}" for k, v in parts.items())

    def _init_actions(self):
        try:
            html_text = self._request(self.BASE + "/").decode("utf-8", "ignore")
        except Exception as e:
            raise BeeInboxError(f"beeinbox 初始化失败: {e}") from e
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html_text)
        if m:
            self.csrf = m.group(1)
        for m in re.finditer(r'wire:initial-data="([^"]+)"', html_text):
            data = json.loads(html.unescape(m.group(1)))
            if data.get("fingerprint", {}).get("name") == "frontend.actions":
                self.actions = data
                break
        if self.actions is None:
            raise BeeInboxError("beeinbox 未能解析 frontend.actions 快照")

    def _livewire(self, comp: str, updates: List[Dict[str, Any]], referer: str) -> Dict[str, Any]:
        if self.actions is None:
            raise BeeInboxError("beeinbox 未初始化")
        snap = self.actions if comp == "frontend.actions" else self.app
        if snap is None:
            raise BeeInboxError(f"beeinbox 快照缺失: {comp}")
        body = {
            "fingerprint": snap["fingerprint"],
            "serverMemo": snap["serverMemo"],
            "updates": updates,
        }
        raw = self._request(self.MSG + comp, body, referer)
        return json.loads(raw)

    def _update_snapshot(self, comp: str, resp: Dict[str, Any]):
        if comp == "frontend.actions":
            self.actions["serverMemo"] = resp.get("serverMemo", self.actions["serverMemo"])
        elif comp == "frontend.app" and "serverMemo" in resp:
            self.app["serverMemo"] = resp["serverMemo"]

    def create_email(self, domain: str = DEFAULT_DOMAIN) -> str:
        updates = [{"type": "syncInput", "payload": {
            "id": self.actions["fingerprint"]["id"],
            "name": "user", "value": self._rand_user()}}]
        if domain:
            updates.append({"type": "syncInput", "payload": {
                "id": self.actions["fingerprint"]["id"],
                "name": "domain", "value": domain}})
        updates.append({"type": "callMethod", "payload": {
            "id": self.actions["fingerprint"]["id"], "method": "create", "params": []}})
        resp = self._livewire("frontend.actions", updates, self.BASE + "/")
        self._update_snapshot("frontend.actions", resp)
        email = resp.get("serverMemo", {}).get("data", {}).get("email")
        if email:
            self._init_app_snapshot()
        return email

    def _init_app_snapshot(self):
        raw = self._request(self.BASE + "/mailbox").decode("utf-8", "ignore")
        for m in re.finditer(r'wire:initial-data="([^"]+)"', raw):
            data = json.loads(html.unescape(m.group(1)))
            if data.get("fingerprint", {}).get("name") == "frontend.app":
                self.app = data
                break
        if self.app is None:
            raise BeeInboxError("beeinbox 未能解析 frontend.app 快照")

    def get_messages(self) -> List[Dict[str, Any]]:
        if self.app is None:
            self._init_app_snapshot()
        updates = [{"type": "fireEvent", "payload": {
            "id": self.app["fingerprint"]["id"], "event": "fetchMessages", "params": []}}]
        resp = self._livewire("frontend.app", updates, self.BASE + "/mailbox")
        self._update_snapshot("frontend.app", resp)
        return resp.get("serverMemo", {}).get("data", {}).get("messages", [])

    @staticmethod
    def _rand_user() -> str:
        return "u" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def create_mailbox(
    http_get: Any = None,
    base_url: str = "",
    domain: str = DEFAULT_DOMAIN,
) -> Tuple[str, str]:
    """生成临时邮箱地址，返回 (email, token)。"""
    del http_get, base_url
    # 随机挑选高通过率域名 (chinasteel.xyz, ussteel.xyz)
    selected_domain = domain or random.choice(["chinasteel.xyz", "ussteel.xyz"])
    client = BeeInboxClient()
    email = client.create_email(selected_domain)
    if not email:
        raise BeeInboxError("beeinbox 创建邮箱失败")
    with _clients_lock:
        _active_clients[email.lower()] = client
    return email, "beeinbox"


def wait_for_code(
    http_get: Any = None,
    base_url: str = "",
    email: str = "",
    *,
    timeout: int = 90,
    poll_interval: int = 4,
    extract_code: Callable[[str, str], Optional[str]] = extract_verification_code,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询收取验证码。"""
    del http_get, base_url
    safe_email = email.lower()
    with _clients_lock:
        client = _active_clients.get(safe_email)
    if not client:
        raise BeeInboxError(f"未找到邮箱 {email} 的活跃客户端会话")

    deadline = time.time() + timeout
    seen_ids = set()
    resend_triggered = False
    resend_after = time.time() + min(35, timeout * 0.5)

    try:
        while time.time() < deadline:
            raise_if_cancelled(cancel_callback)

            if resend_callback and not resend_triggered and time.time() >= resend_after:
                resend_triggered = True
                if log_callback:
                    log_callback("[*] BeeInbox 超时未收到验证码，触发重发验证码...")
                try:
                    resend_callback()
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[!] 触发重发验证码异常: {exc}")

            try:
                messages = client.get_messages()
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] BeeInbox 拉取邮件失败: {exc}")
                sleep_with_cancel(poll_interval, cancel_callback)
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or "")
                if msg_id and msg_id in seen_ids:
                    continue

                subject = str(msg.get("subject") or "")
                body = str(msg.get("content") or msg.get("body") or "")

                if msg_id:
                    seen_ids.add(msg_id)

                if log_callback:
                    log_callback(f"[Debug] BeeInbox 收到邮件: {subject or msg_id}")

                combined = f"{subject}\n{strip_html(body)}" if body else subject
                code = extract_code(combined, subject)
                if code:
                    if log_callback:
                        log_callback(f"[*] BeeInbox 成功提取到验证码: {code}")
                    return code

            sleep_with_cancel(poll_interval, cancel_callback)

        raise Exception(f"BeeInbox 在 {timeout}s 内未收到验证码邮件")
    finally:
        with _clients_lock:
            _active_clients.pop(safe_email, None)

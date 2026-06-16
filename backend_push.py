"""将礼物/房间事件推送到 minibackground（WebSocket 或 HTTP）。"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Dict, Optional

import requests

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore


def _log(msg: str) -> None:
    print(msg, flush=True)


class BackendPusher:
    """异步推送采集事件到后端。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled"))
        self.ws_url = str(cfg.get("ws_url") or "").strip()
        self.http_url = str(cfg.get("http_url") or "").strip()
        self.secret = str(cfg.get("secret") or "").strip()
        self.anchor_id = int(cfg.get("anchor_id") or 0)
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._ws_app = None
        self._ws_lock = threading.Lock()

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.ws_url and not self.http_url:
            _log("[推送] backend_push 已启用但未配置 ws_url / http_url")
            return
        self._stop = False
        self._thread = threading.Thread(target=self._worker, name="backend-push", daemon=True)
        self._thread.start()
        mode = "WS+HTTP" if self.ws_url and self.http_url else ("WS" if self.ws_url else "HTTP")
        _log(f"[推送] 已启动 ({mode})")

    def stop(self) -> None:
        self._stop = True
        with self._ws_lock:
            if self._ws_app is not None:
                try:
                    self._ws_app.close()
                except Exception:
                    pass
                self._ws_app = None

    def push(self, event: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = dict(event)
        if self.anchor_id and not payload.get("anchor_id"):
            payload["anchor_id"] = self.anchor_id
        self._queue.put(payload)

    def _worker(self) -> None:
        while not self._stop:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            self._send(item)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Mqtt-Bridge-Secret"] = self.secret
        return headers

    def _send(self, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False)
        if self.ws_url:
            ok, detail = self._send_ws(body)
            if ok:
                return
            _log(f"[推送] WebSocket 未成功: {detail}")
        if self.http_url:
            try:
                resp = requests.post(
                    self.http_url,
                    data=body.encode("utf-8"),
                    headers=self._headers(),
                    timeout=3,
                )
                if resp.status_code >= 400:
                    _log(f"[推送] HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    try:
                        result = resp.json()
                        if isinstance(result, dict) and result.get("code") not in (0, None):
                            _log(f"[推送] HTTP 业务失败: {result}")
                    except ValueError:
                        pass
            except requests.RequestException as e:
                _log(f"[推送] HTTP 失败: {e}")

    def _send_ws(self, body: str) -> tuple[bool, str]:
        if websocket is None:
            return False, "未安装 websocket-client"
        for attempt in range(2):
            try:
                with self._ws_lock:
                    if self._ws_app is None:
                        self._ws_app = websocket.create_connection(
                            self.ws_url,
                            timeout=5,
                            header=[f"{k}: {v}" for k, v in self._headers().items()],
                        )
                    self._ws_app.send(body)
                    # 礼物事件 bridge 会立即回 accepted，短超时即可
                    self._ws_app.settimeout(2)
                    try:
                        resp = self._ws_app.recv()
                        text = resp.decode("utf-8") if isinstance(resp, bytes) else str(resp)
                    except Exception:
                        # 已发出即视为成功，避免阻塞采集线程
                        return True, "sent"
                try:
                    result = json.loads(text)
                    if isinstance(result, dict) and result.get("code") == 0:
                        return True, text
                    return False, text
                except ValueError:
                    return True, text
            except Exception as e:
                with self._ws_lock:
                    if self._ws_app is not None:
                        try:
                            self._ws_app.close()
                        except Exception:
                            pass
                        self._ws_app = None
                if attempt == 0:
                    time.sleep(0.1)
                    continue
                return False, str(e)
        return False, "unknown"

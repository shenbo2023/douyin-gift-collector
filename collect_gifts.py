#!/usr/bin/env python3
"""
抖音直播间礼物采集（对齐 抖音全量采集.exe）

exe 无需手填 Cookie：config.json 里配置 sid_guard，再访问 live.douyin.com 拿 ttwid。
本脚本同样自动处理，用法:

  python3 collect_gifts.py <room_id>
  python3 collect_gifts.py 7649281884771519274 --no-join

需定期更新 config.json 中的 sid_guard（约 2026-07 过期）。
"""

import gzip
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings
from typing import Any, Callable, Dict, Optional, Tuple

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")


def _bundle_dir() -> str:
    """打包后只读资源目录；开发时为脚本目录。"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _app_dir() -> str:
    """配置文件与礼物 txt 写入目录（exe 同目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


_BUNDLE = _bundle_dir()
_APP = _app_dir()
_ROOT = _APP
sys.path.insert(0, os.path.join(_BUNDLE, "vendor"))
sys.path.insert(0, os.path.join(_BUNDLE, "proto"))

if getattr(sys, "frozen", False):
    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass

import requests
import websocket
from backend_push import BackendPusher
from douyin_live_pb2 import (
    BindingGiftMessage,
    ChatMessage,
    GiftMessage,
    GroupLiveGiftRecipientRecommendMessage,
    LightGiftMessage,
    MemberMessage,
    PushFrame,
    Response,
    RoomMessage,
    SocialMessage,
    UpdateFanTicketMessage,
    User,
)

_VERBOSE = False


def log(msg: str = "") -> None:
    if _VERBOSE:
        print(msg, flush=True)


DEFAULT_CONFIG: Dict[str, Any] = {
    "sid_guard": (
        "54d995b5cdbacd3772a812a1fd3d3391%7C1779089451%7C5184000%7C"
        "Fri%2C+17-Jul-2026+07%3A30%3A51+GMT"
    ),
    "sign_api": "http://203.195.182.244/api/sign?roomid={room_id}",
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "extra_cookie": "",
    "gift_log_dir": "",
    "backend_push": {
        "enabled": False,
        "ws_url": "ws://127.0.0.1:8290",
        "http_url": "",
        "secret": "",
        "anchor_id": 0,
    },
}

CONFIG: Dict[str, Any] = dict(DEFAULT_CONFIG)

SIGN_JS = os.path.join(_BUNDLE, "sign.js")
LIVE_ORIGIN = "https://live.douyin.com"
_ws_app = None

ROOM_ID_PATTERNS = [
    re.compile(r'roomId\\":\\"(\d+)\\"'),
    re.compile(r'"roomId"\s*:\s*"(\d+)"'),
    re.compile(r'room_id_str\\":\\"(\d+)\\"'),
    re.compile(r'"room_id_str"\s*:\s*"(\d+)"'),
]
UID_PATTERN = re.compile(r'user_unique_id\\":\\"(\d+)\\"')

SIGN_PARAM_ORDER = (
    "live_id,aid,version_code,webcast_sdk_version,room_id,sub_room_id,sub_channel_id,"
    "did_rule,user_unique_id,device_platform,device_type,ac,identity"
).split(",")


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读取 config.json；缺失字段用内置默认值。exe 运行时读 exe 同目录配置。"""
    global CONFIG
    config_path = path or os.path.join(_APP, "config.json")
    bundled_example = os.path.join(_BUNDLE, "config.example.json")
    bundled_config = os.path.join(_BUNDLE, "config.json")
    if not os.path.isfile(config_path):
        if os.path.isfile(bundled_example):
            shutil.copy2(bundled_example, config_path)
        elif os.path.isfile(bundled_config):
            shutil.copy2(bundled_config, config_path)
    merged = dict(DEFAULT_CONFIG)
    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            if key in merged and value is not None:
                if key == "backend_push" and isinstance(value, dict) and isinstance(merged[key], dict):
                    nested = dict(merged[key])
                    nested.update(value)
                    merged[key] = nested
                else:
                    merged[key] = value
    else:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "_说明": "sid_guard 过期后需更新；可从 exe 或浏览器 live.douyin.com Cookie 复制",
                    **{k: v for k, v in DEFAULT_CONFIG.items()},
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.write("\n")
        log(f"[配置] 已生成默认配置文件 {config_path}")
    CONFIG = merged
    return CONFIG


def sid_guard_expiry_hint() -> Optional[str]:
    """解析 sid_guard 中的过期时间，返回提醒文案。"""
    raw = urllib.parse.unquote(str(CONFIG.get("sid_guard") or ""))
    parts = raw.split("|")
    if len(parts) < 3 or not parts[1].isdigit():
        return None
    created_at = int(parts[1])
    duration = int(parts[2]) if parts[2].isdigit() else 0
    expire_at = created_at + duration if duration else created_at
    remain = expire_at - int(time.time())
    if remain <= 0:
        return "sid_guard 已过期，请更新 config.json 中的 sid_guard"
    if remain <= 7 * 86400:
        days = max(1, remain // 86400)
        return f"sid_guard 约 {days} 天后过期，请提前更新 config.json"
    return None


def stop_collector() -> None:
    """GUI / exe 停止采集时关闭 WebSocket。"""
    global _ws_app
    if _ws_app is not None:
        try:
            _ws_app.close()
        except Exception:
            pass
        _ws_app = None


def format_gift_line(count: int, gift_name: str) -> str:
    name = gift_name or "礼物"
    if re.search(r"\d+个", name):
        return f"送给主播 {name}"
    return f"送给主播 {count}个{name}"


def format_gift_log_line(
    ts: str,
    user: Optional[User],
    count: int,
    gift_name: str,
    diamond: int = 0,
) -> str:
    """写入 txt 的单行格式: 15:19:53 A一涛 ID:xxx  抖音号:xxx  送给主播 1个加油鸭 抖币:15"""
    name = "未知"
    uid = ""
    display_id = ""
    if user is not None and user.ByteSize() > 0:
        name = user.nickName or user.displayId or user.idStr or "未知"
        uid = user.idStr or (str(user.id) if user.id and user.id != 111111 else "")
        display_id = user.displayId or ""

    line = f"{ts} {name}"
    if uid:
        line += f" ID:{uid}"
    if display_id:
        line += f"  抖音号:{display_id}"
    line += f"  {format_gift_line(count, gift_name)}"
    if diamond:
        line += f" 抖币:{diamond}"
    return line


class GiftLogWriter:
    """实时追加礼物记录到本地 txt"""

    def __init__(self, path: str, room_id: str) -> None:
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"\n# {time.strftime('%Y-%m-%d %H:%M:%S')} 开始记录 room_id={room_id}\n")
                f.flush()

    def write(self, line: str) -> None:
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()


def extract_gift_count(msg: GiftMessage) -> int:
    """礼物数量：优先 totalCount（连击结束），其次 combo/repeat/group。"""
    total = int(msg.totalCount or 0)
    combo = int(msg.comboCount or 0)
    repeat = int(msg.repeatCount or 0)
    group = int(msg.groupCount or 0)
    if total > 0:
        return total
    if combo > 0:
        return combo
    if repeat > 0:
        return repeat
    if group > 0:
        return group
    return 1


def extract_light_gift_count(msg: LightGiftMessage) -> int:
    count = int(msg.count or 0)
    combo = int(msg.comboCount or 0)
    repeat = int(msg.repeatCount or 0)
    group = int(msg.groupCount or 0)
    if count > 0:
        return count
    if combo > 0:
        return combo
    if repeat > 0:
        return repeat
    if group > 0:
        return group
    return 1


class GiftTracker:
    """对齐 exe 内 giftTracker：连击礼物合并，结束时输出「送给主播 N个礼物」"""

    def __init__(self) -> None:
        self.pending_log_line: Optional[str] = None
        self._seen_keys: set = set()

    def _is_duplicate(self, msg: GiftMessage, envelope_msg_id: int = 0) -> bool:
        """按消息 id 去重；groupId 仅在连击结束时再判重。"""
        keys: list = []
        if envelope_msg_id:
            keys.append(("env", envelope_msg_id))
        if msg.HasField("common") and msg.common.msgId:
            keys.append(("cm", msg.common.msgId))
        if not keys:
            return False
        for k in keys:
            if k in self._seen_keys:
                return True
        for k in keys:
            self._seen_keys.add(k)
        if len(self._seen_keys) > 100_000:
            self._seen_keys.clear()
        return False

    def _is_duplicate_gift_event(self, msg: GiftMessage) -> bool:
        """同一笔送礼（groupId）只记录一次，必须在 repeatEnd=1 后调用。"""
        gid = int(msg.groupId or 0)
        if not gid:
            return False
        uid = msg.user.id if msg.HasField("user") else 0
        gift_id = int(msg.giftId or (msg.gift.id if msg.HasField("gift") else 0))
        key = ("grp", gid, uid, gift_id)
        if key in self._seen_keys:
            return True
        self._seen_keys.add(key)
        if len(self._seen_keys) > 100_000:
            self._seen_keys.clear()
        return False

    def feed(self, msg: GiftMessage, ts: str = "", envelope_msg_id: int = 0) -> Optional[str]:
        if self._is_duplicate(msg, envelope_msg_id):
            return None

        repeat_end = int(msg.repeatEnd or 0)
        count = extract_gift_count(msg)
        has_group = bool(msg.groupId)

        # 有 groupId 的连击/长按：等 repeatEnd=1 再记，避免先记 1 个、最终数量被去重
        if has_group and not repeat_end:
            return None
        # 无 groupId 且数量>1 的中间态
        if not repeat_end and count > 1:
            return None

        if has_group and repeat_end and self._is_duplicate_gift_event(msg):
            return None

        user_text = format_user(msg.user) if msg.HasField("user") else "未知用户"
        gift_name = "礼物"
        diamond = 0
        if msg.HasField("gift"):
            gift_name = msg.gift.name or msg.gift.describe or f"礼物ID:{msg.gift.id}"
            diamond = int(msg.gift.diamondCount or 0)

        parts = [user_text, format_gift_line(count, gift_name)]
        if diamond:
            parts.append(f"抖币:{diamond}")
        self.pending_log_line = format_gift_log_line(
            ts,
            msg.user if msg.HasField("user") else None,
            count,
            gift_name,
            diamond,
        )
        return " | ".join(parts)

    def mark_light_seen(self, envelope_msg_id: int = 0, common_msg_id: int = 0) -> bool:
        """LightGiftMessage 去重，返回 True 表示已处理过。"""
        keys: list = []
        if envelope_msg_id:
            keys.append(("env", envelope_msg_id))
        if common_msg_id:
            keys.append(("cm", common_msg_id))
        if not keys:
            return False
        for k in keys:
            if k in self._seen_keys:
                return True
        for k in keys:
            self._seen_keys.add(k)
        return False


def generate_ms_token(length: int = 107) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(alphabet) for _ in range(length))


def apply_exe_cookies(session: requests.Session) -> None:
    """对齐 exe：config 内 sid_guard + 页面访问刷新 ttwid"""
    session.cookies.set("sid_guard", CONFIG["sid_guard"])


def merge_cookie_str(session: requests.Session, extra: Optional[str] = None) -> str:
    """合并 Cookie；优先 session 中的 ttwid，保留 sid_guard"""
    merged: Dict[str, str] = {"sid_guard": CONFIG["sid_guard"]}
    for k, v in session.cookies.get_dict().items():
        merged[k] = v
    for part in (extra or "").split(";"):
        part = part.strip()
        if part and "=" in part:
            k, v = part.split("=", 1)
            merged[k.strip()] = v.strip()
    if "ttwid" not in merged:
        raise RuntimeError("未能获取 ttwid Cookie，请检查网络")
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def prepare_session(
    live_id: str,
    extra_cookie: Optional[str] = None,
) -> Tuple[str, requests.Session, str, Optional[str]]:
    """自动 Cookie（exe 同款 sid_guard + 页面 ttwid）"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": CONFIG["user_agent"],
        "Referer": f"{LIVE_ORIGIN}/",
    })
    apply_exe_cookies(session)
    if extra_cookie:
        for part in extra_cookie.split(";"):
            part = part.strip()
            if part and "=" in part:
                k, v = part.split("=", 1)
                session.cookies.set(k.strip(), v.strip())

    session.get(LIVE_ORIGIN, timeout=15)
    ms_token = generate_ms_token()
    session.cookies.set("msToken", ms_token)
    resp = session.get(f"{LIVE_ORIGIN}/{live_id}", timeout=15)
    resp.raise_for_status()

    cookie_str = merge_cookie_str(session, extra_cookie)
    html = resp.text
    room_id = extract_room_id(html, live_id)
    uid = extract_user_unique_id(html)
    keys = ", ".join(k for k in session.cookies.get_dict().keys())
    log(f"[Cookie] 自动获取（exe 同款 sid_guard + 页面 ttwid）: {keys}")
    if uid:
        log(f"[设备] user_unique_id={uid}")
    if room_id != live_id:
        log(f"[房间] room_id={room_id}（输入: {live_id}）")
    return cookie_str, session, room_id, uid


def extract_room_id(html: str, fallback: str) -> str:
    for pat in ROOM_ID_PATTERNS:
        m = pat.search(html)
        if m and m.group(1).isdigit():
            return m.group(1)
    if fallback.isdigit():
        return fallback
    m = re.search(r"live\.douyin\.com/(\d+)", fallback)
    return m.group(1) if m else fallback


def check_room_status(room_id: str, session: requests.Session) -> Tuple[Optional[int], str]:
    url = (
        "https://webcast.amemv.com/webcast/room/reflow/info/"
        f"?type_id=0&live_id=1&room_id={room_id}&sec_user_id=&app_id=1128"
    )
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    room = resp.json().get("data", {}).get("room", {})
    status = room.get("status")
    title = room.get("title") or "未知"
    if status == 2:
        log(f"[房间] 直播中: {title}")
    elif status == 4:
        log(f"[房间] 已下播: {title}")
        log("[提示] 未开播的直播间连接后会被服务器立即断开，请换正在直播的 room_id")
    elif status is not None:
        log(f"[房间] status={status} title={title}")
    else:
        log("[房间] 无法确认直播状态（room_id 可能无效）")
    return status, title


def extract_user_unique_id(html: str) -> Optional[str]:
    m = UID_PATTERN.search(html)
    return m.group(1) if m else None


def _local_signature(wss_query: str) -> str:
    qs = dict(p.split("=", 1) for p in wss_query.split("&") if "=" in p)
    stub = ",".join(f"{k}={qs.get(k, '')}" for k in SIGN_PARAM_ORDER)
    md5 = hashlib.md5(stub.encode()).hexdigest()
    script = (
        'const fs=require("fs");eval(fs.readFileSync(process.argv[1],"utf8"));'
        "process.stdout.write(get_sign(process.argv[2]));"
    )
    return subprocess.check_output(
        ["node", "-e", script, SIGN_JS, md5],
        cwd=_ROOT,
        text=True,
        timeout=30,
    ).strip()


def fetch_wss_url_sign_api(room_id: str) -> str:
    """与 exe 一致：直接使用 203.195.182.244 签名 API 返回的 wss_url"""
    url = CONFIG["sign_api"].format(room_id=room_id)
    resp = requests.get(url, timeout=15, headers={"User-Agent": CONFIG["user_agent"]})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"签名失败: {data.get('msg', data)}")
    wss_url = data.get("wss_url")
    if not wss_url:
        raise RuntimeError(f"响应中无 wss_url: {data}")
    log(f"[签名] API（exe 同款）room_id={data.get('room_id')} signature={data.get('signature')}")
    return wss_url


def fetch_wss_url_local_sign(room_id: str, user_unique_id: Optional[str]) -> str:
    """DouyinLiveWebFetcher 风格：本地 sign.js + user_unique_id + 完整 WSS 参数"""
    if not user_unique_id:
        raise RuntimeError("本地签名需要 user_unique_id，页面未提取到时可改用 --sign-api")
    if not os.path.isfile(SIGN_JS):
        raise RuntimeError(f"未找到 {SIGN_JS}")

    now = int(time.time() * 1000)
    cursor = f"d-1_u-1_fh-{user_unique_id}_t-{now}_r-1"
    internal = (
        f"internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:{user_unique_id}"
        f"|first_req_ms:{now - 100}|fetch_time:{now}|seq:1|wss_info:0-{now}-0-0|wrds_v:{user_unique_id}"
    )
    base = (
        "wss://webcast100-ws-web-lf.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
        "&version_code=180800&webcast_sdk_version=1.0.14-beta.0&update_version_code=1.0.14-beta.0"
        "&compress=gzip&device_platform=web&cookie_enabled=true&screen_width=1920&screen_height=1080"
        "&browser_language=zh-CN&browser_platform=Win32&browser_name=Mozilla&browser_version=146.0.0.0"
        "&browser_online=true&tz_name=Asia/Shanghai"
        f"&cursor={urllib.parse.quote(cursor)}&internal_ext={urllib.parse.quote(internal)}"
        "&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
        f"&user_unique_id={user_unique_id}&im_path=/webcast/im/fetch/&identity=audience"
        f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={room_id}&heartbeatDuration=0"
    )
    query = urllib.parse.urlparse(base).query
    sig = _local_signature(query)
    log(f"[签名] 本地 sign.js user_unique_id={user_unique_id} signature={sig[:20]}...")
    return f"{base}&signature={sig}"


def fetch_wss_url(
    room_id: str,
    user_unique_id: Optional[str] = None,
    *,
    use_local_sign: bool = False,
) -> str:
    if use_local_sign and user_unique_id and os.path.isfile(SIGN_JS):
        try:
            return fetch_wss_url_local_sign(room_id, user_unique_id)
        except (subprocess.SubprocessError, FileNotFoundError, OSError, RuntimeError) as e:
            log(f"[签名] 本地签名失败({e})，回退签名 API")
    return fetch_wss_url_sign_api(room_id)


def decode_push_frame(raw: bytes) -> Tuple[PushFrame, Optional[Response]]:
    frame = PushFrame()
    frame.ParseFromString(raw)

    payload = frame.payload
    if not payload:
        return frame, None

    encoding = (frame.payloadEncoding or "").lower()
    if encoding == "gzip" or payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)

    response = Response()
    response.ParseFromString(payload)
    return frame, response


def send_ack(ws, frame: PushFrame, response: Response) -> None:
    if not response.needAck or not response.internalExt:
        return
    ack = PushFrame()
    ack.payloadType = "ack"
    ack.logId = frame.logId
    ack.payload = response.internalExt.encode("utf-8")
    ws.send(ack.SerializeToString(), opcode=websocket.ABNF.OPCODE_BINARY)


def start_heartbeat(ws) -> None:
    """Douyin WSS 心跳（PushFrame payloadType=hb）"""
    while True:
        time.sleep(5)
        try:
            hb = PushFrame()
            hb.payloadType = "hb"
            ws.send(hb.SerializeToString(), opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception:
            break


def user_to_dict(user: Optional[User]) -> Dict[str, Any]:
    if user is None or user.ByteSize() <= 0:
        return {}
    uid = user.idStr or (str(user.id) if user.id and user.id != 111111 else "")
    level = user.payGrade.level if user.HasField("payGrade") else 0
    return {
        "uid": uid,
        "nickname": user.nickName or user.displayId or uid or "未知",
        "display_id": user.displayId or "",
        "gender": int(user.gender or 0),
        "level": int(level or 0),
    }


def format_user(user: User) -> str:
    info = user_to_dict(user)
    if not info:
        return "未知"
    gender = {0: "未知", 1: "男", 2: "女"}.get(info.get("gender", 0), str(info.get("gender", 0)))
    parts = [f"昵称:{info.get('nickname', '未知')}"]
    if info.get("uid"):
        parts.append(f"ID:{info['uid']}")
    if info.get("display_id"):
        parts.append(f"抖音号:{info['display_id']}")
    parts.append(f"性别:{gender}")
    if info.get("level"):
        parts.append(f"等级:{info['level']}")
    return " | ".join(parts)


def gift_message_to_payload(msg: GiftMessage, method: str) -> Dict[str, Any]:
    count = extract_gift_count(msg)
    gift_name = "礼物"
    gift_id = int(msg.giftId or 0)
    diamond = 0
    if msg.HasField("gift"):
        gift_name = msg.gift.name or msg.gift.describe or f"礼物ID:{msg.gift.id}"
        gift_id = int(msg.gift.id or gift_id)
        diamond = int(msg.gift.diamondCount or 0)
    user = msg.user if msg.HasField("user") else None
    return {
        "gift": {
            "method": method,
            "gift_id": gift_id,
            "gift_name": gift_name,
            "count": count,
            "diamond": diamond,
            "total_diamond": diamond * count if diamond else 0,
        },
        "user": user_to_dict(user),
    }


def light_gift_message_to_payload(msg: LightGiftMessage, method: str) -> Dict[str, Any]:
    count = extract_light_gift_count(msg)
    gift_name = "礼物"
    gift_id = 0
    diamond = 0
    if msg.HasField("gift"):
        gift_name = msg.gift.name or msg.gift.describe or f"礼物ID:{msg.gift.id}"
        gift_id = int(msg.gift.id or 0)
        diamond = int(msg.gift.diamondCount or 0)
    user = msg.common.user if msg.HasField("common") and msg.common.HasField("user") else None
    return {
        "gift": {
            "method": method,
            "gift_id": gift_id,
            "gift_name": gift_name,
            "count": count,
            "diamond": diamond,
            "total_diamond": diamond * count if diamond else 0,
        },
        "user": user_to_dict(user),
    }


def format_gift(
    payload: bytes,
    tracker: GiftTracker,
    ts: str = "",
    envelope_msg_id: int = 0,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    msg = GiftMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None, None
    text = tracker.feed(msg, ts, envelope_msg_id)
    if not text:
        return None, None
    return text, gift_message_to_payload(msg, "WebcastGiftMessage")


def format_binding_gift(
    payload: bytes,
    tracker: GiftTracker,
    ts: str = "",
    envelope_msg_id: int = 0,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    msg = BindingGiftMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None, None
    if not msg.HasField("msg"):
        return None, None
    text = tracker.feed(msg.msg, ts, envelope_msg_id)
    if not text:
        return None, None
    return text, gift_message_to_payload(msg.msg, "WebcastBindingGiftMessage")


def format_light_gift(
    payload: bytes,
    tracker: GiftTracker,
    ts: str = "",
    envelope_msg_id: int = 0,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    msg = LightGiftMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None, None, None
    if tracker.mark_light_seen(envelope_msg_id, msg.common.msgId if msg.HasField("common") else 0):
        return None, None, None
    count = extract_light_gift_count(msg)
    gift_name = "礼物"
    diamond = 0
    if msg.HasField("gift"):
        gift_name = msg.gift.name or msg.gift.describe or f"礼物ID:{msg.gift.id}"
        diamond = int(msg.gift.diamondCount or 0)
    user = msg.common.user if msg.HasField("common") and msg.common.HasField("user") else None
    user_text = format_user(user) if user else ""
    display_parts = ([user_text] if user_text else []) + [format_gift_line(count, gift_name)]
    if diamond:
        display_parts.append(f"抖币:{diamond}")
    log_line = format_gift_log_line(ts, user, count, gift_name, diamond)
    payload_dict = light_gift_message_to_payload(msg, "WebcastLightGiftMessage")
    return " | ".join(display_parts), log_line, payload_dict


def format_fan_ticket(payload: bytes, last_count: Dict[str, int]) -> Optional[str]:
    msg = UpdateFanTicketMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None
    if not msg.roomFanTicketCount:
        return None
    prev = last_count.get("fan", 0)
    last_count["fan"] = msg.roomFanTicketCount
    if prev and msg.roomFanTicketCount > prev:
        delta = msg.roomFanTicketCount - prev
        return f"直播间贡献值 +{delta}（合计:{msg.roomFanTicketCount}）"
    return None


def print_gift(ts: str, text: str) -> None:
    log(f"[礼物] {ts} {text}")


def format_member(payload: bytes) -> Optional[str]:
    msg = MemberMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None
    if not msg.HasField("user"):
        return None
    text = format_user(msg.user)
    if msg.memberCount:
        text += f" | 在线:{msg.memberCount}"
    return text


def format_chat(payload: bytes) -> Optional[str]:
    msg = ChatMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None
    if not msg.content:
        return None
    user_part = format_user(msg.user) if msg.HasField("user") else "未知"
    return f"{user_part} | 说:{msg.content}"


def format_social(payload: bytes) -> Optional[str]:
    msg = SocialMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None
    if not msg.HasField("user"):
        return None
    user_part = format_user(msg.user)
    if msg.followCount:
        return f"{user_part} | 关注主播(累计:{msg.followCount})"
    return f"{user_part} | 关注主播"


def format_room(payload: bytes) -> Optional[str]:
    """WebcastRoomMessage = 系统/团播卡片，不是送礼事件"""
    msg = RoomMessage()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None
    parts = []
    if msg.bizScene:
        parts.append(f"scene={msg.bizScene}")
    if msg.content:
        text = msg.content.replace("\n", " ").strip()
        if len(text) > 100:
            text = text[:100] + "..."
        parts.append(text)
    elif msg.HasField("common") and msg.common.describe:
        parts.append(msg.common.describe)
    return " | ".join(parts) if parts else None


def run(
    live_id: str,
    debug: bool = False,
    show_join: bool = True,
    gifts_only: bool = False,
    extra_cookie: Optional[str] = None,
    use_local_sign: bool = False,
    gift_log_path: Optional[str] = None,
    save_gifts: bool = True,
    on_started: Optional[Callable[[str, str], None]] = None,
) -> None:
    cookie, session, room_id, uid = prepare_session(live_id, extra_cookie)
    room_status, room_title = check_room_status(room_id, session)
    wss_url = fetch_wss_url(room_id, uid, use_local_sign=use_local_sign)

    backend_pusher = BackendPusher(CONFIG.get("backend_push") or {})
    backend_pusher.start()
    room_info = {
        "room_id": room_id,
        "live_id": live_id,
        "title": room_title,
        "status": room_status or 0,
    }
    backend_pusher.push({
        "event": "room_connected",
        "ts": int(time.time()),
        "room": room_info,
    })

    gift_log: Optional[GiftLogWriter] = None
    log_file = ""
    if save_gifts:
        log_dir = (CONFIG.get("gift_log_dir") or "").strip()
        if gift_log_path:
            log_file = gift_log_path
        elif log_dir:
            log_file = os.path.join(log_dir, f"gifts_{room_id}.txt")
        else:
            log_file = os.path.join(_APP, f"gifts_{room_id}.txt")
        gift_log = GiftLogWriter(log_file, room_id)
        log(f"[记录] 礼物实时写入 {log_file}")

    if on_started:
        on_started(room_id, log_file)

    log(f"[连接] {wss_url}")
    if gift_log:
        log("[运行] 礼物仅写入 txt，Ctrl+C 退出\n")
    else:
        log("[等待] 礼物消息中，Ctrl+C 退出\n")

    ws_headers = {
        "User-Agent": CONFIG["user_agent"],
        "Cookie": cookie,
        "Origin": LIVE_ORIGIN,
        "Referer": f"{LIVE_ORIGIN}/",
    }

    state = {
        "room_offline": room_status == 4,
        "got_room_msg": False,
        "stats": {},
        "gift_tracker": GiftTracker(),
        "fan_ticket": {},
        "gift_events": 0,
        "room_info": room_info,
        "backend_pusher": backend_pusher,
    }

    def record_gift(
        ts: str,
        text: Optional[str],
        method: str,
        debug: bool,
        payload_len: int,
        log_line: Optional[str] = None,
        gift_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if text:
            state["gift_events"] += 1
            line = log_line or state["gift_tracker"].pending_log_line
            if line and gift_log:
                gift_log.write(line)
            elif not gift_log:
                print_gift(ts, text)
            if gift_payload and state.get("backend_pusher"):
                event = {
                    "event": "gift",
                    "ts": int(time.time()),
                    "room": state["room_info"],
                    "log_line": line or text,
                }
                event.update(gift_payload)
                state["backend_pusher"].push(event)
        elif debug:
            log(f"[礼物] {ts} {method} 收到但解析失败 payload={payload_len}B")

    def bump(method: str) -> None:
        state["stats"][method] = state["stats"].get(method, 0) + 1

    def on_open(ws):
        log("[WebSocket] 已连接\n")
        threading.Thread(target=start_heartbeat, args=(ws,), daemon=True).start()

    def on_message(ws, message):
        if isinstance(message, str):
            return
        try:
            frame, response = decode_push_frame(message)
        except Exception as e:
            log(f"[解析错误] {e}")
            return
        if not response:
            return

        send_ack(ws, frame, response)

        for item in response.messagesList:
            method = item.method
            bump(method)
            ts = time.strftime("%H:%M:%S")

            envelope_msg_id = int(item.msgId or 0)

            if method == "WebcastGiftMessage":
                text, gift_payload = format_gift(
                    item.payload,
                    state["gift_tracker"],
                    ts,
                    envelope_msg_id,
                )
                record_gift(
                    ts,
                    text,
                    method,
                    debug,
                    len(item.payload),
                    gift_payload=gift_payload,
                )
            elif method == "WebcastBindingGiftMessage":
                text, gift_payload = format_binding_gift(
                    item.payload,
                    state["gift_tracker"],
                    ts,
                    envelope_msg_id,
                )
                record_gift(
                    ts,
                    text,
                    method,
                    debug,
                    len(item.payload),
                    gift_payload=gift_payload,
                )
            elif method == "WebcastLightGiftMessage":
                text, log_line, gift_payload = format_light_gift(
                    item.payload,
                    state["gift_tracker"],
                    ts,
                    envelope_msg_id,
                )
                record_gift(
                    ts,
                    text,
                    method,
                    debug,
                    len(item.payload),
                    log_line,
                    gift_payload=gift_payload,
                )
            elif method == "WebcastUpdateFanTicketMessage":
                text = format_fan_ticket(item.payload, state["fan_ticket"])
                if text:
                    state["gift_events"] += 1
                    print_gift(ts, text)
            elif method == "WebcastChatMessage" and not gifts_only:
                text = format_chat(item.payload)
                if text:
                    log(f"[弹幕] {ts} {text}")
            elif method == "WebcastMemberMessage" and show_join and not gifts_only:
                text = format_member(item.payload)
                if text:
                    log(f"[进房] {ts} {text}")
                elif debug:
                    log(f"[进房] {ts} 收到但解析失败 payload={len(item.payload)}B")
            elif method == "WebcastSocialMessage" and not gifts_only:
                text = format_social(item.payload)
                if text:
                    log(f"[关注] {ts} {text}")
            elif method == "WebcastRoomMessage":
                # 系统通知/团播道具卡，不是礼物；此前误显示为「直播间状态更新」
                state["got_room_msg"] = True
                if debug:
                    text = format_room(item.payload)
                    if text:
                        log(f"[房间系统] {ts} {text}")
            elif method == "WebcastGroupLiveGiftRecipientRecommendMessage" and debug:
                msg = GroupLiveGiftRecipientRecommendMessage()
                try:
                    msg.ParseFromString(item.payload)
                    uid = msg.recipientUserId or (
                        msg.common.user.id if msg.HasField("common") and msg.common.HasField("user") else 0
                    )
                    log(f"[团播礼物推荐] {ts} recipient={uid}")
                except Exception:
                    log(f"[团播礼物推荐] {ts} payload={len(item.payload)}B")
            elif debug and not gifts_only:
                if "Gift" in method or "FanTicket" in method:
                    log(f"[礼物相关] {ts} {method} payload={len(item.payload)}B")
                else:
                    log(f"[其他] {ts} {method}")

    def on_error(ws, error):
        if state["room_offline"] or state["got_room_msg"]:
            return
        log(f"[错误] {error}")

    def on_close(ws, close_status_code, close_msg):
        if state.get("backend_pusher"):
            state["backend_pusher"].push({
                "event": "room_disconnected",
                "ts": int(time.time()),
                "room": state["room_info"],
                "close_code": close_status_code,
                "close_msg": close_msg or "",
            })
            state["backend_pusher"].stop()
        if state["stats"]:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(state["stats"].items()))
            log(f"\n[统计] {summary}")
            gift_n = state["stats"].get("WebcastGiftMessage", 0)
            gift_n += state["stats"].get("WebcastBindingGiftMessage", 0)
            gift_n += state["stats"].get("WebcastLightGiftMessage", 0)
            if state["gift_events"] == 0:
                gift_n = sum(
                    state["stats"].get(k, 0)
                    for k in (
                        "WebcastGiftMessage",
                        "WebcastBindingGiftMessage",
                        "WebcastLightGiftMessage",
                    )
                )
                if gift_n:
                    log(f"[提示] 收到 {gift_n} 条礼物消息但解析未输出，可加 --debug 查看")
                else:
                    log("[提示] 未收到礼物消息，请确认直播间正在送礼且网络正常")
        if state["room_offline"]:
            log("[断开] 直播间未开播，服务器已关闭连接（请换正在直播的 room_id）")
        elif state["got_room_msg"]:
            log("[断开] 连接已结束")
        else:
            log(f"[断开] code={close_status_code} msg={close_msg}")

    ws = websocket.WebSocketApp(
        wss_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header=ws_headers,
    )
    global _ws_app
    _ws_app = ws
    try:
        ws.run_forever(ping_interval=0)
    finally:
        _ws_app = None
        backend_pusher.stop()


def _read_flag_value(flag: str) -> Optional[str]:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
            return sys.argv[i + 1]
    return None


def main():
    global _VERBOSE
    _VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv
    config_path = _read_flag_value("--config")
    load_config(config_path)
    hint = sid_guard_expiry_hint()
    if hint:
        print(f"[配置] {hint}")
    debug = "--debug" in sys.argv or "-d" in sys.argv
    show_join = "--no-join" not in sys.argv
    gifts_only = "--gifts-only" in sys.argv
    save_gifts = "--no-log" not in sys.argv
    gift_log_path = _read_flag_value("--log-file") or _read_flag_value("-o")
    use_sign_api = "--sign-api" in sys.argv or "--local-sign" not in sys.argv
    use_local_sign = not use_sign_api
    extra_cookie = (
        _read_flag_value("--cookie")
        or _read_flag_value("-c")
        or os.environ.get("DOUYIN_COOKIE")
        or (CONFIG.get("extra_cookie") or "").strip()
        or None
    )
    skip_values = {
        v for v in (
            _read_flag_value("--cookie"),
            _read_flag_value("-c"),
            _read_flag_value("--log-file"),
            _read_flag_value("-o"),
            _read_flag_value("--config"),
        ) if v
    }
    args = [a for a in sys.argv[1:] if not a.startswith("-") and a not in skip_values]

    if not args:
        print("用法: python3 collect_gifts.py <room_id> [选项]")
        print("  --no-join       不显示进房消息")
        print("  --gifts-only    仅礼物")
        print("  --log-file / -o 礼物记录 txt（默认 gifts_<room_id>.txt）")
        print("  --no-log        不写入 txt")
        print("  --local-sign    本地 sign.js 签名（默认与 exe 相同用签名 API）")
        print("  --config        指定配置文件（默认 config.json）")
        print("  --gui           打开图形界面（输入房间 ID）")
        print("  --verbose / -v  显示连接、弹幕等控制台日志")
        print("  --debug         显示系统消息等详情（需配合 --verbose）")
        print("示例: python3 collect_gifts.py 7649281884771519274 --no-join")
        sys.exit(1)

    live_id = args[0].strip()
    if not re.fullmatch(r"\d+", live_id) and "live.douyin.com" not in live_id:
        print("请输入纯数字 room_id 或 live.douyin.com/xxx URL")
        sys.exit(1)

    try:
        run(
            live_id,
            debug=debug,
            show_join=show_join,
            gifts_only=gifts_only,
            extra_cookie=extra_cookie,
            use_local_sign=use_local_sign,
            gift_log_path=gift_log_path,
            save_gifts=save_gifts,
        )
    except KeyboardInterrupt:
        log("\n已退出")
    except requests.RequestException as e:
        print(f"网络错误: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"运行失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    gui_mode = "--gui" in sys.argv or (
        getattr(sys, "frozen", False) and len(sys.argv) == 1
    )
    if gui_mode:
        from app_gui import main_gui

        main_gui()
    else:
        main()

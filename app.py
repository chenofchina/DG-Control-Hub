# -*- coding: utf-8 -*-
"""
DG-Control Hub —— 郊狼多设备分布式综合控制中台 (v1.0.0)

架构拓扑：
  - FastAPI + Uvicorn 内嵌 HTTP / WebSocket 服务（默认 0.0.0.0:18888，端口被占用时自动递增）
  - pywebview (Edge WebView2) 承载 Windows 桌面端窗口；--server-only 可纯服务模式运行
  - /ws/admin              管理端通道（桌面 WebView2 / 手机浏览器，多端同权）
  - /dglab/ws/{target_id}  DG-LAB 官方 App 桥接通道（扫描带参二维码接入）

安全闭环：
  - 全局安全上限 safe_max_limit（默认 50，绝对硬上限 200），服务端统一下发前硬封顶
  - 全局急停 kill_all：取消全部玩法任务 -> clear-1/2 广播 -> 双通道强度归零
  - App 断线（WebSocket 心跳/传输层 ping 超时或主动关闭）自动置 Offline 并将记录强度归零

用法：
  python app.py                # 桌面模式（pywebview 窗口 + 后台服务）
  python app.py --server-only  # 纯服务模式（无 GUI，用于调试 / Linux）
  python app.py --port 18900   # 指定起始端口
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ----------------------------------------------------------------------------
# 全局常量
# ----------------------------------------------------------------------------
APP_NAME = "DG-Control Hub"
APP_VERSION = "1.0.0"
PORT_BASE = 18888          # 默认监听端口（PRD NFR-4：被占用时自动递增）
PORT_MAX_TRY = 12          # 端口探测次数上限
ABSOLUTE_HARD_LIMIT = 200  # 绝对硬上限（PRD CONFIG_HARD_LIMIT，任何情况下不可超越）
DEFAULT_SAFE_LIMIT = 50    # 默认安全上限（PRD SAFE_MAX_INTENSITY，可由管理端调节，最大 200）
BIND_WAIT_TIMEOUT = 20     # App 连接后等待 bind 握手的超时（秒）

STRENGTH_RE = re.compile(r"^strength-([12])[+-]([012])[+-](\d+)$")  # V3: strength-<ch>+<mode>+<val>
TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
PULSE_MSG_MAX = 1900        # V3 协议：单条消息上限 1950 字符，超出被 App 丢弃
PULSE_FRAME_MS = 100        # V3：每个 16 字符波形帧固定 100ms
APP_HEARTBEAT_INTERVAL = 60  # V3：服务端每 60s 向 App 下发心跳
CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/"
                   "download/cloudflared-windows-amd64.exe")
CREATE_NO_WINDOW = 0x08000000  # Windows: 隐藏子进程控制台窗口


def cloudflared_bin() -> str:
    """
    解析 cloudflared 可执行文件路径（Quick Tunnel 无需登录令牌）：
    1) 与主程序同目录的 cloudflared.exe（绿色部署场景，可随 exe 一起分发）
    2) 系统 PATH
    3) %LOCALAPPDATA%\\DG-Control-Hub\\cloudflared.exe（首次启用时自动下载缓存）
    """
    cache = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "DG-Control-Hub", "cloudflared.exe")
    beside = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "cloudflared.exe")
    if os.path.isfile(beside):
        return beside
    found = shutil.which("cloudflared")
    if found:
        return found
    if os.path.isfile(cache):
        return cache
    return cache  # 供 start_tunnel 下载目标使用


# ----------------------------------------------------------------------------
# 用户可编辑配置文件（config.json，与主程序同目录）
# ----------------------------------------------------------------------------
GITHUB_URL = "https://github.com/chenofchina/DG-Control-Hub"
APP_DIR = (os.path.dirname(os.path.abspath(sys.argv[0])) if bool(getattr(sys, "frozen", False))
           else os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG: dict = {
    "_comment": "本文件为 DG-Control Hub 用户配置，可直接用文本编辑器修改；保存后重启程序生效（部分项实时生效）。",
    "port": 18888,
    "safe_max_limit": 50,
    "oobe_completed": False,
    "agree_license": False,
    "tunnel_auto_start": False,
    "v3_init_mode": "both",  # V3 握手引导包模式：targetId / bind / both（both 兼容两类 App 解析器）
    "window": {"width": 1320, "height": 920},
    "waveform_default_duration_ms": 10000,
    "roulette_defaults": {"interval_s": 15, "duration_ms": 2000, "strength": 40, "once": False},
    "ladder_defaults": {"start": 0, "end": 50, "duration_s": 300, "channel": "AB"},
}


def load_config() -> dict:
    """读取用户配置并与默认值深合并（缺失键自动补全）"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                for k, v in user.items():
                    if k == "_comment":
                        continue
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
    except Exception as e:
        print(f"[config] 读取 config.json 失败，使用默认配置: {e}", flush=True)
    return cfg


def save_config(cfg: dict) -> None:
    """将配置持久化到 config.json（带 _comment 说明，方便用户编辑）"""
    try:
        out = {"_comment": DEFAULT_CONFIG["_comment"]}
        out.update(cfg)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[config] 写入 config.json 失败: {e}", flush=True)


LOG_PATH = os.path.join(APP_DIR, "app.log")


def dlog(msg: str) -> None:
    """诊断日志：同时输出到控制台与主程序同目录 app.log（--noconsole 模式下可查文件）"""
    line = time.strftime("[%H:%M:%S] ") + msg
    print(line, flush=True)
    try:
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            os.remove(LOG_PATH)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_frozen() -> bool:
    """是否运行于 PyInstaller 打包环境"""
    return bool(getattr(sys, "frozen", False))


def resource_path(rel: str) -> str:
    """兼容开发态与 PyInstaller --onefile 解压态（sys._MEIPASS）的资源路径"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def _is_lan_ip(ip: str) -> bool:
    """RFC1918 私网地址判断（排除 TUN/虚拟网卡常见的 198.18.0.0/15 基准段）"""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return (a == 10) or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)


def get_lan_ip() -> str:
    """
    局域网出口 IP 识别（PRD FR-02，规避 WSL / Tailscale / 虚拟机 / TUN 网卡）：
    1. UDP Socket 连接公网探针（不产生实际流量），取默认路由出口；
    2. 若出口非 RFC1918 私网（典型如 VPN TUN 的 198.18.x），改用多探针与本机
       网卡地址枚举交叉筛选，优先 192.168.x.x；
    3. 全部失败时回退 127.0.0.1。
    """
    candidates: list[str] = []
    for probe in ("8.8.8.8", "223.5.5.5", "114.114.114.114", "1.1.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if _is_lan_ip(ip):
                return ip
            candidates.append(ip)
        except Exception:
            continue
        finally:
            s.close()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _is_lan_ip(ip):
                return ip
    except Exception:
        pass
    return candidates[0] if candidates else "127.0.0.1"


def find_free_port(start: int = PORT_BASE, tries: int = PORT_MAX_TRY) -> int:
    """端口占用容错：从 start 起逐个探测可用端口（PRD NFR-4）"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError("18888~18899 端口均被占用，请释放端口后重试")


# ----------------------------------------------------------------------------
# 波形库（官方 V3 规范：每帧 16 字符 hex = 4 个时间片段 × (强度字节 + 时间字节)，
# 每帧固定 100ms（时间字节取 0x05=5ms×4 点），pulse-<A|B>:[...] 下发，10 帧/秒）
# ----------------------------------------------------------------------------
def _frame(values) -> str:
    """一个 16 字符波形帧：4 个片段，每片段 = 2 位强度 hex + 2 位时间 hex(0x05)"""
    values = list(values)[:4]
    while len(values) < 4:
        values.append(0)
    return "".join(f"{max(0, min(255, int(v))):02X}05" for v in values)


def _wave_breathing() -> list:
    """呼吸波：正弦平方包络，由弱到强平滑过渡，周期约 3.2s（32 帧）"""
    n = 32
    frames = []
    for f in range(n):
        for j in range(4):
            t = (f * 4 + j) / (n * 4)
            frames.append(_frame([int(220 * (math.sin(math.pi * t) ** 2))] * 1))
    return frames


def _wave_tide() -> list:
    """潮汐波：正弦规律周期振荡，周期约 1.6s（16 帧）"""
    n = 16
    frames = []
    for f in range(n):
        for j in range(4):
            t = (f * 4 + j) / (n * 4)
            frames.append(_frame([int(120 + 100 * math.sin(2 * math.pi * t))] * 1))
    return frames


def _wave_burst() -> list:
    """连击波：100ms 高强度帧 + 100ms 静默帧交替，断续高频刺痛感"""
    hi, lo = _frame([255] * 4), _frame([0] * 4)
    return [hi, lo] * 5


def _wave_chaos() -> list:
    """乱数波：伪随机强度抖动"""
    rnd = random.Random()
    return [_frame([rnd.randint(40, 255)]) for _ in range(16)]


WAVEFORMS = {
    "breathing": {"label": "呼吸波", "desc": "由弱到强平滑过渡", "gen": _wave_breathing},
    "tide":      {"label": "潮汐波", "desc": "正弦规律周期振荡", "gen": _wave_tide},
    "burst":     {"label": "连击波", "desc": "断续高频刺痛感",   "gen": _wave_burst},
    "chaos":     {"label": "乱数波", "desc": "随机占空比抖动",   "gen": _wave_chaos},
}


# ----------------------------------------------------------------------------
# 控制中台核心（Hub Engine）
# ----------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.slots: dict[str, dict] = {}       # targetId -> 卡槽会话
        self.admins: set[WebSocket] = set()    # 管理端连接池（桌面 + 手机多端同权）
        self.cfg = load_config()
        self.safe_max_limit = max(1, min(int(self.cfg.get("safe_max_limit", DEFAULT_SAFE_LIMIT)),
                                         ABSOLUTE_HARD_LIMIT))
        self.server_ip = get_lan_ip()
        self.port: int | None = None
        # 自动化玩法引擎（PRD FR-07）
        self.roulette = {"running": False, "interval_s": 15, "duration_ms": 2000, "strength": 40}
        self.ladder = {"running": False, "start": 0, "end": 50, "duration_s": 300, "channel": "AB"}
        self._roulette_task: asyncio.Task | None = None
        self._ladder_task: asyncio.Task | None = None
        # Cloudflare Quick Tunnel 公网穿透
        self.tunnel = {"status": "off", "url": "", "error": ""}   # off / starting / ready
        self._tunnel_proc: subprocess.Popen | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---------- 基础工具 ----------
    def cap(self) -> int:
        """当前生效的封顶值 = min(安全上限, 绝对硬上限)"""
        return max(1, min(int(self.safe_max_limit), ABSOLUTE_HARD_LIMIT))

    def clamp(self, v) -> int:
        return max(0, min(int(v), self.cap()))

    def online_slots(self) -> list[dict]:
        return [s for s in self.slots.values() if s["status"] == "online"]

    def sync_payload(self) -> dict:
        return {
            "type": "sync",
            "server_ip": self.server_ip,
            "port": self.port,
            "safe_max_limit": self.safe_max_limit,
            "hard_limit": ABSOLUTE_HARD_LIMIT,
            "admin_count": len(self.admins),
            "tunnel": {**self.tunnel},
            "github_url": GITHUB_URL,
            "config_path": CONFIG_PATH,
            "oobe": {
                "completed": bool(self.cfg.get("oobe_completed", False)),
                "agree_license": bool(self.cfg.get("agree_license", False)),
            },
            "auto_tunnel": bool(self.cfg.get("tunnel_auto_start", False)),
            "waveforms": [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in WAVEFORMS.items()],
            "automation": {
                "roulette": {**self.roulette},
                "ladder": {**self.ladder},
            },
            "devices": [
                {
                    "targetId": s["targetId"],
                    "name": s["name"],
                    "status": s["status"],          # waiting / online / offline
                    "clientId": s["clientId"],
                    "channelA": s["channelA"],
                    "channelB": s["channelB"],
                }
                for s in self.slots.values()
            ],
        }

    async def broadcast_admins(self, obj: dict) -> None:
        dead = []
        for ws in list(self.admins):
            try:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.admins.discard(ws)

    async def push_sync(self) -> None:
        await self.broadcast_admins(self.sync_payload())

    async def toast(self, message: str, level: str = "info") -> None:
        await self.broadcast_admins({"type": "toast", "level": level, "message": message})

    # ---------- 卡槽生命周期 ----------
    def create_slot(self, name: str) -> dict:
        target_id = "slot_" + secrets.token_hex(3)
        slot = {
            "targetId": target_id,
            # V3 协议：clientId 由服务端在生成卡槽时预分配，直接编入二维码路径，
            # App 扫码后连接 ws(s)://host/{clientId}，服务端再下发 targetId 完成绑定
            "clientId": str(uuid.uuid4()),
            "appId": "",
            "name": (name or "").strip() or f"设备{len(self.slots) + 1:02d}",
            "status": "waiting",
            "channelA": 0,
            "channelB": 0,
            "ws": None,
            "tasks": set(),
        }
        self.slots[target_id] = slot
        return slot

    async def remove_slot(self, target_id: str) -> None:
        slot = self.slots.pop(target_id, None)
        if not slot:
            return
        await self.cancel_slot_tasks(slot)
        if slot["ws"] is not None:
            old, slot["ws"] = slot["ws"], None
            try:
                await old.close()
            except Exception:
                pass

    async def cancel_slot_tasks(self, slot: dict) -> None:
        for t in list(slot["tasks"]):
            t.cancel()
        slot["tasks"].clear()

    # ---------- App 通道收发 ----------
    async def app_send_raw(self, slot: dict, message: str) -> bool:
        """按 PRD 6.2.2 报文规范向官方 App 下发控制消息"""
        ws = slot.get("ws")
        if ws is None or slot["status"] != "online":
            return False
        payload = {
            "type": "msg",
            "clientId": slot["targetId"],
            "targetId": slot["clientId"],
            "message": message,
        }
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def set_strength(self, slot: dict, channel, mode, val) -> bool:
        """
        强度调控（官方 V3 报文：strength-<ch>+<mode>+<val>，mode: 0=减量 1=增量 2=设定）：
          mode 0 绝对赋值 -> strength-<ch>+2+<v>
          mode 1 增量步进 -> strength-<ch>+1+<d>
          mode 2 减量步进 -> strength-<ch>+0+<d>
        全部数值在下发前经过双层安全架构封顶。
        """
        channel, mode, val = int(channel), int(mode), int(val)
        key = "channelA" if channel == 1 else "channelB"
        cur = slot[key]
        if mode == 0:
            new = self.clamp(val)
            msg = f"strength-{channel}+2+{new}"
        elif mode == 1:
            new = self.clamp(cur + val)
            d = new - cur
            if d <= 0:
                return True  # 已触顶，无需下发
            msg = f"strength-{channel}+1+{d}"
        else:
            new = self.clamp(cur - val)
            d = cur - new
            if d <= 0:
                return True  # 已到底
            msg = f"strength-{channel}+0+{d}"
        ok = await self.app_send_raw(slot, msg)
        if ok:
            slot[key] = new
        return ok

    async def handle_app_message(self, slot: dict, data: dict) -> None:
        """处理官方 App 上行的 msg / feedback 报文（V3 反馈：strength-<ch>+<mode>+<val>，mode 0=减 1=增 2=设定）"""
        msg = str(data.get("message", ""))
        m = STRENGTH_RE.match(msg)
        if m:
            ch, mode, val = int(m.group(1)), int(m.group(2)), int(m.group(3))
            key = "channelA" if ch == 1 else "channelB"
            if mode == 2:
                slot[key] = self.clamp(val)
            elif mode == 1:
                slot[key] = self.clamp(slot[key] + val)
            else:
                slot[key] = self.clamp(slot[key] - val)
            await self.push_sync()

    async def app_gone(self, slot: dict) -> None:
        """App 异常离线保护（PRD NFR-3）：置灰卡槽、强度归零、注销玩法任务"""
        slot["ws"] = None
        slot["status"] = "offline"
        slot["channelA"] = 0
        slot["channelB"] = 0
        await self.cancel_slot_tasks(slot)
        await self.push_sync()

    # ---------- 清空 / 急停 ----------
    async def run_app_session(self, ws: WebSocket, slot: dict | None = None,
                              v3: bool = True, path_id: str = "") -> None:
        """
        官方 App 会话（统一入口，支持三种连接形态）：
          - 根路径 ws://host:port/     （V3 App 常见形态：不带路径，卡槽在 bind 阶段按报文解析）
          - /{clientId|targetId}       （扫码路径形态）
          - /dglab/ws/{targetId}       （PRD 兼容形态，v3=False：等 App 先发 bind）
        官方时序（对齐 PyDGLab-WS 实测实现）：accept 后毫秒级下发绑定引导包
        {"type":"bind","clientId":"<分配给本连接的id>","targetId":"","message":"targetId"}；
        App 回 message="DGLAB"（clientId 携带二维码中的终端 id），服务端回 "200" 字符串完成绑定。
        """
        await ws.accept()
        dlog(f"[+] WebSocket 底层物理握手成功: {path_id or '/'} (v3={v3})")
        bound = False
        bound_slot: dict | None = None
        try:
            app_uuid = str(uuid.uuid4())
            if v3:
                # 1. 官方时序（对齐 PyDGLab-WS 实测实现）：accept 后立即下发绑定引导包，
                #    clientId 字段携带分配给本连接的 ID，message 固定 "targetId"，
                #    targetId 字段缺省（参考其"缩小JSON消息长度"提交，不发送空字段）
                app_uuid = str(uuid.uuid4())
                init = {"type": "bind", "clientId": app_uuid, "targetId": "", "message": "targetId"}
                await ws.send_text(json.dumps(init, ensure_ascii=False))
                dlog(f"[+] 已向 App 主动下发绑定引导包: {init}")
            # 2. 接收 App 的 bind 确认（官方 V3 App 回 message="DGLAB"；兼容别名）
            raw = await asyncio.wait_for(ws.receive_text(), BIND_WAIT_TIMEOUT)
            data = json.loads(raw)
            dlog(f"[+] 收到 App 上报数据: {raw[:200]}")
            if data.get("type") != "bind" or str(data.get("message", "")) not in ("DGLAB", "DG-Lab", "targetId"):
                raise ValueError(f"bind 握手报文不符合 V3 规范: {raw[:120]}")
            # 3. 解析目标卡槽：路径已指定则直接使用；根路径连接按 App 上报的 clientId（终端 id）/
            #    targetId（App 自身 id）匹配
            if slot is None:
                slot = next((s for s in self.slots.values()
                             if s["clientId"] == str(data.get("clientId", ""))
                             or s["targetId"] == str(data.get("targetId", ""))
                             or s.get("appId") == app_uuid), None)
                if not slot:
                    await ws.send_text(json.dumps(
                        {"type": "error", "message": "404",
                         "detail": "未找到匹配的卡槽（二维码已失效？请重新生成后再扫）"},
                        ensure_ascii=False))
                    await ws.close()
                    dlog("[-] 根路径连接未匹配到卡槽，已回 404 并关闭")
                    return
            # 同一卡槽重复接入时踢掉旧连接
            if slot["ws"] is not None:
                old, slot["ws"] = slot["ws"], None
                try:
                    await old.close()
                except Exception:
                    pass
            slot["ws"] = ws
            slot["status"] = "online"
            slot["appId"] = app_uuid
            bound = True
            bound_slot = slot
            # 4. 回复确认包（message == "200" 字符串；字段原样回显 App 上报内容，缺失则回落）
            ack = {
                "type": "bind",
                "clientId": str(data.get("clientId") or slot["clientId"]),
                "targetId": str(data.get("targetId") or app_uuid),
                "message": "200",
            }
            await ws.send_text(json.dumps(ack, ensure_ascii=False))
            dlog(f"[+] 握手成功，已回传 200 确认: {ack}")
            bound = True
            await self.push_sync()
            await self.toast(f"{slot['name']} 已上线", "success")

            # 4. 会话保持：官方 V3 由服务端每 60s 下发心跳；同时传输层 ping 兜底，
            #    连接死亡会在 ping 超时后抛出异常 -> 离线保护。收到的 heartbeat 按规范忽略。
            last_beat = time.monotonic()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(),
                                                 max(1, APP_HEARTBEAT_INTERVAL - (time.monotonic() - last_beat)))
                except asyncio.TimeoutError:
                    # 官方心跳：clientId=接收方(App)，targetId=绑定方(终端)，message="200"
                    beat = {"type": "heartbeat", "clientId": app_uuid,
                            "targetId": slot["clientId"], "message": "200"}
                    await ws.send_text(json.dumps(beat, ensure_ascii=False))
                    last_beat = time.monotonic()
                    continue
                last_beat = time.monotonic()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = data.get("type")
                if t in ("msg", "feedback"):
                    await self.handle_app_message(slot, data)
                elif t == "break":
                    break
                # heartbeat 等其他类型：按官方规范忽略，保持连接
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, ConnectionError):
            pass
        except Exception as e:
            dlog(f"[!] App 会话处理异常: {e}")
        finally:
            if bound_slot is not None and bound_slot.get("ws") is ws:
                await self.app_gone(bound_slot)
                dlog(f"[-] 设备断开连接: {bound_slot['targetId']}")
                if bound:
                    await self.toast(f"{bound_slot['name']} 已离线，强度已归零", "error")

    async def clear_device(self, slot: dict, note: str = "") -> None:
        """设备级清空复位：清波形缓冲 + 双通道归零"""
        await self.cancel_slot_tasks(slot)
        await self.app_send_raw(slot, "clear-1")
        await self.app_send_raw(slot, "clear-2")
        await self.app_send_raw(slot, "strength-1+2+0")
        await self.app_send_raw(slot, "strength-2+2+0")
        slot["channelA"] = 0
        slot["channelB"] = 0
        if note:
            await self.toast(note, "info")

    async def kill_all(self) -> None:
        """
        全局紧急制动（PRD 4.2.3 Kill Switch）：
        1. 清空所有正在轮询中的自动化玩法任务
        2. 广播 clear-1 / clear-2
        3. 紧随广播双通道强度归零
        """
        await self.stop_roulette()
        await self.stop_ladder()
        for s in list(self.slots.values()):
            await self.cancel_slot_tasks(s)
            await self.app_send_raw(s, "clear-1")
            await self.app_send_raw(s, "clear-2")
            await self.app_send_raw(s, "strength-1+2+0")
            await self.app_send_raw(s, "strength-2+2+0")
            s["channelA"] = 0
            s["channelB"] = 0
        await self.toast("已触发全局急停：玩法任务已清空，全部通道归零", "error")

    # ---------- 自动化玩法引擎 ----------
    async def start_roulette(self, params: dict) -> None:
        await self.stop_roulette()
        r = self.roulette
        r["interval_s"] = max(1, int(params.get("interval_s", 15)))
        r["duration_ms"] = max(100, int(params.get("duration_ms", 2000)))
        r["strength"] = self.clamp(params.get("strength", 40))
        r["once"] = bool(params.get("once", False))
        r["running"] = True
        self._roulette_task = asyncio.create_task(self._roulette_loop())

    async def stop_roulette(self) -> None:
        self.roulette["running"] = False
        if self._roulette_task is not None:
            self._roulette_task.cancel()
            self._roulette_task = None

    async def _roulette_loop(self) -> None:
        """轮盘随机模式：随机挑选一台在线设备执行限时脉冲，其余设备静默"""
        try:
            while self.roulette["running"]:
                await asyncio.sleep(self.roulette["interval_s"])
                if not self.roulette["running"]:
                    break
                cands = self.online_slots()
                if not cands:
                    continue
                pick = random.choice(cands)
                r = self.roulette
                prev = (pick["channelA"], pick["channelB"])
                await self.toast(f"轮盘命中：{pick['name']}（{r['strength']} / {r['duration_ms']}ms）", "error")
                await self.set_strength(pick, 1, 0, r["strength"])
                await self.set_strength(pick, 2, 0, r["strength"])
                await self.push_sync()
                await asyncio.sleep(r["duration_ms"] / 1000)
                if pick["status"] == "online":
                    await self.set_strength(pick, 1, 0, prev[0])
                    await self.set_strength(pick, 2, 0, prev[1])
                await self.push_sync()
                if self.roulette.get("once"):
                    self.roulette["running"] = False
                    await self.toast("轮盘单次模式：命中一次后自动停止", "success")
                    await self.push_sync()
                    break
        except asyncio.CancelledError:
            pass

    async def start_ladder(self, params: dict) -> None:
        await self.stop_ladder()
        p = self.ladder
        start = self.clamp(params.get("start", 0))
        end = self.clamp(params.get("end", 50))
        if end < start:
            end = start
        channel = params.get("channel", "AB")
        if channel not in ("A", "B", "AB"):
            channel = "AB"
        p.update(start=start, end=end,
                 duration_s=max(4, int(params.get("duration_s", 300))),
                 channel=channel, running=True)
        self._ladder_task = asyncio.create_task(self._ladder_loop())

    async def stop_ladder(self) -> None:
        self.ladder["running"] = False
        if self._ladder_task is not None:
            self._ladder_task.cancel()
            self._ladder_task = None

    async def _ladder_loop(self) -> None:
        """阶梯渐增爬升：按时间片计算步进斜率，定时下发增量指令"""
        p = self.ladder
        interval = 2.0
        channels = (1, 2) if p["channel"] == "AB" else ((1,) if p["channel"] == "A" else (2,))
        try:
            cur = p["start"]
            for s in self.online_slots():
                for ch in channels:
                    await self.set_strength(s, ch, 0, cur)
            await self.push_sync()
            n = max(1, int(p["duration_s"] / interval))
            total = p["end"] - p["start"]
            step = max(1, math.ceil(abs(total) / n)) if total > 0 else 0
            while p["running"] and cur < p["end"]:
                await asyncio.sleep(interval)
                if not p["running"]:
                    break
                nxt = min(cur + step, p["end"])
                d = nxt - cur
                for s in self.online_slots():
                    for ch in channels:
                        await self.set_strength(s, ch, 1, d)  # PRD：定时发送 strength-{ch}-1-{step}
                cur = nxt
                await self.push_sync()
        except asyncio.CancelledError:
            pass

    async def play_waveform(self, target_id: str | None, wave: str,
                            duration_ms: int, channel: str = "AB") -> None:
        """波形下发：官方 V3 帧格式（16 字符/帧，每帧 100ms），分片规避 1950 字符上限"""
        if channel not in ("A", "B", "AB"):
            channel = "AB"
        channels = ["A", "B"] if channel == "AB" else [channel]
        if target_id:
            slot = self.slots.get(target_id)
            targets = [slot] if slot and slot["status"] == "online" else []
        else:
            targets = self.online_slots()
        if not targets:
            await self.toast("没有在线设备，波形未下发", "error")
            return
        w = WAVEFORMS.get(wave)
        if not w:
            return
        frames = w["gen"]()
        # 按 1950 字符上限分片（V3 规范：超长消息会被 App 丢弃）
        chunks, cur, ln = [], [], 2
        for fr in frames:
            add = len(fr) + (3 if cur else 1)
            if ln + add > PULSE_MSG_MAX and cur:
                chunks.append(cur)
                cur, ln = [], 2
            cur.append(fr)
            ln += len(fr) + 3
        if cur:
            chunks.append(cur)
        chunk_msgs = {ch: [f"pulse-{ch}:{json.dumps(arr, separators=(',', ':'))}" for arr in chunks]
                      for ch in channels}
        for s in targets:
            for ch in channels:
                for m in chunk_msgs[ch]:
                    await self.app_send_raw(s, m)
            t = asyncio.create_task(self._waveform_hold(s, chunk_msgs, duration_ms))
            s["tasks"].add(t)
            t.add_done_callback(lambda tt, s=s: s["tasks"].discard(tt))
        await self.toast(f"波形「{w['label']}」已下发 {len(targets)} 台设备，持续 {duration_ms / 1000:g}s")

    async def _waveform_hold(self, slot: dict, chunk_msgs: dict, duration_ms: int) -> None:
        """波形保持任务：按波形周期持续补给分片数据（流式缓冲模型），结束后清空通道"""
        try:
            frames_per_cycle = sum(len(arr) for arr in next(iter(chunk_msgs.values()), []))
            cycle = max(0.5, frames_per_cycle * PULSE_FRAME_MS / 1000)
            remaining = max(0.2, duration_ms / 1000)
            while remaining > 0:
                await asyncio.sleep(min(cycle, remaining))
                remaining -= cycle
                if slot["status"] != "online":
                    return
                for ch, msgs in chunk_msgs.items():  # 持续补给波形数据
                    for m in msgs:
                        await self.app_send_raw(slot, m)
        except asyncio.CancelledError:
            pass
        finally:
            if slot["status"] == "online":
                for ch in chunk_msgs:
                    await self.app_send_raw(slot, f"clear-{ch}")

    # ---------- Cloudflare Quick Tunnel ----------
    def _ensure_cloudflared(self) -> str:
        """确保 cloudflared 可用；缺失时自动从 Cloudflare 官方 Release 下载缓存"""
        path = cloudflared_bin()
        if os.path.isfile(path) or shutil.which("cloudflared"):
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        req = urllib.request.Request(CLOUDFLARED_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, path)
        return path

    def _tunnel_reader(self, proc: subprocess.Popen) -> None:
        """后台线程：解析 cloudflared stderr 中的 trycloudflare 随机域名"""
        got = ""
        err = ""
        try:
            for line in iter(proc.stderr.readline, b""):
                text = line.decode("utf-8", "ignore")
                m = TUNNEL_URL_RE.search(text)
                if m:
                    got = m.group(0)
                    break
                low = text.lower()
                if ("failed" in low or "error" in low) and len(err) < 300:
                    err = text.strip()[:300]
        except Exception:
            pass
        if got and self._tunnel_proc is proc:
            self.tunnel.update(status="ready", url=got, error="")
            if self.loop:
                asyncio.run_coroutine_threadsafe(self.push_sync(), self.loop)
                asyncio.run_coroutine_threadsafe(
                    self.toast(f"公网隧道已就绪：{got}", "success"), self.loop)
        elif self._tunnel_proc is proc:
            self.tunnel.update(status="off", url="", error=err or "cloudflared 已退出")
            self._tunnel_proc = None
            if self.loop:
                asyncio.run_coroutine_threadsafe(self.push_sync(), self.loop)
                asyncio.run_coroutine_threadsafe(
                    self.toast(f"公网隧道已退出：{err or '未获取到隧道域名'}", "error"), self.loop)

    async def start_tunnel(self) -> None:
        if self._tunnel_proc is not None:
            await self.toast("公网隧道已在运行中", "info")
            return
        self.tunnel.update(status="starting", url="", error="")
        await self.push_sync()
        try:
            path = await asyncio.to_thread(self._ensure_cloudflared)
        except Exception as e:
            self.tunnel.update(status="off", error=f"下载 cloudflared 失败: {e}")
            await self.push_sync()
            await self.toast(f"cloudflared 获取失败，请检查外网连接或手动放置 cloudflared.exe：{e}", "error")
            return
        try:
            proc = subprocess.Popen(
                [path, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.tunnel.update(status="off", error=str(e))
            await self.push_sync()
            await self.toast(f"cloudflared 启动失败：{e}", "error")
            return
        self._tunnel_proc = proc
        threading.Thread(target=self._tunnel_reader, args=(proc,), daemon=True).start()
        await self.toast("正在建立 Cloudflare Quick Tunnel（首次使用将自动下载 cloudflared，约 20MB）…")

    async def stop_tunnel(self) -> None:
        proc, self._tunnel_proc = self._tunnel_proc, None
        self.tunnel.update(status="off", url="", error="")
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        await self.toast("公网隧道已关闭", "info")

    def stop_tunnel_sync(self) -> None:
        """进程退出时的同步清理（pywebview 窗口关闭路径）"""
        proc = self._tunnel_proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    # ---------- 管理端指令分发（PRD 6.1.2） ----------
    async def handle_admin(self, data: dict) -> None:
        action = data.get("action")
        if action == "create_slot":
            self.create_slot(str(data.get("name", "")))
            await self.push_sync()

        elif action == "remove_slot":
            await self.remove_slot(str(data.get("targetId", "")))
            await self.push_sync()

        elif action == "strength":
            slot = self.slots.get(str(data.get("targetId", "")))
            if not slot or slot["status"] != "online":
                await self.toast("目标设备不在线，指令未下发", "error")
                return
            await self.set_strength(slot, data.get("channel", 1),
                                    data.get("mode", 0), data.get("val", 0))
            await self.push_sync()

        elif action == "clear":
            slot = self.slots.get(str(data.get("targetId", "")))
            if slot and slot["status"] == "online":
                await self.clear_device(slot, f"{slot['name']} 已清空复位")
                await self.push_sync()

        elif action == "rename_slot":
            slot = self.slots.get(str(data.get("targetId", "")))
            name = str(data.get("name", "")).strip()[:30]
            if slot and name:
                slot["name"] = name
                await self.push_sync()
                await self.toast(f"设备已重命名为「{name}」", "success")

        elif action == "clear_all":
            targets = self.online_slots()
            for s in targets:
                await self.cancel_slot_tasks(s)
                await self.app_send_raw(s, "clear-1")
                await self.app_send_raw(s, "clear-2")
            await self.toast(f"已向 {len(targets)} 台在线设备下发波形清空（强度保持不变）", "success")

        elif action == "tunnel":
            if data.get("op") == "start":
                await self.start_tunnel()
            else:
                await self.stop_tunnel()
            await self.push_sync()

        elif action == "oobe_complete":
            self.cfg["oobe_completed"] = True
            self.cfg["agree_license"] = True
            save_config(self.cfg)
            await self.toast("初始化向导已完成，配置已写入 config.json", "success")
            await self.push_sync()

        elif action == "save_config":
            """用户可编辑配置的 GUI 入口：深合并补丁并持久化（port 修改需重启生效）"""
            patch = data.get("patch")
            if not isinstance(patch, dict):
                await self.toast("配置补丁格式错误", "error")
                return
            for k, v in patch.items():
                if k == "_comment":
                    continue
                if isinstance(v, dict) and isinstance(self.cfg.get(k), dict):
                    self.cfg[k].update(v)
                elif k in DEFAULT_CONFIG:
                    self.cfg[k] = v
            if "safe_max_limit" in patch:
                try:
                    self.safe_max_limit = max(1, min(int(patch["safe_max_limit"]), ABSOLUTE_HARD_LIMIT))
                except (TypeError, ValueError):
                    pass
            save_config(self.cfg)
            await self.toast("配置已保存至 config.json", "success")
            await self.push_sync()

        elif action == "kill_all":
            await self.kill_all()
            await self.push_sync()

        elif action == "set_config":
            try:
                v = int(data.get("safe_max_limit", self.safe_max_limit))
            except (TypeError, ValueError):
                return
            self.safe_max_limit = max(1, min(v, ABSOLUTE_HARD_LIMIT))
            self.cfg["safe_max_limit"] = self.safe_max_limit
            save_config(self.cfg)
            # 收紧上限时，将在线设备超出部分立即压回封顶值
            for s in self.online_slots():
                for key, ch in (("channelA", 1), ("channelB", 2)):
                    if s[key] > self.cap():
                        await self.set_strength(s, ch, 0, self.cap())
            await self.toast(f"安全上限已调整为 {self.safe_max_limit}", "success")
            await self.push_sync()

        elif action == "roulette":
            if data.get("op") == "start":
                await self.start_roulette(data)
            else:
                await self.stop_roulette()
            await self.push_sync()

        elif action == "ladder":
            if data.get("op") == "start":
                await self.start_ladder(data)
            else:
                await self.stop_ladder()
            await self.push_sync()

        elif action == "waveform":
            try:
                dur = int(data.get("duration_ms", 10000))
            except (TypeError, ValueError):
                dur = 10000
            await self.play_waveform(data.get("targetId"), str(data.get("wave", "")),
                                     dur, str(data.get("channel", "AB")))


# ----------------------------------------------------------------------------
# FastAPI 应用与路由
# ----------------------------------------------------------------------------
hub = Hub()
WEB_DIR = resource_path("web")
api = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)


@api.on_event("startup")
async def _capture_loop():
    """捕获 uvicorn 事件循环，供隧道读取线程回推状态；并按配置自启隧道"""
    hub.loop = asyncio.get_running_loop()
    if hub.cfg.get("tunnel_auto_start"):
        await hub.toast("检测到 tunnel_auto_start=true，正在自动建立公网隧道…")
        asyncio.create_task(hub.start_tunnel())


@api.get("/api/state")
async def api_state():
    """状态全量快照（调试 / 健康检查用）"""
    return JSONResponse(hub.sync_payload())


@api.get("/api/manifest")
async def api_manifest():
    """PWA Web App Manifest（PRD FR-01：手机端添加到主屏幕全屏运行）"""
    return JSONResponse(
        {
            "name": APP_NAME,
            "short_name": "DG Hub",
            "start_url": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#121316",
            "theme_color": "#6750A4",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        },
        media_type="application/manifest+json",
    )


@api.get("/api/bing")
def api_bing():
    """必应每日壁纸代理（PRD FR-06：后端转发规避浏览器 CORS 限制）"""
    try:
        req = urllib.request.Request(
            "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        img = data["images"][0]
        return {"url": "https://www.bing.com" + img["url"],
                "copyright": img.get("copyright", ""),
                "date": img.get("enddate", "")}
    except Exception as e:
        return JSONResponse({"error": f"必应壁纸获取失败: {e}"}, status_code=502)


@api.websocket("/ws/admin")
async def ws_admin(ws: WebSocket):
    """管理端通道：桌面 WebView2 / 手机浏览器共用，多端全量状态同步"""
    await ws.accept()
    hub.admins.add(ws)
    try:
        await ws.send_text(json.dumps(hub.sync_payload(), ensure_ascii=False))
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                await hub.handle_admin(data)
            except Exception as e:  # 单条指令异常不中断会话
                await hub.toast(f"指令执行异常: {e}", "error")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        hub.admins.discard(ws)


# 根路径连接（V3 App 常见形态 ws://host:port/ 不带路径）也必须完成 WS 升级，
# 不能落到静态文件 200 OK，否则 OkHttp 客户端抛 ProtocolException「服务器地址错误」
@api.websocket("/")
@api.websocket("/{target_id}")
@api.websocket("/{target_id}/")
@api.websocket("/dglab/ws/{target_id}")
async def ws_dglab(ws: WebSocket, target_id: str = ""):
    """DG-LAB 官方 App 桥接通道（多路径注册，共享同一会话逻辑）"""
    slot = None
    if target_id:
        slot = hub.slots.get(target_id) or next(
            (s for s in hub.slots.values() if s["clientId"] == target_id), None)
        if not slot:
            await _reject_ws(ws, "targetId / clientId 不存在")
            return
    await hub.run_app_session(ws, slot,
                              v3=(slot is None or slot["targetId"] != target_id),
                              path_id=target_id or "/")


async def _reject_ws(ws: WebSocket, detail: str) -> None:
    """接受连接后回发 404 错误报文并关闭（官方 App 会提示二维码无效）"""
    await ws.accept()
    try:
        await ws.send_text(json.dumps(
            {"type": "error", "message": "404", "detail": detail}, ensure_ascii=False))
        await ws.close()
    except Exception:
        pass


# 静态前端（挂载在最后，避免覆盖 API 路由）
api.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


# ----------------------------------------------------------------------------
# 进程编排：后台服务线程 + pywebview 桌面窗口
# ----------------------------------------------------------------------------
def start_uvicorn(port: int, holder: list) -> None:
    config = uvicorn.Config(api, host="0.0.0.0", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    # 服务运行于守护线程：屏蔽主线程专属的信号处理（旧版 uvicorn 兼容）
    try:
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    except Exception:
        pass
    holder.append(server)
    server.run()


def main() -> None:
    # PyInstaller --noconsole 模式下 std 流可能为 None，避免 uvicorn 日志崩溃
    if getattr(sys, "stdout", None) is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if getattr(sys, "stderr", None) is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

    server_only = "--server-only" in sys.argv or "--headless" in sys.argv
    start_port = int(hub.cfg.get("port", PORT_BASE) or PORT_BASE)
    if "--port" in sys.argv:
        try:
            start_port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass

    port = find_free_port(start_port)
    hub.port = port
    hub.server_ip = get_lan_ip()

    holder: list[uvicorn.Server] = []
    t = threading.Thread(target=start_uvicorn, args=(port, holder), daemon=True)
    t.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        if holder and holder[0].started:
            break
        time.sleep(0.1)

    banner = (
        f"\n  ╔════════════════════════════════════════════════╗\n"
        f"  ║   {APP_NAME} v{APP_VERSION}                          ║\n"
        f"  ╠════════════════════════════════════════════════╣\n"
        f"  ║  局域网地址 : http://{hub.server_ip}:{port:<15}║\n"
        f"  ║  本机地址   : http://127.0.0.1:{port:<18}║\n"
        f"  ╚════════════════════════════════════════════════╝\n"
    )
    print(banner, flush=True)

    if server_only:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            hub.stop_tunnel_sync()
            if holder:
                holder[0].should_exit = True
            return

    # 桌面模式：Windows 原生 WebView2 容器
    import webview  # 延迟导入：纯服务模式无需 GUI 依赖

    win_cfg = hub.cfg.get("window", {}) or {}
    window = webview.create_window(
        f"{APP_NAME} v{APP_VERSION}",
        f"http://127.0.0.1:{port}/",
        width=int(win_cfg.get("width", 1320)), height=int(win_cfg.get("height", 920)),
        min_size=(960, 640),
        background_color="#121316",
    )
    webview.start()  # 阻塞至窗口关闭

    # 窗口销毁：连带终止后台服务线程、网络套接字与 cloudflared 子进程
    hub.stop_tunnel_sync()
    if holder:
        holder[0].should_exit = True
    time.sleep(0.6)
    os._exit(0)


if __name__ == "__main__":
    main()

"""Vidu 组件版会话：建会话 → conn_init → 推 PCM → 打断 / 挂断。

这里只放「跟 Vidu 云端打交道」的部分，不碰界面：
画面由阿里云 ARTC 原生 SDK 直接给裸帧（见 `avatar_rtc.py`），
所以这个模块不需要浏览器、不需要页面、也不需要单独的进程。
"""

from __future__ import annotations

import base64
import json
import threading
import pathlib
import tempfile
import time

import numpy as np
import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

HOST = "api.vidu.cn"
HTTP = f"https://{HOST}"
WS = f"wss://{HOST}"
SAMPLE_RATE = 24000
FRAME_SECONDS = 0.02          # 20ms 一帧 PCM
# 数字人模型不在这里定：`data/settings/avatar.json` 的 model 才是唯一来源
# （s2 输出 888×1184、嘴部和牙齿比 s1 的 624×832 清楚；s1 官方标"稳定性好"）
SILENCE_SECONDS = 3           # 先推静音把 Vidu 的渲染/推流叫起来
CONN_ID = "noesia-avatar"     # Vidu 侧认这条连接的名字
FRAME_BYTES = int(SAMPLE_RATE * 2 * FRAME_SECONDS)  # 20ms = 960 字节


def read_token_or_empty() -> str:
    """Vidu 的凭证（存在 `data/NoesiaHome/.env` 的 VIDU_TOKEN，界面上能填）。"""
    from app import avatar_prefs

    return avatar_prefs.read_token("vidu")


def read_token() -> str:
    """真要建会话时用它：没有就直接报清楚缺什么。"""
    value = read_token_or_empty()
    if not value:
        raise RuntimeError(
            "还没填 Vidu Token：到「设置 → 语音互动」里填，或写进 data/NoesiaHome/.env 的 VIDU_TOKEN"
        )
    return value


def write_token(value: str) -> None:
    from app import avatar_prefs

    avatar_prefs.write_token(value, "vidu")


def verify_token(token: str, timeout: float = 10.0) -> str:
    """真去问一次云端这把 Token 能不能用：只列一条历史会话，不建会话、不花积分。

    401/403 就是 Token 不对（复制少了、被撤了、或者是别的产品的 Key）；
    其它错误码原样抛出来，别把"网络抖了"说成"Key 错了"。
    """
    try:
        resp = requests.get(
            f"{HTTP}/live/v1/lives",
            params={"page_size": 1},
            headers={"Authorization": f"Token {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"连不上 Vidu（{exc.__class__.__name__}）：检查一下网络，Token 先没动") from exc
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Vidu 不认这把 Token（{resp.status_code}）：多半是复制时漏了几位，或者它已经被撤掉")
    if resp.status_code != 200:
        raise RuntimeError(f"Vidu 返回 {resp.status_code}：{resp.text[:120]}")
    return "Token 可用（查到了你的会话列表；这是一次普通查询，不花积分）"


def build_green_input(image_path: pathlib.Path, out_path: pathlib.Path) -> pathlib.Path:
    """带 alpha 的形象图先合成到纯绿底；已经是绿幕图的直接压成 JPG 上传。"""
    from PIL import Image

    src = Image.open(image_path)
    if src.mode in ("RGBA", "LA", "P"):
        rgba = src.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (0, 255, 0))
        canvas.paste(rgba, (0, 0), rgba)
        canvas.save(out_path, format="JPEG", quality=95)
    else:
        src.convert("RGB").save(out_path, format="JPEG", quality=95)
    return out_path


_ERROR_SOURCES = {
    "baseline_reset": "复位画面（type 15）",
    "switch_avatar": "切换形象（type 16）",
}

_ERROR_CODES = {
    "not_supported": "这个直播形态不支持这项能力",
    "live_not_active": "会话已经不在运行了",
    "SWITCH_AVATAR_PARAM_INVALID": "转场视频参数不合法",
    "SWITCH_AVATAR_LIVE_NOT_ACTIVE": "会话已经不在运行了",
    "SWITCH_AVATAR_MEDIA_TRANSFER_FAILED": "转场视频或新形象图片处理失败",
    "SWITCH_AVATAR_RENDER_NOT_CONNECTED": "渲染服务没就绪，稍后可以重试",
    "SWITCH_AVATAR_FAILED": "其它失败",
}


def describe_error(source: str, code: str, detail: str = "") -> str:
    """把 type=103 的错误码翻成人话；认不出的码原样带着，绝不吞掉。"""
    what = _ERROR_SOURCES.get(source, source or "服务端信令")
    why = _ERROR_CODES.get(code, code or "未知错误")
    text = f"{what}失败：{why}"
    if detail and detail not in text:
        text += f"（{detail[:120]}）"
    return text


class ViduSession:
    """组件版会话：建会话 → WebSocket conn_init → 推 PCM → 挂断。"""

    def __init__(self, token: str, image_path: pathlib.Path, model: str):
        self.token = token
        self.image_path = image_path
        self.model = model
        self.live_id = ""
        self.rtc: dict = {}
        self._ws = None
        self._secret = ""
        self._seq = 100
        self._send_lock = threading.Lock()   # 定速推帧线程和主线程都会发信令，串着来
        self._closed = ""            # 非空表示连接已经没了（服务端断开或本地关掉）
        self.on_event = None         # 服务端消息/断线交给外面记日志，别静默
        self.on_error = None         # type=103 错误回执：(source, code, 人话)，交给外面提示人

    def create(self) -> dict:
        green = build_green_input(
            self.image_path, pathlib.Path(tempfile.gettempdir()) / "noesia_vidu_input.jpg"
        )
        payload = {
            "model": self.model,
            "image_uri": "data:image/jpeg;base64," + base64.b64encode(green.read_bytes()).decode(),
            "managed_rtc": {"enable_recording": False},
        }
        resp = requests.post(
            f"{HTTP}/live/s_avatar/component",
            headers={"Authorization": f"Token {self.token}"},
            json=payload,
            timeout=60,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Vidu 不认这把 Token（{resp.status_code}）：到「设置 → 语音模型 → 语音互动」里重新填一次"
            )
        if resp.status_code != 200:
            raise RuntimeError(f"建会话失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self.live_id = str(data["live"]["id"])
        self.rtc = data["managed_rtc"]
        self._secret = data["client_secret"]
        return data

    @property
    def closed(self) -> str:
        """空串=连接还在；否则是断开原因。"""
        return self._closed

    def _recv_loop(self) -> None:
        """必须有：不回包的话服务端会把连接当死的踢掉（实测 7~15 秒就断）。

        顺带把服务端下发的消息交给 `on_event`——出问题才有东西可查，不能靠猜。
        """
        ws = self._ws
        while ws is not None:
            try:
                raw = ws.recv()
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    self._closed = f"连接断开：{exc}"
                    if self.on_event:
                        self.on_event(self._closed)
                return
            if not raw:
                continue
            if isinstance(raw, str):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._handle_message(msg)

    def _handle_message(self, msg: dict) -> None:
        """服务端消息：错误回执翻成人话交出去，强制挂断记下来，其余照旧记日志。"""
        kind = msg.get("type")
        if kind == 103:
            error = (msg.get("payload") or {}).get("error") or {}
            source = str(error.get("source") or "")
            detail = str(error.get("message") or "")
            code = str(error.get("code") or "")
            text = describe_error(source, code, detail)
            if self.on_error:
                self.on_error(source, code, text)
            if self.on_event:
                self.on_event(f"服务端报错：{text}")
            return
        if kind == 6:
            if not self._closed:
                self._closed = "服务端强制挂断"
                if self.on_event:
                    self.on_event(self._closed)
            return
        if self.on_event:
            self.on_event(f"服务端消息：{str(msg)[:300]}")

    def _send_signal(self, kind: int, payload: dict | None = None) -> bool:
        """发一条 JSON 信令；连接没了/发不出去就返回 False，绝不抛给调用方。

        推帧线程（20ms 一帧）和主线程都会调到这里，所以用同一把锁串起来。
        """
        ws = self._ws
        if ws is None or self._closed:
            return False
        with self._send_lock:
            self._seq += 1
            message = {
                "type": kind,
                "live_id": self.live_id,
                "conn_id": CONN_ID,
                "seq_id": self._seq,
            }
            if payload is not None:
                message["payload"] = payload
            try:
                ws.send(json.dumps(message))
                return True
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    self._closed = f"发信令失败：{exc}"
                    if self.on_event:
                        self.on_event(self._closed)
                return False

    def send_text(self, content: str) -> bool:
        """type=10 output_transcription：把这轮要说的回复文本给 Vidu（语义提示，不改口型）。

        官方只写了"模型生成的回复文本；content 为空则忽略"，没写它具体驱动什么；
        `action.enabled=false` 时它不上匹配，只当表情/节奏的语境用。
        """
        text = (content or "").strip()
        if not text:
            return False
        now = int(time.time() * 1000)
        return self._send_signal(
            10,
            {"text_msg": {"msg_id": f"noesia-{now}", "content": text[:4000], "timestamp": now}},
        )

    def baseline_reset(self) -> bool:
        """type=15：把画面复位到闭口静息态。成功不回执，失败只回 type=103。"""
        return self._send_signal(15)

    def open_stream(self, timeout: float = 20.0) -> None:
        import websocket  # websocket-client

        url = (
            f"{WS}/live/v1/external-lives/{self.live_id}/stream"
            f"?conn_id={CONN_ID}&client_secret={self._secret}"
        )
        self._ws = websocket.create_connection(url, timeout=timeout)
        deadline = time.time() + timeout
        seq = 1
        while time.time() < deadline:
            self._ws.send(
                json.dumps(
                    {
                        "type": 1,
                        "live_id": self.live_id,
                        "conn_id": CONN_ID,
                        "seq_id": seq,
                        "payload": {"conn_init": {"version": 1}},
                    }
                )
            )
            end = time.time() + 3
            while time.time() < end:
                try:
                    raw = self._ws.recv()
                except Exception:  # noqa: BLE001
                    break
                if isinstance(raw, str):
                    msg = json.loads(raw)
                    if msg.get("type") == 2 and msg["payload"]["conn_init_ack"].get("success"):
                        threading.Thread(target=self._recv_loop, daemon=True,
                                         name="vidu-recv").start()
                        return
            seq += 1
            time.sleep(2)
        raise RuntimeError("conn_init 未就绪")

    def push_frame(self, frame: bytes) -> None:
        """推一帧（20ms）；节奏由调用方控制（`AvatarRtc` 的定速发送线程）。"""
        if self._closed:
            raise ConnectionError(self._closed)
        ws = self._ws
        if ws is None:
            return
        with self._send_lock:
            ws.send_binary(frame)

    def push_pcm(self, pcm: bytes) -> None:
        """按实时节奏推 24k 单声道 s16le；推太快口型会跑。"""
        ws = self._ws
        if ws is None:
            return
        step = int(SAMPLE_RATE * FRAME_SECONDS) * 2
        start = time.time()
        for offset in range(0, len(pcm), step):
            with self._send_lock:
                ws.send_binary(pcm[offset : offset + step])
            delay = start + (offset + step) / (SAMPLE_RATE * 2) - time.time()
            if delay > 0:
                time.sleep(delay)

    def interrupt(self) -> bool:
        """打断：type=7，payload 是空对象（官方示例就是空的）。"""
        return self._send_signal(7, {})

    def hangup(self) -> None:
        ws = self._ws
        if not ws:
            return
        try:
            with self._send_lock:
                ws.send(
                    json.dumps(
                        {
                            "type": 5,
                            "live_id": self.live_id,
                            "conn_id": CONN_ID,
                            "seq_id": 99999,
                            "payload": {"hangup": {"hangup_reason": "client_hangup"}},
                        }
                    )
                )
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._ws = None
        self._closed = self._closed or "本地挂断"

    def billing(self) -> str:
        try:
            data = requests.get(
                f"{HTTP}/live/v1/lives/{self.live_id}",
                headers={"Authorization": f"Token {self.token}"},
                timeout=20,
            ).json()
            live = data.get("live", data)
            return f"秒={live.get('billed_seconds')} 积分={live.get('credits_cost')}"
        except Exception as exc:  # noqa: BLE001
            return f"查询失败 {exc}"


def load_wav(path: pathlib.Path) -> bytes:
    """读 PCM16 wav → 24k 单声道 s16le 原始字节。"""
    import wave

    with wave.open(str(path)) as handle:
        channels, width, rate = handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit("只支持 16bit wav")
    data = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != SAMPLE_RATE:
        count = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(
            np.linspace(0, len(data) - 1, count), np.arange(len(data)), data.astype(np.float32)
        ).astype(np.int16)
    return data.tobytes()

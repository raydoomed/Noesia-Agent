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

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

HOST = "api.vidu.cn"
HTTP = f"https://{HOST}"
WS = f"wss://{HOST}"
SAMPLE_RATE = 24000
FRAME_SECONDS = 0.02
CONN_ID = "noesia-avatar"
FRAME_BYTES = int(SAMPLE_RATE * 2 * FRAME_SECONDS)


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


def upload_avatar_asset(token: str, image_path: pathlib.Path) -> dict:
    """把形象图存成云端的「数字人设资产」，返回 `{"id", "status"}`。

    这一步不建会话、不计费。之后建会话传 `avatar_id`，云端就不用每次重新收图、
    重新处理形象。官方原话：「可大幅节省创建数字人的时间」。
    """
    green = build_green_input(
        image_path, pathlib.Path(tempfile.gettempdir()) / "noesia_vidu_asset.jpg"
    )
    body = {
        "image_uri": "data:image/jpeg;base64," + base64.b64encode(green.read_bytes()).decode()
    }

    def _post() -> requests.Response:
        return requests.post(
            f"{HTTP}/live/v1/avatars",
            headers={"Authorization": f"Token {token}"},
            json=body,
            timeout=60,
        )

    resp = _post()
    for _ in range(2):
        if resp.status_code != 429:
            break
        time.sleep(5.0)
        resp = _post()
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Vidu 不认这把 Token（{resp.status_code}）：到「设置 → 语音互动」里重新填一次"
        )
    if resp.status_code != 200:
        raise RuntimeError(f"上传形象失败 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return {"id": str(data["id"]), "status": str(data.get("status", ""))}


def avatar_status(token: str, avatar_id: str) -> str:
    """查一条形象资产现在处理到哪一步：created / processing / success / failed。

    只读、不传图——等云端处理完就走这个，别再拿整图重复 POST（那样会被限流）。
    429 / 5xx / 网络抽风返回 "unknown"，调用方接着等就行，不当作失败。
    """
    try:
        resp = requests.get(
            f"{HTTP}/live/v1/avatars/{avatar_id}",
            headers={"Authorization": f"Token {token}"},
            timeout=30,
        )
    except requests.RequestException:
        return "unknown"
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Vidu 不认这把 Token（{resp.status_code}）：到「设置 → 语音互动」里重新填一次"
        )
    if resp.status_code == 404:
        return "missing"
    if resp.status_code != 200:
        return "unknown"
    return str(resp.json().get("status", ""))


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
        self._send_lock = threading.Lock()
        self._closed = ""
        self.on_event = None
        self.on_error = None

    def create(self, avatar_id: str = "") -> dict:
        """建会话。`avatar_id` 非空就只传编号，否则现传整张图（两者二选一）。"""
        payload = {
            "model": self.model,
            "managed_rtc": {"enable_recording": False},
            "action": {"enabled": True, "library_id": "0"},
        }
        if avatar_id:
            payload["avatar_id"] = avatar_id
        else:
            green = build_green_input(
                self.image_path, pathlib.Path(tempfile.gettempdir()) / "noesia_vidu_input.jpg"
            )
            payload["image_uri"] = (
                "data:image/jpeg;base64," + base64.b64encode(green.read_bytes()).decode()
            )
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

    def send_action(self, content: str) -> bool:
        """type=9 input_transcription：驱动数字人动作的文本。

        和 type=10 分工不同：10 是「她这轮要念什么」的语义提示，9 才是拿去动作库里找
        相近动作的输入（官方示例 content 是「比个心」这种话）。建会话时没带
        `action.enabled` 的话，这条发上去也没人用。
        """
        text = (content or "").strip()
        if not text:
            return False
        now = int(time.time() * 1000)
        return self._send_signal(
            9,
            {
                "text_msg": {
                    "msg_id": f"noesia-act-{now}",
                    "content": text[:4000],
                    "timestamp": now,
                }
            },
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
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    kind = msg.get("type")
                    if kind == 103:
                        error = (msg.get("payload") or {}).get("error") or {}
                        raise RuntimeError(
                            describe_error(
                                str(error.get("source") or ""),
                                str(error.get("code") or ""),
                                str(error.get("message") or ""),
                            )
                        )
                    if kind == 6:
                        raise RuntimeError("服务端在建会话阶段强制挂断了")
                    if kind == 2:
                        ack = (msg.get("payload") or {}).get("conn_init_ack") or {}
                        if ack.get("success"):
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


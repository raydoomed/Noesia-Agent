"""语音互动的音频来源：一句话 → 24k 单声道 s16le PCM 分片，边合成边给。

纯语音和语音互动两档都从这里拿流：拿到第一片就能出声、能推给 Vidu 驱动口型。
provider 复用同一套设置（speech_prefs.current()），所以换音色/换引擎两边同步生效。

三家的流式来源各不相同：
- 豆包：`/api/v3/tts/unidirectional` 本身就是分行 JSON + base64 PCM（48k），直接边读边给
- Edge：edge-tts 的音频本来就是 chunk，但只有 mp3，得用 ffmpeg 管道增量解码
- ElevenLabs：HTTP 流式响应（pcm_16000），边读边重采样到 24k
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator

import numpy as np

from app.speech_prefs import SpeechError

# 本程序是窗口程序，子进程不带这个标志会在 Windows 上闪一个 cmd 黑窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

TARGET_RATE = 24000  # Vidu 组件版要 24k 单声道；本地播放也用这个，省一次重采样

# 单次请求送给语音服务的最大字数。整段回答多长都照念，超过这个数就按句子切成几段，
# 一段接一段合成、连续喂给播放器——段与段之间没有空隙（TTS 比实时快几倍，后段在前段还在播时就合成了）。
SEGMENT_CHARS = 300


def _split_for_tts(text: str, limit: int = SEGMENT_CHARS) -> list[str]:
    """把长文本切成每段不超过 limit 字的几段，尽量在句子边界断开。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[。！？；\n])", text):
        if not sentence:
            continue
        while len(sentence) > limit:
            # 单句本身超长：先找逗号，再找不到就硬切（罕见，只在没有标点的长串里出现）
            cut = max(sentence.rfind(mark, 0, limit) for mark in "，、：, ")
            cut = cut + 1 if cut > limit // 3 else limit
            pieces.append(sentence[:cut])
            sentence = sentence[cut:]
        if len(buf) + len(sentence) > limit:
            if buf:
                pieces.append(buf)
            buf = sentence
        else:
            buf += sentence
    if buf:
        pieces.append(buf)
    return pieces


class Resampler:
    """把任意采样率转成 24k，分片调用，跨分片保留相位和尾巴。"""

    def __init__(self, source_rate: int, target_rate: int = TARGET_RATE) -> None:
        self.step = source_rate / target_rate
        self.buffer = np.zeros(0, dtype=np.float32)
        self.phase = 0.0

    def push(self, pcm: bytes) -> bytes:
        """喂一段原始 PCM，返回重采样后的 24k PCM（可能为空，凑不满一个点）。"""
        if not pcm or self.step == 1.0:
            return pcm
        block = np.concatenate(
            [self.buffer, np.frombuffer(pcm, dtype=np.int16).astype(np.float32)]
        )
        out: list[float] = []
        while self.phase + 1 < len(block):
            index = int(self.phase)
            frac = self.phase - index
            out.append(block[index] * (1 - frac) + block[index + 1] * frac)
            self.phase += self.step
        if out:
            cut = int(self.phase)
            self.buffer = block[cut:]
            self.phase -= cut
            values = np.clip(np.array(out, dtype=np.float32), -32768, 32767)
            return values.astype(np.int16).tobytes()
        self.buffer = block
        return b""


def stream_pcm(text: str) -> Iterator[bytes]:
    """按当前语音设置流式合成，产出 24k 单声道 s16le 分片。

    整段文字多长都念完：超过 SEGMENT_CHARS 就分段合成，一段接一段，中间不断。
    """
    from app.speech_prefs import current

    chosen = current()
    provider = chosen.get("provider") or "edge"
    if provider == "edge":
        synth = _stream_edge
    elif provider == "doubao":
        synth = _stream_doubao
    else:
        synth = _stream_elevenlabs
    for segment in _split_for_tts(text):
        yield from synth(segment, chosen)


def _stream_doubao(text: str, chosen: dict) -> Iterator[bytes]:
    from app.doubao_voices import default_resource
    from app.speech_prefs import DOUBAO_SAMPLE_RATE, DOUBAO_URL, _api_key, doubao_error_message

    resource = str(chosen.get("model_id") or "").strip() or default_resource()
    body = json.dumps(
        {
            "req_params": {
                "text": text,
                "speaker": chosen["voice_id"],
                "audio_params": {"format": "pcm", "sample_rate": DOUBAO_SAMPLE_RATE},
            }
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        DOUBAO_URL,
        data=body,
        headers={
            "X-Api-Key": _api_key("doubao"),
            "X-Api-Resource-Id": resource,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    resampler = Resampler(DOUBAO_SAMPLE_RATE)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            for raw in response:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                chunk = message.get("data")
                if chunk:
                    pcm = resampler.push(base64.b64decode(chunk))
                    if pcm:
                        yield pcm
                if message.get("code") == 20000000:
                    return
                if message.get("code"):
                    raise SpeechError(doubao_error_message(
                        int(message.get("code") or 0), str(message.get("message") or "")
                    ))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SpeechError(doubao_error_message(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise SpeechError(f"连不上豆包：{exc.reason}") from exc


def _stream_edge(text: str, chosen: dict) -> Iterator[bytes]:
    try:
        import edge_tts
    except ImportError:
        raise SpeechError("没装 edge-tts：先 pip install edge-tts") from None

    decoder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         # 不带这个，Windows 上每句朗读都会闪一个 cmd 黑窗（本程序是窗口程序，没有控制台可继承）
         "-f", "s16le", "-ar", str(TARGET_RATE), "-ac", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
        creationflags=_NO_WINDOW,
    )
    stop = threading.Event()

    def pump() -> None:
        async def run() -> None:
            communicate = edge_tts.Communicate(text, chosen["voice_id"])
            async for piece in communicate.stream():
                if piece["type"] == "audio" and decoder.stdin:
                    decoder.stdin.write(piece["data"])
                    decoder.stdin.flush()

        try:
            asyncio.run(run())
        finally:
            stop.set()
            try:
                decoder.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=pump, daemon=True).start()
    assert decoder.stdout is not None
    try:
        while True:
            block = decoder.stdout.read(4096)
            if block:
                yield block
                continue
            # 读到空不等于「合成完了」：ffmpeg 出错自己退了也是空，
            # 只认 stop 的话这里会一直空转；反过来只认 poll() 又会把末尾几个块丢掉。
            if stop.is_set() or decoder.poll() is not None:
                break
            time.sleep(0.01)
    finally:
        # 被中途打断（换句/停止）时，这里必须把 ffmpeg 收掉，否则它挂在管道上等 stdin
        try:
            if decoder.poll() is None:
                decoder.kill()
            decoder.wait(timeout=5)
        except Exception:  # noqa: BLE001 — 收尾失败不影响已经拿到的音频
            pass


def _stream_elevenlabs(text: str, chosen: dict) -> Iterator[bytes]:
    from app.speech_prefs import ELEVENLABS_URL, _api_key, elevenlabs_error_message

    if not str(chosen.get("voice_id") or "").strip():
        # 音色属于账号：没选就不发请求，也不拿别人的 id 去试
        raise SpeechError(
            "还没选 ElevenLabs 的音色：「设置 → 语音」里选一个（清单是用你自己的 Key 拉的），"
            "或者改选不用 Key 的 Edge。"
        )
    url = ELEVENLABS_URL.format(voice_id=chosen["voice_id"]) + "?output_format=pcm_16000"
    body = json.dumps({"text": text, "model_id": chosen["model_id"]}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"xi-api-key": _api_key("elevenlabs"), "Content-Type": "application/json"},
        method="POST",
    )
    resampler = Resampler(16000)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            while True:
                block = response.read(4096)
                if not block:
                    return
                pcm = resampler.push(block)
                if pcm:
                    yield pcm
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SpeechError(elevenlabs_error_message(exc.code, detail)) from exc

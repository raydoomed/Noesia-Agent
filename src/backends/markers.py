"""控制标记的解析与剥离（所有 LLM 后端共用）。

模型在回复里夹带情绪标记，用来告诉上层「她是什么情绪」。标记不能念给用户听、也不能进 TTS：

    [[emotion:名字:强度]]                强度 0~1，省略则 0.5

记忆不走这套标记：写记忆是引擎 `memory` 工具的事，模型直接调工具，回复正文里不再需要
「要记什么」的暗号。

为什么放在独立模块：这是**外壳自己的**协议（她在引擎回复的正文里夹一句情绪，界面据此换表情），
推理在引擎、解析在这里，所以放在与后端无关的模块里，`chat.py` 与后续的 TTS 共用一份。

**注意**：这里只用字符串切分与枚举校验，不做正则语义判断（见 docs/conventions.md）。
解析结果是一等数据结构 `LLMResult`，上层不重新解读文本。
"""

from __future__ import annotations

import logging

from .base import LLMResult

logger = logging.getLogger(__name__)


# 模型偶尔会把标记写成全角（「：」「（）」「［［」）。只在标记体内部统一成全角→半角，
# 正文一个字都不动——那些标点是要念给用户听的，改了就是改她的语气。
FULLWIDTH = {"：": ":", "，": ",", "（": "(", "）": ")", "［": "[", "］": "]", "　": " "}
OPEN_FORMS = ("[[", "［［")
CLOSE_FORMS = ("]]", "］］")


def _find_open(text: str, start: int = 0) -> tuple[int, int]:
    """最早出现的开标记：(位置, 标记长度)；没有返回 (-1, 0)。"""
    best, width = -1, 0
    for form in OPEN_FORMS:
        at = text.find(form, start)
        if at != -1 and (best == -1 or at < best):
            best, width = at, len(form)
    return best, width


def _find_close(text: str, start: int) -> tuple[int, int]:
    """开标记之后最近出现的闭标记：(位置, 标记长度)；没有返回 (-1, 0)。"""
    best, width = -1, 0
    for form in CLOSE_FORMS:
        at = text.find(form, start)
        if at != -1 and (best == -1 or at < best):
            best, width = at, len(form)
    return best, width


def normalize_marker(body: str) -> str:
    """标记体统一成全角→半角，去掉首尾空白（内容里的正常字符保持原样）。"""
    for wide, half in FULLWIDTH.items():
        body = body.replace(wide, half)
    return body.strip()


class ControlTagFilter:
    """从流式文本里剥掉 `[[...]]` 控制标记，允许标记跨块。"""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            start, open_width = _find_open(self._buf)
            if start == -1:
                # 末尾可能是半个开标记（"[[" 或 "［［"），先压住，等下一块再判断
                keep = 1 if self._buf.endswith(("[", "［")) else 0
                if keep:
                    out.append(self._buf[:-1])
                    self._buf = self._buf[-1:]
                else:
                    out.append(self._buf)
                    self._buf = ""
                break
            out.append(self._buf[:start])
            end, close_width = _find_close(self._buf, start + open_width)
            if end == -1:  # 标记还没收完，留在缓冲里
                self._buf = self._buf[start:]
                break
            self._buf = self._buf[end + close_width:]
        return "".join(out)

    def flush(self) -> str:
        """收尾：没闭合的标记直接丢掉，宁可少一句话也不要把 `[[` 念出来。"""
        rest, self._buf = self._buf, ""
        if _find_open(rest)[0] != -1:
            logger.warning("控制标记没闭合，这段被丢弃：%r", rest[:60])
            return ""
        return rest


def split_markers(text: str) -> tuple[str, list[str]]:
    """把可见文本与所有 `[[...]]` 标记分开。未闭合的标记连同其后内容一起丢弃（并记日志）。"""
    markers: list[str] = []
    clean: list[str] = []
    rest = text
    while True:
        start, open_width = _find_open(rest)
        if start == -1:
            clean.append(rest)
            break
        clean.append(rest[:start])
        end, close_width = _find_close(rest, start + open_width)
        if end == -1:
            # 未闭合：后面的正文没法判断是标记还是内容，只能丢；但必须留痕，否则永远查不到
            logger.warning("控制标记没闭合，后面 %d 字被丢弃：%r", len(rest) - start, rest[start:start + 60])
            break
        markers.append(rest[start + open_width: end])
        rest = rest[end + close_width:]
    return "".join(clean).strip(), markers


def parse_emotion(marker: str) -> tuple[str, float] | None:
    """`emotion:名字:强度` → (名字, 强度)；不是情绪标记返回 None。"""
    parts = normalize_marker(marker).split(":")
    if len(parts) < 2 or parts[0].strip().lower() != "emotion":
        return None
    name = parts[1].strip().lower() or "neutral"
    intensity = 0.5
    if len(parts) >= 3 and parts[2].strip():
        try:
            intensity = min(max(float(parts[2]), 0.0), 1.0)
        except ValueError:
            intensity = 0.5
    return name, intensity


def finalize_with_markers(text: str) -> LLMResult:
    """把带标记的完整回复整理成结构化结果（正文 + 情感）。

    认不出来的标记一律丢掉——留在正文里会被念给用户听。这里不写 warning：
    标记是模型自己写歪的，日志里刷这些对定位没帮助（情绪缺一次就是 neutral，无害）。
    """
    body, markers = split_markers(text)
    emotion, intensity = "neutral", 0.5
    for marker in markers:
        parsed = parse_emotion(marker)
        if parsed is not None:
            emotion, intensity = parsed
    return LLMResult(text=body, emotion=emotion, emotion_intensity=intensity)

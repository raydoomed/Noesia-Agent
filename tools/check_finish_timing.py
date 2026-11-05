"""离线验证"什么时候收尾"：用真实的 TTS + 模拟云端回声，不起 Vidu 会话、不花积分。

模拟：推送开始后 1.4 秒云端才开始回音频（实测过），回来后持续 = 这句话的长度，
之后是它一路多送的静音。然后看 StreamSpeaker 什么时候收尾。
"""

from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

from app.speech_interactive import StreamSpeaker  # noqa: E402

LEAD = 1.4          # 云端开始回音频的延迟（实测）
RATE_BYTES = 24000 * 2


class Cloud:
    """假装是 Vidu：回声比推送晚 LEAD 秒开始，语音段 = 句子长度，之后一路多送静音。"""

    def __init__(self) -> None:
        self.sent = 0
        self.t0 = time.monotonic()
        self.speech = 0.0

    def feed(self, pcm: bytes) -> None:
        self.sent += len(pcm)

    def timeline(self) -> int:
        """时间轴：已经回传的音频字节（语音段 + 后面无尽的静音）。"""
        elapsed = time.monotonic() - self.t0 - LEAD
        if elapsed <= 0:
            return 0
        return int(min(elapsed, self.speech) * RATE_BYTES)

    def quiet(self) -> float:
        """远端安静了多久：没出声前是 0（还没听见过），说完之后才开始涨。"""
        elapsed = time.monotonic() - self.t0 - LEAD
        if elapsed <= 0:
            return 0.0
        return max(0.0, elapsed - self.speech)


TEXT = "这句话是用来验证收尾时机的，她应该念完整句才收回画面，不能提前。"

app = QtWidgets.QApplication([])
cloud = Cloud()
speaker = StreamSpeaker(
    on_chunk=cloud.feed,
    limit_fn=cloud.timeline,
    quiet_fn=cloud.quiet,
    play_local=False,
)
marks: dict[str, float] = {}
speaker.started.connect(lambda _t: marks.setdefault("start", time.monotonic()))
speaker.finished.connect(lambda: marks.setdefault("finish", time.monotonic()))

# 先量这句话本身有多长（只跑一次 TTS，不播）
speaker.speak(TEXT)
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    QtTest.QTest.qWait(40)
    if not speaker.playing and speaker._total_bytes:
        break
total = speaker._total_bytes
dur = total / RATE_BYTES
speaker.stop()
if dur <= 0:
    # 解码链任何一步失败（写 ffmpeg stdin 报 OSError 之类），`_total_bytes` 就停在 0：
    # 拿 0 去比会打出「晚 13 秒」这种假结论，还退 0。测量没成功就明说，别给结论。
    raise SystemExit("[失败] 这句话一个字节都没解出来：解码链断了（看上面的异常），这次测量无效")

cloud = Cloud()
cloud.speech = dur
speaker = StreamSpeaker(
    on_chunk=cloud.feed, limit_fn=cloud.timeline, quiet_fn=cloud.quiet, play_local=False
)
done2: dict = {}
speaker.finished.connect(lambda: done2.setdefault("finish", time.monotonic()))
t0 = time.monotonic()
speaker.speak(TEXT)
deadline = t0 + dur + 25
while time.monotonic() < deadline and "finish" not in done2:
    QtTest.QTest.qWait(40)

finish_at = done2.get("finish", 0) - t0
print("句子长度 %.2f 秒 | 云端 %.1f 秒后才出声 | 收尾发生在 %.2f 秒"
      % (dur, LEAD, finish_at), flush=True)
verdict = "太早" if finish_at < LEAD + dur - 0.05 else ("晚 %.1f 秒" % (finish_at - LEAD - dur))
print("对比\"云端念完那一刻(%.2f 秒)\"：%s" % (LEAD + dur, verdict), flush=True)

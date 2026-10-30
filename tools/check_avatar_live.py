"""手动验一次互动数字人整条链路：预加载 → 预热 → 推音频 → 收帧 → 挂断。

会真的建 Vidu 会话，**按秒计费（1 积分/秒，一次约 6~10 积分）**，所以不进 tests/。

用法：
    python tools/check_avatar_live.py --wav 语音.wav

需要一段 24k 单声道 s16le 的 wav，可以这样生成：
    python -m edge_tts --voice zh-CN-XiaoxiaoNeural --text "你好呀" --write-media a.mp3
    ffmpeg -i a.mp3 -ac 1 -ar 24000 语音.wav
（`load_wav` 会自己转采样率，用别的采样率也行）
"""

from __future__ import annotations

import pathlib
import tempfile
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6 import QtCore, QtWidgets  # noqa: E402

from app.avatar_rtc import AvatarRtc  # noqa: E402
from app.vidu_session import FRAME_BYTES, SAMPLE_RATE, load_wav  # noqa: E402

OUT = pathlib.Path(tempfile.gettempdir()) / "noesia_avatar_check"
OUT.mkdir(exist_ok=True)

WAV = pathlib.Path(sys.argv[sys.argv.index("--wav") + 1]) if "--wav" in sys.argv else None
if WAV is None or not WAV.exists():
    raise SystemExit("用法：python tools/check_avatar_live.py --wav 语音.wav")
app = QtWidgets.QApplication([])
avatar = AvatarRtc()
HOLD = float(sys.argv[sys.argv.index("--hold") + 1]) if "--hold" in sys.argv else 0.0
FULL = pathlib.Path(sys.argv[sys.argv.index("--save-full") + 1]) if "--save-full" in sys.argv else None
if FULL is None:
    avatar.set_target_size(QtCore.QSize(360, 480))  # 不存原图就直接按显示尺寸跑

stamps: list[float] = []
shots: list = []
marks = {"ready_at": 0.0}
t_warm = 0.0

avatar.ready.connect(lambda: marks.update(ready_at=time.perf_counter() - t_warm))
avatar.ended.connect(lambda: print("ended 信号到达", flush=True))
avatar.failed.connect(lambda msg: print("失败:", msg, flush=True))


def on_frame(image):
    if FULL is not None and not FULL.exists():
        image.save(str(FULL))                     # 存一张全分辨率原帧，用来比清晰度
        avatar.set_target_size(QtCore.QSize(360, 480))
        print("已存全分辨率帧:", image.size().toTuple(), flush=True)
    stamps.append(time.perf_counter())
    if len(stamps) in (5, 30):
        shots.append(image.copy())


avatar.frame_ready.connect(on_frame)

t0 = time.perf_counter()
print("preload:", avatar.preload() or "OK", "%.0f ms" % ((time.perf_counter() - t0) * 1000), flush=True)

t_warm = time.perf_counter()
avatar.warm()
deadline = time.time() + 40
while avatar.state != "ready" and time.time() < deadline:
    app.processEvents()
    time.sleep(0.02)
print("预热完成: state=%s，用时 %.1fs" % (avatar.state, time.perf_counter() - t_warm), flush=True)


marks.update(first_push=0.0, last_push=0.0)


def feed():
    pcm = load_wav(WAV)[: SAMPLE_RATE * 2 * 3]  # 只推前 3 秒，省积分
    step = FRAME_BYTES
    start = time.perf_counter()
    marks["first_push"] = start
    for i in range(0, len(pcm), step):
        avatar.feed_pcm(pcm[i : i + step])
        delay = start + (i + step) / (SAMPLE_RATE * 2) - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    marks["last_push"] = time.perf_counter()


print("静音保持 2 秒（模拟：会话建好后用户还在打字）", flush=True)
hold = time.time() + 2
while time.time() < hold:
    app.processEvents()
    time.sleep(0.02)
stamps.clear()

if avatar.begin_speak():
    threading.Thread(target=feed, daemon=True).start()
    print("开始推音频", flush=True)
else:
    print("begin_speak 失败（会话没就绪）", flush=True)

end = time.time() + 6
while time.time() < end:
    app.processEvents()
    time.sleep(0.02)

print("收到帧数:", len(stamps), "| 首帧尺寸:", shots[0].size().toTuple() if shots else None, flush=True)
if len(stamps) > 5:
    span = stamps[-1] - stamps[0]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    print("帧率 %.1f fps | 帧间隔中位 %.0fms 最大 %.0fms"
          % ((len(stamps) - 1) / span, sorted(gaps)[len(gaps) // 2] * 1000, max(gaps) * 1000), flush=True)
if HOLD:
    print("继续挂 %.0f 秒（只推静音），看连接会不会被踢…" % HOLD, flush=True)
    end = time.time() + HOLD
    while time.time() < end:
        app.processEvents()
        time.sleep(0.05)
    print("挂着结束: state=%s，收到帧 %d" % (avatar.state, len(stamps)), flush=True)

p0, p1 = marks.get("first_push", 0.0), marks.get("last_push", 0.0)
during = [s for s in stamps if p0 and s >= p0]
if len(during) > 3:
    span = during[-1] - during[0]
    print("说话这 %.1fs 里 %d 帧 → %.1f fps | 推第一帧音频到画面第一帧: %.0f ms"
          % (p1 - p0, len(during), (len(during) - 1) / span, (during[0] - p0) * 1000), flush=True)
for i, image in enumerate(shots):
    image.save(str(OUT / f"rtc_frame_{i}.png"))

avatar.hangup()
for _ in range(30):
    app.processEvents()
    time.sleep(0.02)
print("已挂断", flush=True)

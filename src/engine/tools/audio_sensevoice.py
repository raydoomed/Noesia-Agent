"""SenseVoice 语音识别：本地 ONNX 推理。

只依赖 `onnxruntime` + `kaldi-native-fbank` + `numpy`，**不装 `funasr-onnx`**：
那个包会带进 librosa、scipy、jieba、sentencepiece 和 modelscope 一整串（近 300 MB），
还钉死 `numpy<=1.26.4`，装上去会把项目现有的 numpy 2.x 降级。

前处理自己实现：80 维 fbank → LFR(7,6) 帧堆叠成 560 维 → CMVN 归一化。这三步都是
公开算法，参数来自模型自带的 `am.mvn`，不依赖上游代码。

模型三个文件，第一次用的时候下到 `data/models/SenseVoice/`：
`model_quant.onnx`(230 MB) / `tokens.json` / `am.mvn`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
N_MELS = 80
LFR_M = 7
LFR_N = 6
FEAT_DIM = N_MELS * LFR_M  # 560

# 空闲多久把模型从内存里放掉。模型占 100 多 MB 常驻没必要，
# 用户不说话了就该还回去
IDLE_UNLOAD_SECONDS = 300.0

_REPO = "iic/SenseVoiceSmall-onnx"
_BASE = f"https://modelscope.cn/models/{_REPO}/resolve/master"

_FILES: dict[str, tuple[int, str]] = {
    "model_quant.onnx": (241216270, "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822"),
    "tokens.json": (352064, "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97"),
    "am.mvn": (11203, "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5"),
}

# 输出里带一堆角标：<|zh|><|HAPPY|><|Speech|><|woitn|>……全剥掉只剩正文
_TAG = re.compile(r"<\|[^|]*\|>")

# 下载超时。模型 230 MB，10 秒超时在大文件上会误伤
_TIMEOUT = 30


class ModelMissing(RuntimeError):
    """模型还没下好。消息可以直接给用户看。"""


# ---------------------------------------------------------------- 路径

def model_dir() -> Path:
    """模型存放目录：项目内 `data/models/SenseVoice`。"""
    return Path(__file__).resolve().parents[3] / "data" / "models" / "SenseVoice"


def missing_files() -> list[str]:
    """还没下好或大小不对的文件。"""
    root = model_dir()
    return [
        name for name, (size, _) in _FILES.items()
        if not (root / name).is_file() or (root / name).stat().st_size != size
    ]


def is_ready() -> bool:
    return not missing_files()


def total_download_bytes() -> int:
    return sum(size for size, _ in _FILES.values())


def downloaded_bytes() -> int:
    root = model_dir()
    done = 0
    for name, (size, _) in _FILES.items():
        path = root / name
        if path.is_file():
            done += min(path.stat().st_size, size)
    return done


# ---------------------------------------------------------------- 下载

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(progress: Callable[[str, int, int], None] | None = None) -> None:
    """把缺的文件下下来。

    `progress(文件名, 已下字节, 总字节)`——界面拿它画进度条。
    断点续传：本地已有 `.part` 就带 `Range` 头接着下。
    **sha256 对不上就整个重下**，不留半截文件让后面的人以为是好的。
    """
    root = model_dir()
    root.mkdir(parents=True, exist_ok=True)

    for name, (size, digest) in _FILES.items():
        path = root / name
        if path.is_file() and path.stat().st_size == size:
            continue

        part = root / f"{name}.part"
        already = part.stat().st_size if part.is_file() else 0
        if already > size:
            part.unlink()
            already = 0

        for attempt in range(3):
            try:
                _fetch(name, part, already, progress)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning("下载 %s 第 %d 次失败：%s", name, attempt + 1, exc)
                if attempt == 2:
                    raise ModelMissing(f"下载识别模型失败（{name}）：{exc}") from exc
                already = part.stat().st_size if part.is_file() else 0
                time.sleep(1.5 * (attempt + 1))

        if _sha256(part) != digest:
            part.unlink(missing_ok=True)
            raise ModelMissing(f"识别模型校验不通过（{name}），请重试")
        part.replace(path)
        logger.info("[语音] 模型文件就位：%s", name)


def _fetch(name: str, part: Path, start: int, progress) -> None:
    headers = {"User-Agent": "Noesia/1.0"}
    if start:
        headers["Range"] = f"bytes={start}-"
    request = urllib.request.Request(f"{_BASE}/{name}", headers=headers)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response, part.open("ab") as handle:
        total = int(response.headers.get("Content-Length") or 0) + start
        done = start
        last = 0.0
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            handle.write(block)
            done += len(block)
            now = time.monotonic()
            if progress and now - last > 0.1:  # 每秒最多报十次，别把界面刷死
                last = now
                progress(name, done, total)
        if progress:
            progress(name, done, total)


# ---------------------------------------------------------------- 推理

class _Engine:
    """模型 + 分词器 + CMVN 参数，一次装好反复用。"""

    def __init__(self, root: Path) -> None:
        missing = missing_files()
        if missing:
            raise ModelMissing(f"识别模型还没下载好（缺 {', '.join(missing)}）")

        import onnxruntime

        options = onnxruntime.SessionOptions()
        # 语音输入是「说完就要」，抢 4 个核足够；再多会跟界面抢 CPU
        options.intra_op_num_threads = 4
        options.log_severity_level = 3
        started = time.monotonic()
        self.session = onnxruntime.InferenceSession(
            str(root / "model_quant.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.tokens: list[str] = json.loads((root / "tokens.json").read_text(encoding="utf-8"))
        shift, scale = _read_cmvn(root / "am.mvn")
        self.shift = shift
        self.scale = scale
        self.loaded_at = time.monotonic()
        logger.info("[语音] SenseVoice 就绪，加载 %.2f 秒", time.monotonic() - started)

    def run(self, samples) -> str:
        import numpy as np
        import kaldi_native_fbank as knf

        features = _fbank(knf, samples)
        if features.shape[0] < LFR_N:
            return ""
        features = _apply_cmvn(_lfr(features, LFR_M, LFR_N), self.shift, self.scale)

        logits, lengths = self.session.run(None, {
            "speech": features[None, :, :],
            "speech_lengths": np.array([features.shape[0]], dtype=np.int32),
            "language": np.array([0], dtype=np.int32),   # 0 = 自动判语种
            "textnorm": np.array([0], dtype=np.int32),   # 0 = 数字带标点
        })
        return _decode(np, logits[0], int(lengths[0]), self.tokens)


def _read_cmvn(path: Path):
    """解析 am.mvn：两个 560 维向量，依次是偏移量和缩放系数。

    文件是 Kaldi 的文本格式，`<AddShift>`/`<Rescale>` 后面各跟着一行
    `<LearnRateCoef> 0 [ 560 个数 ]`，数值在方括号里。
    """
    import numpy as np

    vectors = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "[" not in line or "]" not in line:
            continue
        inner = line[line.index("[") + 1: line.rindex("]")]
        values = [float(token) for token in inner.split() if token]
        if len(values) == FEAT_DIM:
            vectors.append(np.array(values, dtype=np.float32))
    if len(vectors) != 2:
        raise ModelMissing("识别模型的归一化参数不完整（am.mvn）")
    return vectors[0], vectors[1]


def _fbank(knf, samples):
    """80 维 fbank，跟模型训练时一致：16k、25ms 窗、10ms 步、hamming。"""
    import numpy as np

    options = knf.FbankOptions()
    options.frame_opts.samp_freq = SAMPLE_RATE
    options.frame_opts.frame_shift_ms = 10.0
    options.frame_opts.frame_length_ms = 25.0
    options.frame_opts.window_type = "hamming"
    options.frame_opts.dither = 0.0        # 抖动是给训练用的，推理要可复现
    options.frame_opts.snip_edges = True
    options.mel_opts.num_bins = N_MELS
    options.energy_floor = 0.0

    extractor = knf.OnlineFbank(options)
    extractor.accept_waveform(SAMPLE_RATE, samples.tolist())
    extractor.input_finished()
    frames = [extractor.get_frame(index) for index in range(extractor.num_frames_ready)]
    return np.array(frames, dtype=np.float32)


def _lfr(features, m: int, n: int):
    """低帧率堆叠：每 n 帧取一段、把 m 帧拼成一帧。

    首帧复制 (m-1)//2 次垫在左边，尾部不够就复制末帧补齐——这样输出帧数正好是
    `ceil(T/n)`，跟模型训练时一致。
    """
    import numpy as np

    total = features.shape[0]
    output_length = int(math.ceil(total / n))
    padded = np.vstack((np.tile(features[0], ((m - 1) // 2, 1)), features))
    total = padded.shape[0]

    rows = []
    for index in range(output_length):
        offset = index * n
        if m <= total - offset:
            rows.append(padded[offset:offset + m].reshape(1, -1))
            continue
        row = padded[offset:].reshape(-1)
        for _ in range(m - (total - offset)):
            row = np.hstack((row, padded[-1]))
        rows.append(row.reshape(1, -1))
    return np.vstack(rows).astype(np.float32)


def _apply_cmvn(features, shift, scale):
    return (features + shift) * scale


def _decode(np, logits, length: int, tokens: list[str]) -> str:
    """CTC 贪心解码：逐帧取最大，去掉连续重复和空白。

    id 0/1/2 是 `<unk>`、`<s>`、`</s>`，不参与拼接。
    """
    ids = logits.argmax(axis=-1)
    pieces: list[str] = []
    previous = -1
    for raw in ids[:max(0, length)]:
        index = int(raw)
        if index != previous and index not in (0, 1, 2):
            pieces.append(tokens[index])
        previous = index
    text = "".join(pieces)
    text = _TAG.sub("", text)
    return text.replace("▁", " ").strip()


# ---------------------------------------------------------------- 对外

_lock = threading.Lock()
_engine: _Engine | None = None
_last_used = 0.0


def _get_engine() -> _Engine:
    global _engine, _last_used
    with _lock:
        if _engine is not None and time.monotonic() - _last_used > IDLE_UNLOAD_SECONDS:
            logger.info("[语音] 模型空闲超时，放掉")
            _engine = None
        if _engine is None:
            _engine = _Engine(model_dir())
        _last_used = time.monotonic()
        return _engine


def unload() -> None:
    """把模型放掉（下次用会重新加载）。"""
    global _engine
    with _lock:
        _engine = None


_warm_lock = threading.Lock()
_warming: threading.Thread | None = None


def warm() -> None:
    """后台把模型读进内存。

    第一次识别要等六秒（241 MB 从盘上读 + 建会话），而人说完一句话就在等结果。
    进语音模式时就该开始加载，别等按下说话才等。

    模型还没下就什么都不做——那种情况界面上该显示的是「正在下载」。
    """
    global _warming
    if missing_files():
        return
    with _warm_lock:
        if _warming is not None and _warming.is_alive():
            return
        _warming = threading.Thread(target=_warm_worker, name="sensevoice-warm", daemon=True)
        thread = _warming
    thread.start()


def _warm_worker() -> None:
    try:
        _get_engine()
    except Exception:  # noqa: BLE001 - 预热失败就算了，真识别的时候还会再试一次
        logger.warning("[语音] 预热失败", exc_info=True)


def transcribe(wav_path: str | Path, sample_rate: int = SAMPLE_RATE) -> str:
    """一段 WAV → 文本。识别不出东西返回空串。"""
    import numpy as np
    import wave

    path = Path(wav_path)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    if width != 2:
        raise ValueError(f"只支持 16 位 PCM（收到 {width * 8} 位）")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != sample_rate:
        # 采集层已经固定要 16k，走到这里说明调用方给了别的文件；
        # 不静默重采样，让问题暴露出来
        raise ValueError(f"采样率必须是 {sample_rate}Hz（收到 {rate}Hz）")

    engine = _get_engine()
    return engine.run(samples)

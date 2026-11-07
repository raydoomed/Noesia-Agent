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

IDLE_UNLOAD_SECONDS = 300.0

_REPO = "iic/SenseVoiceSmall-onnx"
_BASE = f"https://modelscope.cn/models/{_REPO}/resolve/master"

_FILES: dict[str, tuple[int, str]] = {
    "model_quant.onnx": (241216270, "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822"),
    "tokens.json": (352064, "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97"),
    "am.mvn": (11203, "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5"),
}

_TAG = re.compile(r"<\|[^|]*\|>")

_TIMEOUT = 30


class ModelMissing(RuntimeError):
    """模型还没下好。消息可以直接给用户看。"""



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
            if progress and now - last > 0.1:
                last = now
                progress(name, done, total)
        if progress:
            progress(name, done, total)



class _Engine:
    """模型 + 分词器 + CMVN 参数，一次装好反复用。"""

    def __init__(self, root: Path) -> None:
        missing = missing_files()
        if missing:
            raise ModelMissing(f"识别模型还没下载好（缺 {', '.join(missing)}）")

        import onnxruntime

        options = onnxruntime.SessionOptions()
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
            "language": np.array([0], dtype=np.int32),
            "textnorm": np.array([0], dtype=np.int32),
        })
        try:
            words = _vocab()
        except Exception:  # noqa: BLE001
            words = []
        text = ""
        decoded = False
        if words:
            try:
                text = _decode_beam(np, logits[0], int(lengths[0]), self.tokens, words)
                decoded = True
            except Exception:  # noqa: BLE001
                logger.warning("[语音] 词表解码失败，这一句退回普通解码", exc_info=True)
        if not decoded:
            text = _decode(np, logits[0], int(lengths[0]), self.tokens)
        try:
            _remember_speech(_learnable(np, logits[0], int(lengths[0]), self.tokens))
        except Exception:  # noqa: BLE001
            logger.debug("[语音] 这段语料没记上", exc_info=True)
        return text


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
    options.frame_opts.dither = 0.0
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



_VOCAB_BOOST = 1.6
_BEAM_WIDTH = 8
_BEAM_TOP_K = 12

_vocab_lock = threading.Lock()
_vocab_words: tuple[str, ...] = ()
_vocab_stamp: float | None = None


def _vocab_path() -> Path:
    return model_dir().parent.parent / "settings" / "voice_vocab.json"


def _vocab() -> list[str]:
    """当前词表。按文件修改时间缓存：文件没动过就不重读。

    文件不在、读不了、内容不对，一律当空表 —— 那就走原来的贪心解码，
    识别照常出结果。词表出任何问题都不能让识别失败。
    """
    global _vocab_words, _vocab_stamp
    path = _vocab_path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        with _vocab_lock:
            _vocab_words, _vocab_stamp = (), None
        return []
    with _vocab_lock:
        if stamp == _vocab_stamp:
            return list(_vocab_words)
    words: tuple[str, ...] = ()
    read_ok = False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("words") if isinstance(payload, dict) else None
        if isinstance(raw, list):
            words = tuple(w for w in raw if isinstance(w, str) and w)
            read_ok = True
    except (OSError, ValueError):
        pass
    with _vocab_lock:
        if read_ok:
            _vocab_stamp = stamp
        _vocab_words = words
    return list(words)


def _decode_beam(np, logits, length: int, tokens: list[str], vocab: list[str]) -> str:
    """CTC 前缀搜索解码：保留多条候选，拼出词表词的路径概率乘一个奖励。

    为什么要这么做：贪心每帧只看最大的那个，一旦在模糊音上选错就没法回头。
    这里同时留着若干条路径，候选里出现词表词（或正在拼一个词）时给它加分，
    于是「诺基亚」和「诺西亚」这种只差一个韵母的分叉，会按哪边是已知词来定。

    没法做好的部分说清楚：如果模型把音听岔了（塞→载），奖励拉不回来 ——
    那不是选字问题，是声学判断本身错了。
    """
    horizon = max(0, length)
    if horizon == 0 or not vocab:
        return _decode(np, logits, length, tokens)

    log_boost = math.log(_VOCAB_BOOST)
    max_word = max(len(w) for w in vocab)
    index: set[str] = set()
    for word in vocab:
        index.add(word)
        for cut in range(2, len(word)):
            index.add(word[:cut])

    blank_ids = (0, 1, 2)
    beams: dict[tuple[int, ...], list[float]] = {(): [0.0, -1e30]}
    for frame_index in range(horizon):
        frame = logits[frame_index]
        top = np.argsort(frame)[-_BEAM_TOP_K:]
        grown: dict[tuple[int, ...], list[float]] = {}

        def bump(prefix: tuple[int, ...], slot: int, value: float) -> None:
            cell = grown.setdefault(prefix, [-1e30, -1e30])
            cell[slot] = float(np.logaddexp(cell[slot], value))

        for prefix, (p_blank, p_non_blank) in beams.items():
            reachable = float(np.logaddexp(p_blank, p_non_blank))
            for raw in top:
                token = int(raw)
                step = float(frame[token])
                if token in blank_ids:
                    bump(prefix, 0, reachable + step)
                    continue
                if prefix and prefix[-1] == token:
                    bump(prefix, 1, p_non_blank + step)
                    longer = prefix + (token,)
                    bump(longer, 1, p_blank + step + _tail_boost(tokens, longer, index, max_word, log_boost))
                else:
                    longer = prefix + (token,)
                    bump(longer, 1, reachable + step + _tail_boost(tokens, longer, index, max_word, log_boost))

        ranked = sorted(
            grown.items(),
            key=lambda item: -float(np.logaddexp(item[1][0], item[1][1])),
        )[:_BEAM_WIDTH]
        beams = dict(ranked)

    best = max(beams.items(), key=lambda item: float(np.logaddexp(item[1][0], item[1][1])))[0]
    text = "".join(tokens[i] for i in best)
    text = _TAG.sub("", text)
    return text.replace("▁", " ").strip()


def _tail_boost(tokens, prefix: tuple[int, ...], index: set[str], max_word: int, log_boost: float) -> float:
    """这条候选末尾正好拼出一个词表词（或它的开头一段）时给的奖励，否则 0。"""
    tail = "".join(tokens[i] for i in prefix[-max_word:]).replace("▁", "")
    for size in range(min(max_word, len(tail)), 1, -1):
        if tail[-size:] in index:
            return log_boost
    return 0.0



_CONFIDENT = 0.8

_WHOLE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}")


def _learnable(np, logits, length: int, tokens: list[str]) -> str:
    """按把握程度切开的识别文本，专给攒词表用。

    拿不准的字前后断开：那个字很可能就是听错的，连着写下去会把错词学进词表。
    断了以后，同一句里其余八九个字照常可用 —— 语音说话也能攒词表，不必靠打字。
    宁可少学，不能学歪。
    """
    horizon = max(0, length)
    if horizon == 0:
        return ""
    window = logits[:horizon]
    peak = window.max(axis=1, keepdims=True)
    prob = np.exp(window - peak)
    prob /= prob.sum(axis=1, keepdims=True)
    sure = prob.max(axis=1)
    ids = window.argmax(axis=1)

    merged: list[list] = []
    previous = -1
    for frame in range(horizon):
        token = int(ids[frame])
        if token in (0, 1, 2):
            previous = token
            continue
        if token == previous and merged:
            merged[-1][1] = max(merged[-1][1], float(sure[frame]))
        else:
            merged.append([tokens[token], float(sure[frame])])
        previous = token

    parts: list[str] = []
    for piece, confidence in merged:
        piece = _TAG.sub("", piece).replace("▁", " ")
        if not piece:
            continue
        parts.append(piece if confidence >= _CONFIDENT else " ")
    return " ".join("".join(parts).split())


def _speech_corpus_path() -> Path:
    return model_dir().parent.parent / "settings" / "speech_corpus.jsonl"


def _remember_speech(text: str) -> None:
    """把这次识别里可信的片段写进语音语料；app 那边攒够了会拿去算词表。

    和打字的语料分开放：两个进程各写各的文件，不用抢同一个文件。
    切开后只剩孤零零一两个字的（连不成词）直接丢掉，省得白占一条。
    """
    line = (text or "").strip()
    if not _WHOLE.search(line):
        return
    path = _speech_corpus_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": line}, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("[语音] 这段语料没记上", exc_info=True)



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
    except Exception:  # noqa: BLE001
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
        raise ValueError(f"采样率必须是 {sample_rate}Hz（收到 {rate}Hz）")

    engine = _get_engine()
    return engine.run(samples)

"""自动发现专有名词，给语音识别做解码偏置。

不预设任何词：候选全部来自用户实际打过的字，靠统计筛出来。
两路通道：

- 英文 / 数字串：一整段就是一个词，直接数频次
- 中文：2~4 字 n-gram，用「凝聚度」+「左右邻接熵」判定

中文那路的判据：
  凝聚度（PMI）—— 这几个字是不是总黏在一起，而不是各自的常用字碰巧相邻
  邻接熵 —— 这个词左右能接多少种不同的字；能自由出现在句子里各处，才算一个独立的词

频次上限用来挡掉「今天」「什么」这类高频常用词。它们粘得紧、也自由，
但它们不是专名，把它们放进偏置表只会让正常说话也往那儿偏。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

from settings import RESOLVER

logger = logging.getLogger(__name__)

VOCAB_PATH = "data/settings/voice_vocab.json"
CORPUS_PATH = "data/settings/voice_corpus.jsonl"
SPEECH_CORPUS_PATH = "data/settings/speech_corpus.jsonl"

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,}")

_MIN_FREQ = 3
_MAX_DF = 0.3
_DF_MIN_DOCS = 50
_MIN_PMI = 3.0
_MIN_ENTROPY = 1.2
_MAX_WORDS = 200
_MIN_NEW = 50
_MAX_CORPUS = 4000
_REBUILD_EVERY_S = 600.0


def _entropy(counter: Counter) -> float:
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    return -sum((n / total) * math.log2(n / total) for n in counter.values())


def _discover_chinese(texts: list[str]) -> dict[str, float]:
    """中文 n-gram：频次 + 凝聚度 + 邻接熵。返回 {词: 分数}。"""
    char_freq: Counter = Counter()
    ngram_freq: Counter = Counter()
    doc_freq: Counter = Counter()
    left: dict[str, Counter] = {}
    right: dict[str, Counter] = {}

    for text in texts:
        seen: set[str] = set()
        for run in _CJK_RUN.findall(text):
            char_freq.update(run)
            for n in (2, 3, 4):
                for i in range(len(run) - n + 1):
                    gram = run[i:i + n]
                    ngram_freq[gram] += 1
                    if gram not in seen:
                        seen.add(gram)
                        doc_freq[gram] += 1
                    left.setdefault(gram, Counter())[run[i - 1] if i > 0 else "^"] += 1
                    tail = i + n
                    right.setdefault(gram, Counter())[run[tail] if tail < len(run) else "$"] += 1

    total_chars = sum(char_freq.values())
    if not total_chars:
        return {}

    found: dict[str, float] = {}
    n_docs = max(1, len(texts))
    for gram, freq in ngram_freq.items():
        if freq < _MIN_FREQ:
            continue
        if n_docs >= _DF_MIN_DOCS and doc_freq[gram] / n_docs > _MAX_DF:
            continue
        weakest = math.inf
        for i in range(len(gram) - 1):
            pair = gram[i:i + 2]
            p_pair = ngram_freq.get(pair, 0) / max(1, total_chars)
            p_a = char_freq[pair[0]] / total_chars
            p_b = char_freq[pair[1]] / total_chars
            if p_pair <= 0 or p_a <= 0 or p_b <= 0:
                weakest = 0.0
                break
            weakest = min(weakest, math.log2(p_pair / (p_a * p_b)))
        if weakest < _MIN_PMI:
            continue
        if min(_entropy(left[gram]), _entropy(right[gram])) < _MIN_ENTROPY:
            continue
        found[gram] = weakest * math.log2(1 + freq)

    ordered = sorted(found, key=len, reverse=True)
    kept: dict[str, float] = {}
    for word in ordered:
        if any(word != longer and word in longer for longer in kept):
            continue
        kept[word] = found[word]
    return kept


def _discover_latin(texts: list[str]) -> dict[str, float]:
    """英文 / 数字串：整段就是一个词，只按频次筛。"""
    counter: Counter = Counter()
    doc_freq: Counter = Counter()
    spellings: dict[str, Counter] = {}
    for text in texts:
        seen: set[str] = set()
        for match in _LATIN.finditer(text):
            word = match.group(0)
            key = word.lower()
            counter[key] += 1
            spellings.setdefault(key, Counter())[word] += 1
            if key not in seen:
                seen.add(key)
                doc_freq[key] += 1
    found: dict[str, float] = {}
    n_docs = max(1, len(texts))
    for key, freq in counter.items():
        if freq < _MIN_FREQ:
            continue
        if n_docs >= _DF_MIN_DOCS and doc_freq[key] / n_docs > _MAX_DF:
            continue
        shown = spellings[key].most_common(1)[0][0]
        found[shown] = math.log2(1 + freq)
    return found


def discover(texts: list[str]) -> list[str]:
    """从一堆用户消息里提出候选词，按分数从高到低返回。"""
    merged = {**_discover_chinese(texts), **_discover_latin(texts)}
    ranked = sorted(merged.items(), key=lambda kv: -kv[1])[:_MAX_WORDS]
    return [word for word, _ in ranked]


def build(messages: list[str]) -> dict:
    """算一遍，返回要落盘的结构。"""
    return {
        "words": discover(messages),
        "samples": len(messages),
        "built_at": int(time.time()),
    }


def save(payload: dict, path: Path) -> None:
    """先写临时文件再改名：另一个进程随时可能来读，不能让它读到写了一半的内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def vocab_path() -> Path:
    """词表文件：引擎读它，app 写它。"""
    return RESOLVER.resolve(VOCAB_PATH)


def typed_path() -> Path:
    """打字的语料：只有 app 写。"""
    return RESOLVER.resolve(CORPUS_PATH)


def speech_path() -> Path:
    """语音里可信的片段：引擎写完 app 只读。"""
    return RESOLVER.resolve(SPEECH_CORPUS_PATH)


def corpus_paths() -> list[Path]:
    """两份语料合起来算：打字一份、语音一份，各写各的不会抢文件。"""
    return [typed_path(), speech_path()]


def _payload(path: Path) -> dict:
    """读整个文件；文件不在、读不了、内容不是对象，都当空。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(text: str) -> None:
    """记下一条用户自己打的字。

    只收文字输入：语音识别的结果也长得像用户消息，但那是模型听出来的，
    里面就可能已经有错字，拿去学词会越学越歪。这里由调用方保证只传打字的。

    语料只留最近的 _MAX_CORPUS 条：统计要的是"最近怎么说"，
    攒到几十万字也不会更准，只会让每次重算更慢。
    """
    line = (text or "").strip()
    if len(line) < 2:
        return
    target = typed_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": line}, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("[词表] 这条语料没记上", exc_info=True)


def _read_one(path: Path) -> list[str]:
    """读一个语料文件；文件不在算空，坏行跳过不影响其余。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    texts: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("t"), str):
            texts.append(item["t"])
    return texts


def _read_corpus() -> list[str]:
    """两份语料合起来：打字 + 语音里可信的片段。"""
    texts: list[str] = []
    for path in corpus_paths():
        texts.extend(_read_one(path))
    return texts


def _trim_corpus() -> None:
    """把打字语料裁到上限：只留最近的，让重算的成本封顶。

    只裁 app 自己写的那份。语音语料是引擎写的，app 重写它等于和引擎抢文件——
    引擎刚追加的行会被这里的旧快照覆盖掉。
    """
    path = typed_path()
    texts = _read_one(path)
    if len(texts) <= _MAX_CORPUS:
        return
    kept = texts[-_MAX_CORPUS:]
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(
            "".join(json.dumps({"t": t}, ensure_ascii=False) + "\n" for t in kept),
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        logger.debug("[词表] 语料没裁掉", exc_info=True)


def learn() -> dict | None:
    """攒够新语料才重算一遍并落盘。

    返回新算出的内容；语料不够、写不进去都返回 None（下次再来）。
    识别侧读不到词表就当没有，所以这里失败不会影响说话。
    """
    texts = _read_corpus()
    if len(texts) < _MIN_NEW:
        return None
    target = vocab_path()
    previous = _payload(target)
    built_at = previous.get("built_at") or 0
    newest = str(previous.get("newest") or "")
    _trim_corpus()
    texts = _read_corpus()
    fingerprint = f"{len(texts)}\n{texts[-1] if texts else ''}"
    if not texts or fingerprint == newest:
        return None
    if time.time() - float(built_at) < _REBUILD_EVERY_S:
        return None
    payload = build(texts)
    payload["newest"] = fingerprint
    try:
        save(payload, target)
    except OSError:
        return None
    logger.info("[词表] 重算完成：%d 条语料 → %d 个词", len(texts), len(payload["words"]))
    return payload

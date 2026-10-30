"""语音设置：音色、模型档位、开关落盘；API Key 跟其他供应商一样写进引擎的 `.env`。

语音设置只有一处真相：`data/settings/speech.json`。
- 音色 / 档位 / 开关 / 语音形态（纯语音 or 语音互动）都是**界面的选择**，全在这里；
- API Key 是**凭证**，和其他供应商的键放在同一个 `data/NoesiaHome/.env`，不放 json。

以前 `app.json` 里还有一份 `tts` 段当「出厂默认」，于是同一个键两处都有、谁盖谁全看代码，
而且里面一半的键（output_format / sample_rate）根本没人读——已经整段删掉，只留这一处。
首次运行由 `_TEMPLATE` 写全所有键；文件在但缺键就当场报错，不拿代码默认值悄悄兜底。

这里不 import 引擎的 `noesia_cli.config`：那是引擎进程的包（只有 `src/engine` 在它的 sys.path
上），界面进程去 import 会把引擎整套配置加载逻辑拉进来。只按同样的格式读写那一行。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from app import doubao_voices, env_keys
from app.doubao_voices import DOUBAO_VOICES as DOUBAO_LIST
from settings import ConfigError, RESOLVER

PREFS_PATH = "data/settings/speech.json"
# 凭证读写只有一套：app/env_keys.py（这一行只是给提示语用的展示路径）
_ENV_SHOWN = "data/NoesiaHome/.env"
VOICES_URL = "https://api.elevenlabs.io/v1/voices"
# 朗读地址模板：`{voice_id}` 由用户在「设置 → 语音」里选的音色填进去
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class SpeechError(RuntimeError):
    """语音相关失败（网络 / 额度 / 音色 / 资源），消息直接可以给用户看。"""

# 豆包的接口地址和资源 id 都在 `src/tts_catalog.json`：代码里不写具体值，换资源只改数据文件
DOUBAO_URL = doubao_voices.url()
DOUBAO_RESOURCE = doubao_voices.default_resource()  # 默认资源；用户可以在 speech.json 里换成别的
# 采样率跟声卡首选对齐（这台机是 48000）：少一次驱动重采样，播放走原生 48k。
DOUBAO_SAMPLE_RATE = 48000

# 两个引擎：ElevenLabs 按字符计费、音色可自定义；Edge 是微软的在线朗读，免费、音色固定。
PROVIDERS = [
    ("elevenlabs", "ElevenLabs（按字符计费）"),
    ("doubao", "豆包语音 2.0（按字数计费）"),
    ("edge", "Edge 在线朗读（免费）"),
]
# 要填 Key 才能用的引擎（Edge 不用 Key）
KEYED_PROVIDERS = ("elevenlabs", "doubao")
# 豆包音色是常量（不是账号里的清单）：清单在 app/doubao_voices.py，247 个音色逐个实测能出声，
# 每条带 lang/gender，界面按语言分类。第一个是默认音色。
DOUBAO_VOICES: tuple[dict, ...] = DOUBAO_LIST
EDGE_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓 · 女 · 温柔"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊 · 女 · 活泼"},
    {"id": "zh-CN-YunxiNeural", "name": "云希 · 男 · 清朗"},
    {"id": "zh-CN-YunjianNeural", "name": "云健 · 男 · 浑厚"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬 · 男 · 播音"},
    {"id": "zh-CN-YunxiaNeural", "name": "云夏 · 男 · 少年"},
]

# 首次运行写进 speech.json 的完整模板：每个键都显式落地，之后一切以文件为准。
# ElevenLabs 的音色属于账号：把某个人账号里的音色写进代码，别人装上就念不出来。
# 所以这里不给默认音色，空着；用户选了 ElevenLabs，就用他自己的 Key 拉他自己账号的清单再挑一个。
DEFAULT_ELEVENLABS_VOICE = ""
_TEMPLATE: dict = {
    "enabled": True,
    "mode": "plain",  # plain=纯语音朗读；interactive=语音互动（数字人实时说话），互斥
    # 默认 Edge：不要 Key、谁的机器上都能出声。选别家才需要填 Key——语音本来就是可选的
    "provider": "edge",
    "voice_id": "",
    "voice_name": "",
    "model_id": "eleven_v3",
    "max_chars": 400,  # 一条回答最多念这么多字，超了截到句末
    "output_format": "pcm_16000",  # ElevenLabs 直接给 16k 单声道裸 PCM，落 wav 即可
    "sample_rate": 16000,
    "voices": [],  # ElevenLabs 的音色缓存：[{"id": ..., "name": ...}]
    "voice_history": {},  # 每个引擎各自上次选的音色：{"edge": {"id": ..., "name": ...}, ...}
}
# 缺一个都不行：语音设置只有这一份，缺键当场报错，不拿代码默认值顶
_REQUIRED = (
    "enabled", "mode", "provider", "voice_id", "voice_name", "model_id",
    "max_chars", "output_format", "sample_rate",
)


def path() -> Path:
    return RESOLVER.resolve(PREFS_PATH)


def load() -> dict:
    """读 speech.json：没有就按模板写全一份；有但缺键就报错，不静默兜底。"""
    target = path()
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(_TEMPLATE))
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"读不懂 {target}：{exc}") from None
    if not isinstance(stored, dict):
        raise ConfigError(f"{target} 的最外层要是一个对象")
    missing = [key for key in _REQUIRED if key not in stored]
    if missing:
        raise ConfigError(
            f"{target} 缺 {', '.join(missing)}（语音设置只有这一份，缺键不猜）"
        )
    data = dict(_TEMPLATE)
    data.update(stored)
    data["voices"] = list(data.get("voices") or [])
    data["voice_history"] = dict(data.get("voice_history") or {})
    return data


def save(**changes) -> dict:
    """存音色/开关；`api_key=` 会被转去写 `.env`，不落在 json 里。"""
    changes = dict(changes)
    typed_key = changes.pop("api_key", None)
    data = load()
    # 空串不是「改成空」，是「这一项没填」：别让它把已经存好的音色/资源/名字抹掉
    for key in ("model_id", "voice_id", "voice_name"):
        if key in changes and not str(changes[key] or "").strip():
            changes.pop(key)
    if "model_id" in changes and (changes.get("provider") or data.get("provider")) == "doubao":
        known = doubao_voices.resources()
        if str(changes["model_id"]).strip() not in known:
            # 写错的资源 id 当场拦下，并列出能填的：豆包会回 55000000，用户看不出为什么
            raise ConfigError(
                f"豆包的资源 id 不认识：{changes['model_id']}；"
                f"能填的是 {'、'.join(sorted(known))}（换资源时先在控制台开通）"
            )
    data.update({key: value for key, value in changes.items() if key in _TEMPLATE})
    provider = data.get("provider") or "edge"
    if data.get("voice_id"):
        # 记住「这个引擎上次选的音色」，换引擎再换回来才不会掉回默认
        history = dict(data.get("voice_history") or {})
        history[provider] = {"id": data["voice_id"], "name": data.get("voice_name") or ""}
        data["voice_history"] = history
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if typed_key is not None:
        write_api_key(str(typed_key), provider)
    return data


def remember_voice(provider: str, voice_id: str, voice_name: str = "") -> None:
    """记下某家引擎上次选的音色，不改变「现在用的是哪家」。

    用来让每家各自记住自己的选择：换引擎再换回来，不会掉回默认音色。
    """
    if not provider or not voice_id:
        return
    data = load()
    history = dict(data.get("voice_history") or {})
    history[provider] = {"id": voice_id, "name": voice_name or ""}
    data["voice_history"] = history
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _doubao_model_id(value) -> str:
    """豆包的资源 id：认识就用，不认识就回落到默认资源。

    换引擎留下的档位（`eleven_v3`）、手改错的拼写都算不认识——宁可回落到默认，
    也不让一个拼错的资源 id 把整页设置读崩；真出错时合成本身会给人话提示。
    """
    known = doubao_voices.resources()
    text = str(value or "").strip()
    if text in known:
        return text
    return doubao_voices.default_resource()


def current() -> dict:
    """叠加后的生效值：用户选过就用用户的，没有就回落到该引擎的默认。"""
    data = load()
    provider = data["provider"] or "edge"
    if provider == "edge":
        default_voice = EDGE_VOICES[0]["id"]
    elif provider == "doubao":
        default_voice = DOUBAO_VOICES[0]["id"]
    else:
        # ElevenLabs 没有「谁都有的默认音色」：留空，等界面用用户自己的 Key 拉他自己的清单
        default_voice = DEFAULT_ELEVENLABS_VOICE
    # 换引擎时不把对方的音色 id 带过来（Edge 的 zh-CN-…、豆包的 zh_female_… 在 ElevenLabs 上都是非法 id）
    if provider not in ("edge", "doubao") and data["voice_id"] in {
        voice["id"] for voice in list(EDGE_VOICES) + list(DOUBAO_VOICES)
    }:
        data = dict(data, voice_id="")
    return {
        "enabled": bool(data["enabled"]),
        "mode": "interactive" if data.get("mode") == "interactive" else "plain",
        "provider": provider,
        "voice_id": data["voice_id"] or default_voice,
        "voice_name": data["voice_name"] or "默认音色",
        # 豆包的「模型」就是资源 id：用户在 speech.json 里可以换资源，认不出来的值回落默认
        "model_id": (
            _doubao_model_id(data.get("model_id")) if provider == "doubao"
            else (data["model_id"] or _TEMPLATE["model_id"])
        ),
        # 这里永远给 ElevenLabs 的缓存清单：Edge 的音色是常量，界面自己按引擎取（EDGE_VOICES）
        # 合成参数也在这份配置里：纯语音和语音互动读同一套，不另开一处
        "max_chars": int(data["max_chars"]),
        "output_format": data["output_format"],
        "sample_rate": int(data["sample_rate"]),
        "voices": list(data["voices"]),
        "history": dict(data.get("voice_history") or {}),
    }


def elevenlabs_error_message(code: int, detail: str = "") -> str:
    """把 ElevenLabs 的 HTTP 状态码翻成人话：401 最常见，是额度用完而不是 Key 写错。"""
    if code == 401:
        return (
            "ElevenLabs 不认这把 Key（401）：Key 不对，或者这个月的额度已经用完"
            "（免费版 1 万字符，用完也报 401，不会报 429）。去 elevenlabs.io 的订阅页看用量，"
            "或者先换豆包 / Edge 念。"
        )
    if code == 402:
        # 免费账号调公共音色库的 Voice 会拿到 402：说清楚是哪一种，别让人以为是自己填错了
        if "library" in detail.lower():
            return (
                "这个音色在 ElevenLabs 的公共音色库里，免费账号不能用 API 朗读；"
                "换一个预置音色，或用自己账号里的音色（升级套餐后库音色才可用）。"
            )
        return "这个音色要付费账号才能用（402）：换一个账号里自己的音色。"
    if code == 403:
        return "这把 Key 没有这个权限（403）。"
    if code == 429:
        return "请求太频繁被限流了（429），等一分钟再试。"
    if code == 404:
        return (
            "这个音色不在你的 ElevenLabs 账号里（404）：多半是配置里还留着别人的音色 id。"
            "去「设置 → 语音」重新选一个——清单是用你自己的 Key 拉的，选得到就念得出来。"
        )
    if 500 <= code < 600:
        return f"ElevenLabs 服务器出错（{code}），过一会儿再试。"
    return f"ElevenLabs 返回 {code}" + (f"：{detail[:200]}" if detail else "")


def doubao_error_message(code: int, message: str = "") -> str:
    """把豆包的返回码翻成人话：最常见的是资源没开通/音色和资源对不上，报错码看不懂。"""
    text = f"{message or ''}"
    if "55000000" in text or "mismatched" in text.lower():
        return (
            "这个音色和资源对不上（55000000）：两种情况——①你的火山引擎账号没开通"
            f"「{doubao_voices.default_resource()}」这个语音合成资源；②音色属于别的资源。"
            "先去控制台开通，再回「设置 → 语音」确认豆包这档的资源 id 和音色都对得上。"
        )
    if code in (401, 403):
        return (
            f"豆包不认这把 Key（{code}）：检查 DOUBAO_API_KEY 有没有填错、"
            "以及这个账号有没有开通语音合成资源。"
        )
    if code == 429:
        return "请求太频繁被限流了（429），等一分钟再试。"
    if 500 <= code < 600:
        return f"豆包服务器出错（{code}），过一会儿再试。"
    detail = f"：{message[:200]}" if message else ""
    return f"豆包返回 {code}{detail}"


def fetch_voices(api_key: str = "") -> list[dict]:
    """从 ElevenLabs 拉音色清单（不产生字符消耗），并写进缓存。

    `api_key` 给「刚在界面上填了 Key、还没保存」的情况：那时 `.env` 里还没有它，
    只能用输入框里这一串去拉，否则首次安装永远选不到音色。
    """
    key = (api_key or "").strip() or _api_key("elevenlabs")
    request = urllib.request.Request(VOICES_URL, headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(elevenlabs_error_message(exc.code)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连不上 ElevenLabs：{exc.reason}") from exc

    voices = []
    for item in payload.get("voices") or []:
        voice_id = str(item.get("voice_id") or "")
        if not voice_id:
            continue
        labels = item.get("labels") or {}
        voices.append({
            "id": voice_id,
            "name": str(item.get("name") or "未命名"),
            # 音色的归属：premade=所有账号都有；professional/library=要付费套餐才给 API 念；
            # generated/cloned=账号自己的（别人没有）。界面靠它标注「需付费套餐」「我的音色」，
            # 也是「默认音色只能挑 premade」的依据。
            "category": str(item.get("category") or ""),
            # 只留界面上要显示的三个：语言/性别/口音（账户里 21 个是英文预置，靠这个才分得清）
            "labels": {key: str(labels.get(key) or "")
                       for key in ("language", "gender", "accent") if labels.get(key)},
        })
    save(voices=voices)
    return voices


def usable_default_voice(voices: list[dict]) -> str:
    """账号清单里挑一个「现在就能念」的：优先 premade（免费套餐也能用），没有就用第一个。

    这是给「选了 ElevenLabs 但音色还是空」兜底用的：值来自用户自己的账号，代码里不留任何音色 id。
    """
    if not voices:
        return ""
    free = [item for item in voices if str(item.get("category") or "") == "premade"]
    return str((free or voices)[0].get("id") or "")


# ---------- API Key：NoesiaHome/.env ----------


def env_file() -> Path:
    """`.env` 的位置由 `env_keys` 定，这里只是转一下手（界面提示语要用）。"""
    return env_keys.env_file()


def env_key_name(provider: str = "") -> str:
    """这个引擎的 Key 在 `.env` 里叫什么名字：只有 `env_keys.PROVIDERS` 一张表说了算。"""
    return env_keys.env_key(provider or load()["provider"])


def read_api_key(provider: str = "") -> str:
    return env_keys.read_provider(provider or load()["provider"])


def write_api_key(raw: str, provider: str = "") -> bool:
    """更新 `.env` 里那一行（没有就追加）。返回是否真的写了。

    Edge 这类没有 Key 的引擎：没有地方可写，直接返回 False，不抛异常
    （界面上哪怕把 Key 跟 Edge 一起提交，也不能把保存整条流程带崩）。
    """
    key = clean_api_key(raw)
    if not key:
        return False
    provider = provider or load()["provider"]
    if provider not in KEYED_PROVIDERS:
        return False
    env_keys.write_provider(provider, key)
    return True


def clean_api_key(raw: str) -> str:
    """粘进来的键去掉空白；含中文的直接丢（那是把提示语当键贴了）。"""
    key = "".join(raw.split())
    return key if key and key.isascii() else ""


def key_status(provider: str = "") -> tuple[bool, str]:
    """(有没有键, 来源说明)。不暴露键本身。"""
    if (provider or load()["provider"]) not in KEYED_PROVIDERS:
        return True, "这个引擎不用 Key（Edge 直接出声音）"
    name = env_key_name(provider)
    if read_api_key(provider):
        env_path = env_file()
        # 全新安装时 .env 还不存在：直接 read_text 会抛 FileNotFoundError，把「设置 → 语音」整页带崩
        if env_path.is_file() and name in env_path.read_text(encoding="utf-8", errors="replace"):
            return True, f"已保存（{_ENV_SHOWN} 里的 {name}）"
        return True, f"来自环境变量 {name}"
    return False, f"还没填：粘贴 {name} 的值"


def missing_key_message(provider: str = "") -> str:
    """缺 Key 时给用户看的那句话（界面和语音条共用一份，别两处写不一样的）。"""
    if (provider or load()["provider"]) == "doubao":
        return (
            "缺少 API Key：在「设置 → 语音」里填豆包的 Key（DOUBAO_API_KEY）；"
            "也可以改选免费的 Edge TTS（不用 Key），或者直接关掉语音开关。"
        )
    return "缺少 API Key：在「设置 → 语音」里填 ElevenLabs 的 Key；也可以改选免费的 Edge TTS（不用 Key），或者直接关掉语音开关。"


def _api_key(provider: str = "") -> str:
    key = read_api_key(provider)
    if not key:
        raise RuntimeError(missing_key_message(provider))
    if not key.isascii() or any(ch.isspace() for ch in key):
        # 直接把错误提示之类的东西粘进来了：这种值发出去只会在编码那步炸得莫名其妙
        raise RuntimeError("API Key 里混了中文或空格，重新粘贴一次（只要 sk_ 开头那一串）")
    return key

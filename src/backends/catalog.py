"""模型菜单：真源在引擎配置（`model:` 段），候选表由引擎的供应商/模型表给出。

这一层只做两件事：把引擎的数据整理成界面能直接用的菜单项；把用户的选择交给引擎自己的切换流程
（`noesia_cli.model_switch.switch_model` → `persist_model_selection`），由它解析别名、路由、凭据
并把结果写回引擎配置。我们不再自己判断 base_url、密钥环境变量或模型是否合法。

选项 id 仍是 `<provider>:<model>`；选完由 `AgentHost` 丢掉旧实例，下一轮按引擎配置重建。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .base import LLMConfigError

logger = logging.getLogger(__name__)




@dataclass(frozen=True)
class LlmOption:
    """清单的一项。available=False 时 reason 说明为什么不能选。"""

    id: str
    label: str
    backend: str  # 引擎的 provider id；字段名沿用界面的 backend，改它要同时改前端
    model: str
    available: bool = True
    reason: str = ""

def option_id(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def split_option(option: str) -> tuple[str, str]:
    """`deepseek:deepseek-flash` → (deepseek, deepseek-flash)；模型名里的冒号保留。"""
    provider, sep, model = option.partition(":")
    if not sep or not provider or not model:
        raise LLMConfigError(f"模型选项格式不对：{option!r}（应为 provider:model 形式）")
    return provider, model


# ---- 引擎侧读取 ----


def engine_core(cfg, root: Path | str | None = None) -> Path:
    """引擎代码目录：`engine.core_path`（相对路径按项目根解析，不按进程 cwd）。

    cwd 是不可靠的——装成包之后用户在任意目录启动，按 cwd 找 `src/engine` 必然找不到。
    """
    core = Path(cfg.engine.core_path)
    if core.is_absolute() or root is None:
        return core.resolve()
    return (Path(root) / core).resolve()


def pin_engine_home(cfg, root: Path | str | None) -> Path:
    """把 `NOESIA_HOME` 钉在项目内的引擎家目录上，返回它。

    引擎按 `NOESIA_HOME` 找自己的配置；不钉的话，用户机器上若另有 `~/.noesia`（跑过引擎 CLI），
    我们会读到那份配置——这个程序就变成了「跟着别的程序走」。相对路径按项目根解析。
    """
    home = Path(cfg.engine.home)
    if not home.is_absolute():
        if root is None:
            home = home.resolve()
        else:
            home = (Path(root) / home).resolve()
    else:
        home = home.resolve()
    os.environ["NOESIA_HOME"] = str(home)
    # 记住引擎在哪：「设置模型」这类零散查询可能比第一次列菜单更早发生
    global _ENGINE_CORE, _PROJECT_ROOT
    _ENGINE_CORE = engine_core(cfg, root)
    _PROJECT_ROOT = Path(root).resolve() if root is not None else None
    return home


_ENGINE_CORE: Path | None = None


def ensure_pinned() -> None:
    """还没 pin 过就按项目自己的配置 pin 一次。

    「设置模型…」可能比第一次列菜单更早发生（引擎还没连上、清单还没回来），那时没人调用过
    `pin_engine_home()`，`_ENGINE_CORE` 是空的 → `import noesia_cli` 直接
    `No module named 'noesia_cli'`。GUI 启动时会 pin，这里兜住其它入口。
    """
    if _ENGINE_CORE is not None:
        return
    try:
        from settings import ROOT, SETTINGS

        root = ROOT
        cfg = SETTINGS
    except Exception as exc:  # noqa: BLE001
        raise LLMConfigError(f"找不到引擎代码目录：{exc}") from exc
    pin_engine_home(cfg, root)


def _ensure_core() -> None:
    """把用过的引擎目录重新放回 sys.path。

    菜单是先调 `list_options()` 才认识引擎的；「设置模型」这类零散查询（要变量名、要 .env）
    可能更早发生，没有这一步就会静默拿不到供应商表。
    """
    if _ENGINE_CORE is None:
        ensure_pinned()
    if _ENGINE_CORE is not None and str(_ENGINE_CORE) not in sys.path:
        sys.path.insert(0, str(_ENGINE_CORE))


def _engine(core_path: str | Path):
    """按引擎代码目录导入引擎，返回它的模型模块（供应商表、候选模型、凭据状态都在里面）。

    引擎就在仓库里（`src/engine`），以 sys.path 方式引用，不装包。
    """
    global _ENGINE_CORE
    core = str(Path(core_path).resolve())
    _ENGINE_CORE = Path(core)
    if core not in sys.path:
        sys.path.insert(0, core)
    from noesia_cli import models as engine_models  # noqa: PLC0415 - 依赖引擎路径

    return engine_models


def _engine_env() -> dict[str, str]:
    """引擎自己那份 `.env` 的内容（`<home>/.env`，由引擎的 load_env 解析）。"""
    _ensure_core()
    try:
        from noesia_cli.config import load_env  # noqa: PLC0415 - 依赖引擎路径

        return {str(k): str(v) for k, v in (load_env() or {}).items()}
    except Exception:  # noqa: BLE001 - 引擎还没就绪就当没有 .env
        return {}


def _missing_reason(provider: str, label: str, home_hint: str = "data/NoesiaHome") -> str:
    """没凭据时给一句人话：说清该设哪个变量、写在哪个文件，不猜。"""
    try:
        import providers as engine_providers  # noqa: PLC0415 - 依赖引擎路径

        for row in engine_providers.list_providers():
            if str(getattr(row, "name", "")) == provider:
                envs = [str(v) for v in (getattr(row, "env_vars", ()) or ()) if v]
                if envs:
                    return (
                        f"{label} 还没配置凭据：设 {' 或 '.join(envs)}，"
                        f"或写进引擎的 {home_hint}/.env"
                    )
                break
    except Exception:  # noqa: BLE001 - 拿不到细节也别把菜单弄崩
        pass
    return f"{label} 还没配置凭据"


def _usable(provider: str, engine_flag: bool) -> bool:
    """能不能真的发请求：api_key 类供应商要求对应的环境变量真的有值。

    引擎的 `authenticated` 对 api_key 类供应商只要配置里写了 provider 就算 configured，
    实测一个密钥都没有时它也返回 True——菜单照着标「可用」就是骗人。这类按变量名自己核一遍
    （引擎发请求时认的也是这些变量）；OAuth / 本机 CLI 类（copilot、ollama）交给引擎判断。
    """
    names: list[str] = []
    try:
        import providers as engine_providers  # noqa: PLC0415 - 依赖引擎路径

        for row in engine_providers.list_providers():
            if str(getattr(row, "name", "")) == provider:
                names = [str(v) for v in (getattr(row, "env_vars", ()) or ()) if v]
                break
    except Exception:  # noqa: BLE001
        return engine_flag
    if not names:
        return engine_flag
    try:
        from noesia_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415

        pconfig = PROVIDER_REGISTRY.get(provider)
        if pconfig is not None and str(getattr(pconfig, "auth_type", "")) != "api_key":
            return engine_flag
    except Exception:  # noqa: BLE001
        pass
    # 密钥的真源是引擎：进程环境变量，或引擎自己读的 `<home>/.env`（我们不另造一套读法）
    from_file = _engine_env()
    return any(str(os.environ.get(n) or from_file.get(n) or "").strip() for n in names)


_MODEL_IDS_CACHE: dict[tuple[str, int], list[str]] = {}


def _model_ids(models, provider: str, limit: int) -> list[str]:
    """带缓存的版本：引擎的静态模型表在进程内不会变，四十多家每家问一次要好几秒。"""
    key = (provider, limit)
    cached = _MODEL_IDS_CACHE.get(key)
    if cached is not None:
        return list(cached)
    ids = _read_model_ids(models, provider, limit)
    _MODEL_IDS_CACHE[key] = list(ids)
    return ids


def _read_model_ids(models, provider: str, limit: int) -> list[str]:
    """供应商的模型清单（引擎的静态表；拿不到就退回它的默认模型）。"""
    try:
        rows = models.curated_models_for_provider(provider) or []
    except Exception as exc:  # noqa: BLE001 - 某个供应商的表坏了不该拖垮整个菜单
        logger.warning("读不到 %s 的模型清单：%s", provider, exc)
        rows = []
    ids: list[str] = []
    for row in rows:
        mid = str(row[0] if isinstance(row, (tuple, list)) else row).strip()
        if mid and mid not in ids:
            ids.append(mid)
    if not ids:
        try:
            default = str(models.get_default_model_for_provider(provider) or "").strip()
        except Exception:  # noqa: BLE001
            default = ""
        if default:
            ids = [default]
    return ids[:limit] if limit else ids


# ---- 对外 ----


# 还没选过模型时的占位项：菜单里就要说人话，不能让界面显示一个空的 `auto:`
UNSET = LlmOption(
    "",
    "还没配置模型",
    "",
    "",
    available=False,
    reason="点聊天窗的「设置模型」选一个；密钥写在引擎的 data/NoesiaHome/.env（如 DEEPSEEK_API_KEY）",
)


def provider_env_vars(provider: str) -> tuple[str, ...]:
    """这家供应商要填哪个密钥变量（引擎的表说了算；拿不到就是空）。"""
    return tuple(sorted(_provider_envs(provider)))


def provider_configured(provider: str) -> bool:
    """引擎现在手里有没有这家的凭据：环境变量或引擎那份 `.env` 里有非空值就算有。"""
    return bool(provider_env_vars(provider)) and _usable(provider, True)


def provider_models(provider: str, *, limit: int = 0) -> list[str]:
    """某家供应商的模型清单（引擎静态表；拿不到退回它的默认模型）。对话框选中那家时才取。"""
    _ensure_core()
    try:
        from noesia_cli import models as engine_models  # noqa: PLC0415 - 依赖引擎路径
    except Exception as exc:  # noqa: BLE001
        raise LLMConfigError(f"读不到引擎的模型表：{exc}") from exc
    return _model_ids(engine_models, provider, limit)


def _fillable_here(provider: str, row: dict) -> bool:
    """这家能不能在这个对话框里配：要有可填的变量名，且不是 OAuth / 本机 CLI 那套。

    `copilot-acp`、`openai-codex`、`bedrock`、`vertex` 这些靠引擎自己的登录态或云凭据，
    没有「粘贴一个 key」这回事；列在这里用户只会试了再失望。它们该走引擎的登录流程。
    """
    if not row.get("env_vars"):
        return False
    try:
        from noesia_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415 - 依赖引擎路径

        entry = PROVIDER_REGISTRY.get(provider)
    except Exception:  # noqa: BLE001 - 查不到就当普通 api_key 家，别把能配的挡掉
        return True
    if entry is None:
        return True
    return str(getattr(entry, "auth_type", "") or "") in ("", "api_key", "?")


def provider_choices(*, with_models: bool = False, limit_per_provider: int = 0) -> list[dict]:
    """「设置模型」对话框的供应商清单：引擎支持的**每一家**，不论现在有没有凭据。

    和聊天窗那个下拉框不同——下拉框只列「能用的 + 文档里写了怎么配的」少数几家，否则 48 家会
    把菜单灌满；对话框恰恰是给「还没配」的供应商填密钥的地方，`minimax`、`xiaomi`、`stepfun`
    这些必须都能选到。每家的模型来自引擎的静态表，拿不到就退回它自己的默认模型。
    """
    _ensure_core()
    try:
        from noesia_cli import models as engine_models  # noqa: PLC0415 - 依赖引擎路径
    except Exception as exc:  # noqa: BLE001
        raise LLMConfigError(f"读不到引擎的模型表：{exc}") from exc

    choices: list[dict] = []
    for provider, row in _provider_rows(engine_models).items():
        if not _fillable_here(provider, row):
            continue
        env_vars = list(row.get("env_vars") or [])
        configured = _usable(provider, True) if env_vars else bool(row.get("authenticated"))
        choices.append(
            {
                "provider": provider,
                "label": str(row.get("label") or provider),
                "env_vars": env_vars,
                "configured": configured,
                "models": _model_ids(engine_models, provider, limit_per_provider) if with_models else [],
            }
        )
    # 排序：配好的 → 我们文档里写了怎么配的（`.env.example`，用户能照着填）→ 其余按名字。
    # 「国内主流」这类名单是文件内容决定的，代码里不写死任何一家公司名。
    documented = _documented_env_vars(_PROJECT_ROOT)
    # 排序只看两件便宜的事：有没有配好、是不是文档里写明怎么配的那几家。不为了排序去算四十多家的
    # 模型表——实测要十秒，而排序结果在「配过的那家」面前本来就不重要。
    choices.sort(
        key=lambda item: (
            not item["configured"],
            not (set(item["env_vars"]) & documented),
            item["provider"],
        )
    )
    return choices


def _documented_env_vars(root: Path | str | None) -> set[str]:
    """`.env.example` 里列到的变量名：这些供应商是「我们文档里写了怎么配」的那几家。

    菜单里没密钥的供应商只留这些（各一行灰项），否则引擎的 53 家供应商会把下拉框灌成几百项。
    """
    path = Path(root or ".") / ".env.example"
    names: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key and key.replace("_", "").isalnum():
                names.add(key)
    except OSError:
        pass
    return names


def _provider_envs(provider: str) -> set[str]:
    """这个供应商认的密钥变量名（引擎的表；拿不到就空集）。"""
    _ensure_core()
    try:
        import providers as engine_providers  # noqa: PLC0415 - 依赖引擎路径

        for row in engine_providers.list_providers():
            if str(getattr(row, "name", "")) == provider:
                return {str(v) for v in (getattr(row, "env_vars", ()) or ()) if v}
    except Exception:  # noqa: BLE001
        pass
    return set()


_PROJECT_ROOT: Path | None = None
_PROVIDER_ROWS: dict[str, dict] | None = None
_PROVIDER_ROWS_AT = 0.0
# 供应商表的保鲜时间：引擎问一次凭据池要好几秒（它按「每家 × 每个模型」重建），所以缓存住；
# 保存密钥、切换模型这类会改变结论的动作要主动 invalidate_providers()。
PROVIDER_ROWS_TTL = 60.0


def invalidate_providers() -> None:
    """丢掉缓存的供应商表：刚存了密钥/切了模型，下次问必须重新算。"""
    global _PROVIDER_ROWS, _PROVIDER_ROWS_AT
    _PROVIDER_ROWS = None
    _PROVIDER_ROWS_AT = 0.0
    _MODEL_IDS_CACHE.clear()


def _engine_authenticated(models, provider: str) -> bool:
    """只有「不靠环境变量」的供应商才去问引擎的登录态（OAuth / 本机 CLI）。

    api_key 类由 `_usable()` 直接看变量有没有值——快、准；引擎那种问法要重建整个凭据池，
    53 家 × 12 个模型就是三十多秒，界面等不起。
    """
    try:
        from noesia_cli import auth as engine_auth  # noqa: PLC0415 - 依赖引擎路径

        status = engine_auth.get_auth_status(provider) or {}
        return status.get("authenticated") is True or status.get("configured") is True
    except Exception:  # noqa: BLE001 - 引擎读不出登录态就当没配，别把灰的说成能用
        return False


def _provider_rows(models, *, refresh: bool = False) -> dict[str, dict]:
    """引擎的供应商表：{provider: {id, label, authenticated}}。"""
    global _PROVIDER_ROWS, _PROVIDER_ROWS_AT
    if _PROVIDER_ROWS is not None and not refresh and time.monotonic() - _PROVIDER_ROWS_AT < PROVIDER_ROWS_TTL:
        return _PROVIDER_ROWS
    _ensure_core()
    rows: dict[str, dict] = {}
    try:
        import providers as engine_providers  # noqa: PLC0415 - 依赖引擎路径

        for item in engine_providers.list_providers() or []:
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            env_vars = tuple(str(v) for v in (getattr(item, "env_vars", ()) or ()) if v)
            authenticated = _usable(name, True) if env_vars else _engine_authenticated(models, name)
            rows[name] = {
                "id": name,
                "label": str(getattr(item, "label", "") or name),
                "env_vars": env_vars,
                "authenticated": authenticated,
            }
    except Exception as exc:  # noqa: BLE001
        raise LLMConfigError(f"读不到引擎的供应商表：{exc}") from exc
    _PROVIDER_ROWS, _PROVIDER_ROWS_AT = rows, time.monotonic()
    return rows


def _provider_state(models, provider: str, home_hint: str = "data/NoesiaHome") -> tuple[bool, str]:
    """这个供应商现在能不能用：(可用?, 不可用时给人看的理由)。

    引擎表里没有的 provider（自定义模型名）一律当可用——那种情况由引擎在切换/请求时报错，
    我们这里不替它下结论。
    """
    row = _provider_rows(models).get(provider)
    if row is None:
        return True, ""
    label = str(row.get("label") or provider).strip()
    available = _usable(provider, bool(row.get("authenticated")))
    return available, "" if available else _missing_reason(provider, label, home_hint)


def current_option(cfg, *, root: Path | str | None = None) -> LlmOption:
    """引擎配置里现在选的是哪个（`engine.home/config.yaml` 的 `model:` 段是唯一真源）。

    配置里没有 `model:` 时返回占位项（`available=False`）：这时引擎根本不知道该用哪个模型，
    界面必须说「还没配置模型」，而不是拿一个空模型名假装可用。配置里有模型但没密钥时，
    照样返回它，只是 `available=False` 并带上缺哪个变量的说明——「当前」和「能用」是两件事。
    """
    pin_engine_home(cfg, root)
    models = _engine(engine_core(cfg, root))
    from agent.agent_factory import _config_model_target  # noqa: PLC0415 - 依赖引擎路径

    # 只认配置里写下的模型名。引擎在没有写下时会给一个静默默认（z-ai/glm-5.2），那是它自己的
    # 兜底，不是用户选过的模型——把它当「当前」就会凭空出现一个 deepseek:z-ai/glm-5.2 选项。
    model, provider = _config_model_target()
    if not model:
        return UNSET
    provider = provider or "auto"
    available, reason = _provider_state(models, provider)
    return LlmOption(
        option_id(provider, model),
        f"{provider} · {model}",
        provider,
        model,
        available=available,
        reason=reason,
    )


def list_options(
    cfg,
    *,
    root: Path | str | None = None,
    limit_per_provider: int = 0,
) -> list[LlmOption]:
    """列出全部可选的模型，`provider · model` 一行一个，**不截断、不藏供应商**。

    界面把这份清单收成两级菜单（一级供应商、二级模型），所以这里可以给全：截断到每家的前十
    几个会让人以为它只有那几个模型；同一条只出现一次，当前项永远排最前。

    没凭据的供应商也列（灰的、带缺哪个变量的说明）——「看得见但点不动」比「悄悄消失」清楚。
    能填密钥的供应商以引擎表为准（`provider_choices` 的同一套判断），OAuth / 云凭据那几家不在
    这里出现：它们没有「填一个 key」这回事，列出来只会误导。
    """
    pin_engine_home(cfg, root)
    models = _engine(engine_core(cfg, root))
    current = current_option(cfg, root=root)
    using_provider = current.backend if current.model else ""
    # 第一项永远是「当前」：没配过就是那句占位（免得菜单里显示成某个其实没用上的供应商）
    options: list[LlmOption] = [current]
    seen: set[str] = {option.id for option in options if option.id}
    home_hint = str(getattr(cfg.engine, "home", "data/NoesiaHome"))
    documented = _documented_env_vars(root)

    rows = []
    for provider, row in _provider_rows(models).items():
        if provider != using_provider and not _fillable_here(provider, row):
            continue
        available, reason = _provider_state(models, provider, home_hint)
        rows.append((provider, row, available, reason))
    # 顺序：能用的 → 文档里写了怎么配的 → 其余按名字。菜单一级就是这个顺序，用户第一眼看到的是
    # 自己配过的那家，其次是照着 `.env.example` 能填的那几家。
    rows.sort(
        key=lambda item: (
            item[0] != using_provider,
            not item[2],
            not (_provider_envs(item[0]) & documented),
            item[0],
        )
    )

    for provider, row, available, reason in rows:
        label = str(row.get("label") or provider).strip()
        ids = _model_ids(models, provider, limit_per_provider)
        if provider == using_provider and current.model and current.model not in ids:
            ids.insert(0, current.model)  # 当前这个哪怕不在引擎的候选表里也得能看见
        for mid in ids:
            oid = option_id(provider, mid)
            if oid in seen:  # 当前项已经占了这一格，别再排一遍
                continue
            seen.add(oid)
            options.append(
                LlmOption(
                    oid,
                    f"{label} · {mid}",
                    provider,
                    mid,
                    available=available,
                    reason=reason,
                )
            )
    return options


def apply_option(cfg, option: str, *, root: Path | str | None = None) -> tuple[str, str]:
    """把选择交给引擎：它切换、校验、写回引擎配置，返回 (provider, model)。"""
    pin_engine_home(cfg, root)
    _engine(engine_core(cfg, root))
    from noesia_cli.config import get_config_path, read_user_config_raw  # noqa: PLC0415
    from noesia_cli.model_switch import persist_model_selection, switch_model  # noqa: PLC0415

    provider, model = split_option(option)
    config = read_user_config_raw(get_config_path())
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        current_model = str(model_cfg.get("default") or "").strip()
        current_provider = str(model_cfg.get("provider") or "").strip()
    else:
        current_model = str(model_cfg or "").strip()
        current_provider = ""
    result = switch_model(
        model,
        current_provider=current_provider,
        current_model=current_model,
        explicit_provider=provider,
        user_providers=config.get("providers"),
        custom_providers=config.get("custom_providers"),
    )
    if not result.success:
        raise LLMConfigError(result.error_message or f"引擎不接受 {option}")
    persist_model_selection(result)
    return result.target_provider or provider, result.new_model or model

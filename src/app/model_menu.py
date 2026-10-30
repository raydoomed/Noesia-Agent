"""模型菜单：一级只有供应商（分类），点开才是那家的全部模型。

以前的下拉框是一张扁平长列表（`provider · model` 一行一个），引擎支持的供应商一多就没法看：
找不到自己的那一组，也看不出哪些是「同一家」。这里把清单收成两级：

    模型 ▸ DeepSeek · 2 个模型 ▸ deepseek-flash / deepseek-v4-pro
           xiaomi · 8 个模型  ▸ mimo-v2.6-pro / …

一级只放供应商（带「几个模型 / 还没配密钥」），二级才是模型名。没凭据的那家照样能点开看它
有哪些模型，只是每一项是灰的、鼠标停上去告诉你缺哪个变量——比整组消失清楚。
"""

from __future__ import annotations

from PySide6 import QtWidgets


def _as_dict(item) -> dict:
    """清单项既可能是 `LlmOption`（后端 dataclass）也可能是 dict（IPC 解出来的），统一成 dict。"""
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump()
    return {
        "id": getattr(item, "id", ""),
        "label": getattr(item, "label", ""),
        "backend": getattr(item, "backend", ""),
        "model": getattr(item, "model", ""),
        "available": getattr(item, "available", False),  # 没说能用就当不能用
        "reason": getattr(item, "reason", ""),
    }


def group_models(items) -> list[dict]:
    """按供应商分组：`[{provider, label, available, items: [...]}, ...]`，顺序照原清单。

    空 `id` 的占位项（还没配置模型）单独放最后，不混进任何一家。
    """
    groups: list[dict] = []
    index: dict[str, int] = {}
    placeholder: list[dict] = []

    for raw in items or []:
        row = _as_dict(raw)
        option_id = str(row.get("id") or "")
        provider = str(row.get("backend") or "").strip()
        if not option_id and not provider:
            placeholder.append(row)  # 既没 id 也没供应商：真正的「还没配置模型」占位
            continue
        label = str(row.get("label") or provider)
        if provider not in index:
            index[provider] = len(groups)
            groups.append(
                {
                    "provider": provider,
                    "label": label.split(" · ")[0] or provider,
                    "available": False,
                    "items": [],
                }
            )
        group = groups[index[provider]]
        group["items"].append(row)
        group["available"] = group["available"] or bool(row.get("available", False))

    groups.sort(key=lambda group: not group["available"])  # 能用的那几组排前面
    return groups + ([{"provider": "", "label": "", "available": False, "items": placeholder}] if placeholder else [])


def fill_menu(menu: QtWidgets.QMenu, items, current: str, on_pick) -> None:
    """把分组后的清单填成两级菜单：一级供应商，二级模型。`on_pick(option_id)` 由调用方决定做什么。"""
    menu.clear()
    groups = group_models(items)
    if not any(group["provider"] for group in groups):
        menu.addAction("还没有模型").setEnabled(False)  # 空清单时说一句话，别弹出个空菜单
        return
    for group in groups:
        if not group["provider"]:
            continue  # 有真供应商时不再啰嗦「还没配置模型」，按钮上已经写着

        count = len(group["items"])
        title = f"{group['label']} · {count} 个模型" if group["available"] else f"{group['label']} · 还没配置"
        sub = menu.addMenu(title)
        # 子菜单是独立的弹出窗口，不会自动带上父菜单的 objectName：不写这一行，它就退回
        # 系统默认的白底，而菜单项文字是样式表里的浅色——白底白字，模型名直接看不见。
        sub.setObjectName("modelmenu")
        sub.setToolTipsVisible(True)
        for row in group["items"]:
            model = str(row.get("model") or row.get("label") or "")
            action = sub.addAction(model or str(row.get("id")))
            if not str(row.get("id") or ""):
                # 这家的模型名要配了密钥才问得到：只留一句「配了就出现」，点了也没事干
                action.setText("配好密钥后这里会出现模型")
                action.setEnabled(False)
                action.setToolTip(str(row.get("reason") or ""))
                continue
            action.setCheckable(True)
            action.setChecked(str(row.get("id")) == current)
            action.setEnabled(bool(row.get("available", True)))
            action.setToolTip(str(row.get("reason") or row.get("id") or ""))
            # 灰不灰交给样式表（`QMenu::item:disabled`）：QAction 没有 setForeground，
            # 菜单项的颜色只能由样式控制。
            action.triggered.connect(lambda _checked=False, option_id=str(row.get("id")): on_pick(option_id))


def current_label(items, current: str, *, fallback: str = "选模型") -> str:
    """按钮上显示什么：当前模型的名字（不带供应商前缀，一级菜单里已经有分类了）。"""
    for raw in items or []:
        row = _as_dict(raw)
        if str(row.get("id") or "") == current:
            return str(row.get("model") or row.get("label") or current)
    return fallback

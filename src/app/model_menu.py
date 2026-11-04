"""模型菜单：一级只有供应商（分类），点开才是那家的全部模型。

以前的下拉框是一张扁平长列表（`provider · model` 一行一个），引擎支持的供应商一多就没法看：
找不到自己的那一组，也看不出哪些是「同一家」。这里把清单收成两级：

    模型 ▸ DeepSeek · 2 个模型 ▸ deepseek-flash / deepseek-v4-pro
           xiaomi · 8 个模型  ▸ mimo-v2.6-pro / …

一级只放供应商（带「几个模型 / 还没配密钥」），二级才是模型名。没凭据的那家照样能点开看它
有哪些模型，只是每一项是灰的、鼠标停上去告诉你缺哪个变量——比整组消失清楚。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


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


def fill_menu(
    menu: QtWidgets.QMenu,
    items,
    current: str,
    on_pick,
    *,
    on_refresh=None,
    refreshing: bool = False,
) -> QtWidgets.QPushButton | None:
    """把分组后的清单填成两级菜单：一级供应商，二级模型。`on_pick(option_id)` 由调用方决定做什么。

    `on_refresh` 给了就在最后加一条「刷新模型」：上游发了新模型、下架了旧模型，菜单不必等着
    重开程序才更新。刷新途中这条变灰写「刷新中…」，免得连点几下发好几次请求。
    """
    menu.clear()
    groups = group_models(items)
    if not any(group["provider"] for group in groups):
        # 空清单时别弹出个空菜单。措辞要说"没取到"而不是"还没有"：拿不到 live 目录时
        # 清单就是空的（不再拿仓库内静态表充数），用户该做的是点下面那条刷新。
        menu.addAction("模型清单没取到，点下面的刷新重试").setEnabled(False)
        if on_refresh is not None:
            return _add_refresh_action(menu, on_refresh, refreshing)
        return None
    for group in groups:
        if not group["provider"]:
            continue  # 有真供应商时不再啰嗦「还没配置模型」，按钮上已经写着

        count = len(group["items"])
        title = f"{group['label']} · {count} 个模型" if group["available"] else f"{group['label']} · 还没配置"
        sub = menu.addMenu(title)
        # 标题里的「几个模型」会随清单变，重建后没法靠它找回原来是哪一家：把不含数量的
        # 供应商名挂在 action 上（`data()`），重画时按它认。
        sub.menuAction().setData(group["label"])
        # 子菜单是独立的弹出窗口，不会自动带上父菜单的 objectName：不写这一行，它就退回
        # 系统默认的白底，而菜单项文字是样式表里的浅色——白底白字，模型名直接看不见。
        sub.setObjectName("modelmenu")
        sub.setToolTipsVisible(True)
        # 勾选必须互斥：QAction 自己不带互斥，点第二条时第一条不会松手，菜单里就会出现两个勾
        # （而且只有引擎回了清单才重画，中间那段时间一直两个勾）。给每个子菜单一个排他组。
        picks = QtGui.QActionGroup(sub)
        picks.setExclusive(True)
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
            picks.addAction(action)
            action.setChecked(str(row.get("id")) == current)
            action.setEnabled(bool(row.get("available", True)))
            action.setToolTip(str(row.get("reason") or row.get("id") or ""))
            # 灰不灰交给样式表（`QMenu::item:disabled`）：QAction 没有 setForeground，
            # 菜单项的颜色只能由样式控制。
            action.triggered.connect(lambda _checked=False, option_id=str(row.get("id")): on_pick(option_id))

    if on_refresh is not None:
        return _add_refresh_action(menu, on_refresh, refreshing)
    return None


_REFRESH_QSS = """
QPushButton {
    background: transparent; border: none; text-align: left;
    padding: 5px 22px 5px 10px; color: #e8e8ef; font-size: 12.5px;
}
QPushButton:hover { background-color: rgba(154, 208, 255, 60); }
QPushButton[busy="true"] { color: #6b6b76; }
QPushButton[busy="true"]:hover { background: transparent; }
"""


def _add_refresh_action(
    menu: QtWidgets.QMenu, on_refresh, refreshing: bool
) -> QtWidgets.QPushButton:
    """菜单最后那条「刷新模型」：刷新途中是灰的、写「刷新中…」。

    这一条不用 QAction 而用 QWidgetAction + 按钮：QAction 一击中 Qt 就把整个菜单关掉，
    而刷新要让人看着它变「刷新中…」、清单回来再原地换成新的——菜单不能走。
    """
    menu.addSeparator()
    button = QtWidgets.QPushButton("刷新中…" if refreshing else "刷新模型", menu)
    button.setStyleSheet(_REFRESH_QSS)
    button.setFlat(True)
    button.setCursor(QtCore.Qt.PointingHandCursor)
    # 不用 setEnabled(False) 表示「刷新中」：把菜单里正亮着的控件禁用，QMenu 会把高亮甩回
    # 第一行（用户看到「点完自己跳到第一个」）。改成变灰 + 点击时忽略，高亮就留在原地。
    button.setProperty("busy", bool(refreshing))
    button.setToolTip("重新向引擎要一次模型清单：上游新上的会加进来，下架的会去掉")
    holder = QtWidgets.QWidgetAction(menu)
    holder.setDefaultWidget(button)
    menu.addAction(holder)
    if not refreshing:
        button.clicked.connect(lambda: on_refresh())
    return button


def current_label(items, current: str, *, fallback: str = "选模型") -> str:
    """按钮上显示什么：当前模型的名字（不带供应商前缀，一级菜单里已经有分类了）。"""
    for raw in items or []:
        row = _as_dict(raw)
        if str(row.get("id") or "") == current:
            return str(row.get("model") or row.get("label") or current)
    return fallback

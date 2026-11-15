"""历史窗口：这一场已经取回的历史行，以及服务端还剩多少更早的。

历史按「从最新往回数」一页一页取（REST `order=latest` + `offset`）。这里只算三件事，
不碰 Qt，所以能单测：

* **顺序**——`items` 从旧到新；接页时按行号去重，同一行永远只留一份。
* **偏移**——`next_offset` 就是下一次要取的 `offset`，等于已经取回的行数。
* **收手**——回来的行数不足一页，就是服务端也到底了（`older_available` 落成 `False`），
  往上滚不会再问。

参照实现（`hermes-agent` desktop）：`store/transcript-tail` 的 `nextOffset` / 短页即到底，
`app/chat/transcript-backfill` 的 `mergeOlderTranscriptPage`（按行去重后接在前面）；
同一场又回来一页最新的（重连、切模型）走 `refresh`，更早那截留住——对应
`graftRefreshedTailOntoBackfill`。
"""

from __future__ import annotations


def row_key(row: dict) -> tuple:
    """一行的身份：有行号认行号；没有行号（本场新发、还没落库）认「谁说的 + 正文」。"""
    row_id = row.get("row_id")
    if isinstance(row_id, int):
        return ("id", row_id)
    return text_key(row)


def text_key(row: dict) -> tuple:
    """「谁说的 + 正文」：没有行号的那几条靠它跟库行对得上。"""
    return ("text", str(row.get("role") or ""), str(row.get("text") or "").strip())


def same_row(hist_row: dict, live_row: dict) -> bool:
    """库行与屏上那行是不是同一条：**不对称**——屏上那行有行号就认行号，没有才看正文。

    库行行号是身份；屏上那行可能是 `session.resume` 画出来的（用户行已经打过号），
    也可能是本场新发的（助手行永远没有行号）。两边都用 `row_key` 会把「本该并掉」的
    助手行配成不相等，重叠数归零、于是重铺时被画第二遍。
    """
    row_id = live_row.get("row_id")
    if isinstance(row_id, int):
        return hist_row.get("row_id") == row_id
    return text_key(hist_row) == text_key(live_row)


class HistoryWindow:
    """一句话：**取回来的都在这儿，接页只往前面加，去重认行号。**"""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.next_offset = 0
        self.older_available = False

    # ---------- 装 ----------

    def reset(self, rows, older_available: bool = False, live=()) -> None:
        """开窗：最新一页 + 屏上已经有、历史里还没有的那几条（本场新发的）。

        屏上那几条通常就是这一页的尾巴（`session.resume` 先把最近几轮画上去，历史随后到）：
        从尾巴往前一条条对，对得上的并进历史，对不上的（更新、这一页里还没有）接在后面。

        进来几条库行、`next_offset` 就推进几条：本场新发的那几条不在服务端的 offset 空间里，
        算进去会把下一页的起点推过头。
        """
        items: list[dict] = list(rows or ())
        rest = list(live or ())
        # 屏上那几条通常从历史页的尾巴接上：找出「历史尾巴 == 屏上开头」最长的那截并掉，
        # 剩下的（比这一页新的）接在后面。
        overlap = 0
        for count in range(min(len(items), len(rest)), 0, -1):
            pairs = zip(items[-count:], rest[:count])
            if all(same_row(hist, live_row) for hist, live_row in pairs):
                overlap = count
                break
        self.items = items + rest[overlap:]
        self.next_offset = len(items)
        self.older_available = bool(older_available)

    def refresh(self, rows, older_available: bool = False) -> int:
        """同一场又来一页最新的（重连、切模型）：新的覆盖旧的，更早那截留住。

        两页靠共有的行接上：新页里有一行出现在旧 `items` 里，它前面那些就是更早的，原样留着。
        一行都对不上，说明这页已经是另一段历史（压缩重写），整段换掉。

        返回锚点前面留住了几条（`prefix` 长度）：界面拿它判「锚点之后的单子坐标还对不对」。
        """
        rows = list(rows or ())
        if not self.items:
            self.reset(rows, older_available)
            return 0
        if not rows:
            return len(self.items)
        keys = {row_key(row) for row in rows}
        anchor = next(
            (index for index, item in enumerate(self.items) if row_key(item) in keys),
            None,
        )
        prefix = self.items[:anchor] if anchor is not None else []
        merged = list(prefix)
        seen = {row_key(row) for row in merged}
        for row in rows:
            key = row_key(row)
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
        self.items = merged
        # 已经有了更早的那截，「还有更早的」这句话就不该被这一页抹成没有。
        self.older_available = bool(older_available) or bool(prefix)
        return len(prefix)

    def prepend(self, rows, older_available: bool) -> int:
        """接一页更早的：按行号去重后放到最前面，返回真的接进来几条（`front_added`）。"""
        rows = list(rows or ())
        seen = {row_key(row) for row in self.items}
        fresh = []
        for row in rows:
            key = row_key(row)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(row)
        if fresh:
            self.items = fresh + self.items
        self.next_offset += len(rows)
        # 这一页全是已有的（offset 漂移）：不再往前问，否则在顶上死循环。
        self.older_available = bool(older_available) and bool(fresh)
        return len(fresh)

    def drop_from_row(self, row_id: int) -> None:
        """引擎从这一行往后截断了（重新生成/编辑重发）：窗口里这一行及之后的都作废。

        库行行号是单调的，拿它当尺子；本场新发、还没落库的那几条没有行号，留给界面自己收。
        """
        self.items = [
            item
            for item in self.items
            if not isinstance(item.get("row_id"), int) or int(item["row_id"]) < int(row_id)
        ]

    def append_newest(self, rows) -> int:
        """接一页更晚的、跟窗口严丝合缝（`page_min == window_max + 1`）：去重后接在尾巴上。

        页序从旧到新，所以接在末尾；`next_offset` 不推进——这一页盖的是更新那一段，
        窗口最早那条的位置没动。去重两头都要管：窗口里已有的、以及这一批自己重号的
        （补洞攒出来的几段本来就会互相压一截）。
        """
        rows = list(rows or ())
        seen = {row_key(item) for item in self.items}
        fresh = []
        for row in rows:
            key = row_key(row)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(row)
        if fresh:
            self.items = self.items + fresh
        return len(fresh)

    def graft(self, rows) -> tuple[int, int]:
        """把补回来的几条插进窗口**中间**它们该在的地方（按行号），返回 `(插进几条, 头一条落在第几位)`。

        补洞跟接页方向相反：窗口新旧两头都在手上，缺的是中间那一段。按行号归位；本场新发、
        没有行号的那几条留在尾巴上不动。`next_offset` 不动——它数的是「比窗口最老那条更新的
        行有多少」，往中间插几条不改变这个数。

        返回的位次是**插入前**的计数（单子和洞的坐标都按那一套记）。
        """
        fresh: list[dict] = []
        seen = {row_key(item) for item in self.items}
        for row in rows or ():
            if not isinstance(row.get("row_id"), int):
                continue
            key = row_key(row)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(row)
        if not fresh:
            return 0, 0
        fresh.sort(key=lambda row: int(row["row_id"]))
        merged: list[dict] = []
        pending = list(fresh)
        anchor: int | None = None
        old_seen = 0
        for item in self.items:
            row_id = item.get("row_id")
            if isinstance(row_id, int):
                while pending and int(pending[0]["row_id"]) < int(row_id):
                    if anchor is None:
                        anchor = old_seen
                    merged.append(pending.pop(0))
            merged.append(item)
            old_seen += 1
        if pending:
            if anchor is None:
                anchor = old_seen
            merged.extend(pending)
        self.items = merged
        return len(fresh), int(anchor or 0)

    def trim_newest(self, keep: int) -> int:
        """上限：条数超了就从**最新那头**丢（人在顶上读的是最旧那端），返回丢了几条。

        只裁不移：留下那些条的位置一个都没变，单子和洞的坐标照旧；被裁掉的由调用方作废。
        `next_offset` 不动——它数的是已经从服务端取回多少条，跟窗口留几条无关。
        """
        over = len(self.items) - max(0, int(keep))
        if over <= 0:
            return 0
        self.items = self.items[: len(self.items) - over]
        return over

    @property
    def hist_count(self) -> int:
        """窗口里带 `_hist` 的条数：补洞公式里「已经握在手上的、比 L 新的」那一半。"""
        return sum(1 for item in self.items if item.get("_hist"))

    def row_id_bounds(self) -> tuple[int | None, int | None]:
        """窗口里**历史行**（`_hist`）的行号 `(最小, 最大)`：判接缝、定补洞封口都用它。

        只数历史行：本场刚发的那几条（`stamp_user_row` 当场打了行号）也在窗口里，行号比任何
        一页都新——拿它当窗口尾巴，缺段会被判成「有重叠」、`L` 会指到洞上方，洞就补不到。
        一条带号的历史行都没有（冷启动只有屏上行）时给 `(None, None)`：调用方退回按 offset 判。
        """
        ids = [
            int(item["row_id"])
            for item in self.items
            if item.get("_hist") and isinstance(item.get("row_id"), int)
        ]
        return (min(ids), max(ids)) if ids else (None, None)

    # ---------- 问 ----------

    @property
    def loaded(self) -> bool:
        return bool(self.items)

    @property
    def need_page(self) -> bool:
        """屏上已经到顶、服务端还有更早的：该再去问一页。"""
        return self.older_available

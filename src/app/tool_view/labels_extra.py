"""`labels.py` 之外的补充文案。

`labels.py` 由 `scripts/gen_tool_labels.py` 从 TS 文案生成，源里没有后加的引擎工具
（`delegate_task` / `session_search` / `skill_*` / 飞书与 yb 这批）。缺条目时
`tr()` 会把键名原样返回，界面标题就变成 `assistant.tool.titles.delegate_task.done`。
这里按下表补上，`i18n.py` 导入时并进 `LABELS`；重新生成 `labels.py` 不会冲掉它。

结构：name → (中文 done, pending, pendingAction, 英文 done, pending, pendingAction)
"""

from __future__ import annotations

_ENTRIES: dict[str, tuple[str, str, str, str, str, str]] = {
    "annotate_preview": ("已标注预览", "正在标注预览", "正在标注", "Annotated preview", "Annotating preview", "Annotating"),
    "close_terminal": ("已关闭终端标签", "正在关闭终端标签", "正在关闭", "Closed terminal tab", "Closing terminal tab", "Closing"),
    "computer_use": ("已操作桌面", "正在操作桌面", "正在操作", "Used the desktop", "Using the desktop", "Using"),
    "cronjob_manage": ("已管理定时任务", "正在管理定时任务", "正在管理", "Managed cron job", "Managing cron job", "Managing"),
    "delegate_task": ("已派发子任务", "正在派发子任务", "正在派发", "Dispatched subagents", "Dispatching subagents", "Dispatching"),
    "desktop_preview": ("已操作预览窗", "正在操作预览窗", "正在操作", "Used the preview pane", "Using the preview pane", "Using"),
    "desktop_project": ("已切换项目", "正在切换项目", "正在切换", "Switched project", "Switching project", "Switching"),
    "drive_preview": ("已浏览预览窗", "正在浏览预览窗", "正在浏览", "Browsed the preview pane", "Browsing the preview pane", "Browsing"),
    "feishu_doc_read": ("已读飞书文档", "正在读飞书文档", "正在读取", "Read Feishu doc", "Reading Feishu doc", "Reading"),
    "feishu_drive_add_comment": ("已添加飞书评论", "正在添加飞书评论", "正在添加", "Added Feishu comment", "Adding Feishu comment", "Adding"),
    "feishu_drive_list_comment_replies": ("已读评论回复", "正在读评论回复", "正在读取", "Read comment replies", "Reading comment replies", "Reading"),
    "feishu_drive_list_comments": ("已列出飞书评论", "正在列出飞书评论", "正在列出", "Listed Feishu comments", "Listing Feishu comments", "Listing"),
    "feishu_drive_reply_comment": ("已回复飞书评论", "正在回复飞书评论", "正在回复", "Replied to Feishu comment", "Replying to Feishu comment", "Replying"),
    "focus_pane": ("已切换面板", "正在切换面板", "正在切换", "Focused pane", "Focusing pane", "Focusing"),
    "gui_tour": ("已带你走一遍界面", "正在带你走一遍界面", "正在演示", "Started GUI tour", "Starting GUI tour", "Showing"),
    "manage_catalog": ("已管理插件", "正在管理插件", "正在管理", "Managed plugin catalog", "Managing plugin catalog", "Managing"),
    "manage_connections": ("已处理账号连接", "正在处理账号连接", "正在连接", "Connected account", "Connecting account", "Connecting"),
    "process_manage": ("已管理进程", "正在管理进程", "正在管理", "Managed process", "Managing process", "Managing"),
    "react_to_message": ("已回应消息", "正在回应消息", "正在回应", "Reacted to message", "Reacting to message", "Reacting"),
    "read_terminal": ("已读终端输出", "正在读终端输出", "正在读取", "Read terminal output", "Reading terminal output", "Reading"),
    "read_window_below": ("已查看后方窗口", "正在查看后方窗口", "正在查看", "Looked at window below", "Looking at window below", "Looking"),
    "session_search": ("已检索会话记录", "正在检索会话记录", "正在检索", "Searched past sessions", "Searching past sessions", "Searching"),
    "set_theme": ("已更换主题", "正在更换主题", "正在更换", "Changed theme", "Changing theme", "Changing"),
    "setup_beat": ("已设置节拍", "正在设置节拍", "正在设置", "Set up beat", "Setting up beat", "Setting up"),
    "show_tip": ("已显示提示", "正在显示提示", "正在提示", "Showed tip", "Showing tip", "Showing"),
    "skill_manage": ("已修改技能", "正在修改技能", "正在修改", "Edited skill", "Editing skill", "Editing"),
    "skill_view": ("已读技能", "正在读技能", "正在读取", "Read skill", "Reading skill", "Reading"),
    "skills_list": ("已列出技能", "正在列出技能", "正在列出", "Listed skills", "Listing skills", "Listing"),
    "text_to_speech": ("已生成语音", "正在生成语音", "正在生成", "Generated speech", "Generating speech", "Generating"),
    "todo_list": ("已更新任务清单", "正在更新任务清单", "正在更新", "Updated todo list", "Updating todo list", "Updating"),
    "video_analyze": ("已分析视频", "正在分析视频", "正在分析", "Analyzed video", "Analyzing video", "Analyzing"),
    "video_generate": ("已生成视频", "正在生成视频", "正在生成", "Generated video", "Generating video", "Generating"),
    "x_search": ("已搜索 X", "正在搜索 X", "正在搜索", "Searched X", "Searching X", "Searching"),
    "yb_query_group_info": ("已查群信息", "正在查群信息", "正在查询", "Looked up group info", "Looking up group info", "Looking up"),
    "yb_query_group_members": ("已查群成员", "正在查群成员", "正在查询", "Looked up group members", "Looking up group members", "Looking up"),
    "yb_search_sticker": ("已找表情包", "正在找表情包", "正在查找", "Searched stickers", "Searching stickers", "Searching"),
    "yb_send_dm": ("已发私信", "正在发私信", "正在发送", "Sent direct message", "Sending direct message", "Sending"),
    "yb_send_sticker": ("已发表情包", "正在发表情包", "正在发送", "Sent sticker", "Sending sticker", "Sending"),
}

def _flatten_entries() -> dict[str, dict[str, str]]:
    """拍平成 `i18n.tr()` 用的点号键，跟 `labels.LABELS` 的形态保持一致。"""
    extra: dict[str, dict[str, str]] = {"zh": {}, "en": {}}
    for name, (zh_done, zh_pending, zh_action, en_done, en_pending, en_action) in _ENTRIES.items():
        base = f"assistant.tool.titles.{name}"
        extra["zh"].update(
            {f"{base}.done": zh_done, f"{base}.pending": zh_pending, f"{base}.pendingAction": zh_action}
        )
        extra["en"].update(
            {f"{base}.done": en_done, f"{base}.pending": en_pending, f"{base}.pendingAction": en_action}
        )
    return extra


EXTRA: dict[str, dict[str, str]] = _flatten_entries()

"""工具行的中文/英文文案：由 `scripts/gen_tool_labels.py` 生成，别手改。

`ZH` / `EN` 保留原始嵌套结构，`LABELS` 是拍平成点号键之后的结果（供 `i18n.tr` 查）。
"""

from __future__ import annotations

from typing import Any

ZH: dict[str, Any] = {
      "copyCode": '复制代码',
      "renderingImage": '正在渲染图片',
      "copyOutput": '复制输出',
      "copyCommand": '复制命令',
      "copyContent": '复制内容',
      "copyUrl": '复制 URL',
      "copyResults": '复制结果',
      "copyQuery": '复制查询',
      "copyFile": '复制文件',
      "copyPath": '复制路径',
      "failedCalls": lambda count: f"{count} 次工具调用失败",
      "skillActivity": {
        "loading": '正在加载技能',
        "loaded": '已加载技能',
        "loadFailed": '技能加载失败',
        "readingResource": '正在读取技能资源',
        "readResource": '已读取技能资源',
        "resourceFailed": '技能资源读取失败',
        "listing": '正在列出技能',
        "listed": '已列出技能',
        "listFailed": '技能列表获取失败',
        "unavailable": '技能结果不可用'
      },
      "outputAlt": '工具输出',
      "rawResponse": '原始响应',
      "copyActivity": '复制活动',
      "recoveredOne": '在 1 个失败步骤后已恢复',
      "recoveredMany": lambda count: f"在 {count} 个失败步骤后已恢复",
      "failedOne": '1 个步骤失败',
      "failedMany": lambda count: f"{count} 个步骤失败",
      "statusRunning": '运行中',
      "statusError": '错误',
      "statusRecovered": '已恢复',
      "statusDone": '完成',
      "resultUnavailable": '结果不可用',
      "resultInterrupted": '已中断',
      "memoryWriteNoted": '已记下记忆写入',
      "actions": {
        "read": '已读取',
        "reading": '正在读取',
        "opened": '已打开',
        "opening": '正在打开',
        "failedToOpen": '打开失败',
        "searched": '已搜索',
        "searching": '正在搜索',
        "ran": '已运行',
        "running": '正在运行',
        "ranCode": '已运行代码',
        "runningCode": '正在编写脚本'
      },
      "prefixes": {
        "browser": '浏览器',
        "web": '网页'
      },
      "titleTemplates": {
        "actionCommand": lambda action, command: f"{action} {command}",
        "actionQuoted": lambda action, value: f"{action}“{value}”",
        "actionTarget": lambda action, target: f"{action} {target}",
        "prefixedDone": lambda prefix, action: f"{prefix}{action}",
        "runningPrefixedTool": lambda prefix, action: f"正在运行{prefix}{action}",
        "runningTool": lambda action: f"正在运行 {action}"
      },
      "titles": {
        "browser_click": { "done": '已点击页面元素', "pending": '正在点击页面元素', "pendingAction": '正在点击' },
        "browser_fill": { "done": '已填写表单字段', "pending": '正在填写表单字段', "pendingAction": '正在填写' },
        "browser_navigate": { "done": '已打开页面', "pending": '正在打开页面', "pendingAction": '正在打开' },
        "browser_snapshot": { "done": '已捕获页面快照', "pending": '正在捕获页面快照', "pendingAction": '正在捕获' },
        "browser_take_screenshot": { "done": '已捕获截图', "pending": '正在捕获截图', "pendingAction": '正在捕获' },
        "browser_type": { "done": '已在页面输入', "pending": '正在页面输入', "pendingAction": '正在输入' },
        "clarify": { "done": '已提问', "pending": '正在提问', "pendingAction": '正在提问' },
        "cronjob": { "done": 'Cron 任务', "pending": '正在安排 Cron 任务', "pendingAction": '正在安排' },
        "edit_file": { "done": '已编辑文件', "pending": '正在编辑文件', "pendingAction": '正在编辑' },
        "execute_code": { "done": '已运行代码', "pending": '正在编写脚本', "pendingAction": '正在编写脚本' },
        "image_generate": { "done": '已生成图片', "pending": '正在生成图片', "pendingAction": '正在生成' },
        "list_files": { "done": '已列出文件', "pending": '正在列出文件', "pendingAction": '正在列出' },
        "memory": { "done": '已保存到记忆', "pending": '正在保存到记忆', "pendingAction": '正在保存' },
        "patch": { "done": '已修补文件', "pending": '正在修补文件', "pendingAction": '正在修补' },
        "read_file": { "done": '已读取文件', "pending": '正在读取文件', "pendingAction": '正在读取' },
        "search_files": { "done": '已搜索文件', "pending": '正在搜索文件', "pendingAction": '正在搜索' },
        "session_search_recall": { "done": '已搜索会话历史', "pending": '正在搜索会话历史', "pendingAction": '正在搜索' },
        "terminal": { "done": '已运行命令', "pending": '正在运行命令', "pendingAction": '正在运行' },
        "todo": { "done": '已更新待办', "pending": '正在更新待办', "pendingAction": '正在更新' },
        "vision_analyze": { "done": '已分析图片', "pending": '正在分析图片', "pendingAction": '正在分析' },
        "web_extract": { "done": '已读取网页', "pending": '正在读取网页', "pendingAction": '正在读取' },
        "web_search": { "done": '已搜索网页', "pending": '正在搜索网页', "pendingAction": '正在搜索' },
        "write_file": { "done": '已编辑文件', "pending": '正在编辑文件', "pendingAction": '正在编辑' }
      }
    }

EN: dict[str, Any] = {
      "copyCode": 'Copy code',
      "renderingImage": 'Rendering image',
      "copyOutput": 'Copy output',
      "copyCommand": 'Copy command',
      "copyContent": 'Copy content',
      "copyUrl": 'Copy URL',
      "copyResults": 'Copy results',
      "copyQuery": 'Copy query',
      "copyFile": 'Copy file',
      "copyPath": 'Copy path',
      "failedCalls": lambda count: f"{count} tool call{('' if count == 1 else 's')} failed",
      "skillActivity": {
        "loading": 'Loading skill',
        "loaded": 'Loaded skill',
        "loadFailed": 'Failed to load skill',
        "readingResource": 'Reading skill resource',
        "readResource": 'Read skill resource',
        "resourceFailed": 'Failed to read skill resource',
        "listing": 'Listing skills',
        "listed": 'Listed skills',
        "listFailed": 'Failed to list skills',
        "unavailable": 'Skill result unavailable'
      },
      "outputAlt": 'Tool output',
      "rawResponse": 'Raw response',
      "copyActivity": 'Copy activity',
      "recoveredOne": 'Recovered after 1 failed step',
      "recoveredMany": lambda count: f"Recovered after {count} failed steps",
      "failedOne": '1 step failed',
      "failedMany": lambda count: f"{count} steps failed",
      "statusRunning": 'Running',
      "statusError": 'Error',
      "statusRecovered": 'Recovered',
      "statusDone": 'Done',
      "resultUnavailable": 'Result unavailable',
      "resultInterrupted": 'Interrupted',
      "memoryWriteNoted": 'Memory write noted',
      "actions": {
        "read": 'Read',
        "reading": 'Reading',
        "opened": 'Opened',
        "opening": 'Opening',
        "failedToOpen": 'Failed to open',
        "searched": 'Searched',
        "searching": 'Searching',
        "ran": 'Ran',
        "running": 'Running',
        "ranCode": 'Ran code',
        "runningCode": 'Scripting'
      },
      "prefixes": {
        "browser": 'Browser',
        "web": 'Web'
      },
      "titleTemplates": {
        "actionCommand": lambda action, command: f"{action} {command}",
        "actionQuoted": lambda action, value: f"{action} “{value}”",
        "actionTarget": lambda action, target: f"{action} {target}",
        "prefixedDone": lambda prefix, action: f"{prefix} {action}",
        "runningPrefixedTool": lambda prefix, action: f"Running {prefix.lower()} {action.lower()}",
        "runningTool": lambda action: f"Running {action.lower()}"
      },
      "titles": {
        "browser_click": { "done": 'Clicked page element', "pending": 'Clicking page element', "pendingAction": 'Clicking' },
        "browser_fill": { "done": 'Filled form field', "pending": 'Filling form field', "pendingAction": 'Filling' },
        "browser_navigate": { "done": 'Opened page', "pending": 'Opening page', "pendingAction": 'Opening' },
        "browser_snapshot": {
          "done": 'Captured page snapshot',
          "pending": 'Capturing page snapshot',
          "pendingAction": 'Capturing'
        },
        "browser_take_screenshot": {
          "done": 'Captured screenshot',
          "pending": 'Capturing screenshot',
          "pendingAction": 'Capturing'
        },
        "browser_type": { "done": 'Typed on page', "pending": 'Typing on page', "pendingAction": 'Typing' },
        "clarify": { "done": 'Asked a question', "pending": 'Asking a question', "pendingAction": 'Asking' },
        "cronjob": { "done": 'Cron job', "pending": 'Scheduling cron job', "pendingAction": 'Scheduling' },
        "edit_file": { "done": 'Edited file', "pending": 'Editing file', "pendingAction": 'Editing' },
        "execute_code": { "done": 'Ran code', "pending": 'Scripting', "pendingAction": 'Scripting' },
        "image_generate": { "done": 'Generated image', "pending": 'Generating image', "pendingAction": 'Generating' },
        "list_files": { "done": 'Listed files', "pending": 'Listing files', "pendingAction": 'Listing' },
        "memory": { "done": 'Saved to memory', "pending": 'Saving to memory', "pendingAction": 'Saving' },
        "patch": { "done": 'Patched file', "pending": 'Patching file', "pendingAction": 'Patching' },
        "read_file": { "done": 'Read file', "pending": 'Reading file', "pendingAction": 'Reading' },
        "search_files": { "done": 'Searched files', "pending": 'Searching files', "pendingAction": 'Searching' },
        "session_search_recall": {
          "done": 'Searched session history',
          "pending": 'Searching session history',
          "pendingAction": 'Searching'
        },
        "terminal": { "done": 'Ran command', "pending": 'Running command', "pendingAction": 'Running' },
        "todo": { "done": 'Updated todos', "pending": 'Updating todos', "pendingAction": 'Updating' },
        "vision_analyze": { "done": 'Analyzed image', "pending": 'Analyzing image', "pendingAction": 'Analyzing' },
        "web_extract": { "done": 'Read webpage', "pending": 'Reading webpage', "pendingAction": 'Reading' },
        "web_search": { "done": 'Searched web', "pending": 'Searching web', "pendingAction": 'Searching' },
        "write_file": { "done": 'Edited file', "pending": 'Editing file', "pendingAction": 'Editing' }
      }
    }

COMMON_ZH: dict[str, str] = {'copied': '已复制', 'copy': '复制'}

COMMON_EN: dict[str, str] = {'copied': 'Copied', 'copy': 'Copy'}

def _flatten(node: dict[str, Any], prefix: str = "assistant.tool") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _with_common(flat: dict[str, Any], common: dict[str, str]) -> dict[str, Any]:
    return {**flat, **{f"common.{key}": value for key, value in common.items()}}


LABELS: dict[str, dict[str, Any]] = {
    "zh": _with_common(_flatten(ZH), COMMON_ZH),
    "en": _with_common(_flatten(EN), COMMON_EN),
}

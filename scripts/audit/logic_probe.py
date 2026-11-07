"""逻辑层探针：每个模块用最小输入真调一次，打印 RESULT 行（ok / 值 / 异常）。"""
from __future__ import annotations

import json
import os
import tempfile
import shutil
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = Path(tempfile.gettempdir()) / "noesia-audit"
TMP = SCRATCH / "noesia_logic_probe"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["NOESIA_NO_DIALOG"] = "1"
os.environ["NOESIA_HOME"] = str(TMP / "home")
(TMP / "home").mkdir(parents=True, exist_ok=True)
shutil.copy2(REPO / "data" / "NoesiaHome" / "config.yaml", TMP / "home" / "config.yaml")

sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)

from PySide6 import QtCore, QtWidgets  # noqa: E402

qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

SETTINGS = REPO / "data" / "settings"
BACKUP = TMP / "settings_backup"
shutil.copytree(SETTINGS, BACKUP)

results = []


def run(name, fn):
    try:
        value = fn()
        results.append((name, "OK", value))
    except Exception as exc:  # noqa: BLE001
        results.append((name, "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"))


def persist_roundtrip():
    from app import avatar_prefs, speech_prefs, text_prefs, voice_input_prefs
    out = {}
    a = avatar_prefs.load()
    out["avatar_keys"] = sorted(a)
    avatar_prefs.save(provider="vidu", model="m1")
    out["avatar_saved"] = avatar_prefs.current().get("provider")
    avatar_prefs.clear_image()
    out["avatar_image"] = avatar_prefs.image_setting()
    s = speech_prefs.load()
    out["speech_keys"] = sorted(s)[:6]
    speech_prefs.save(enabled=True, provider="edge", voice_id="zh-CN-XiaoxiaoNeural")
    out["speech_current"] = speech_prefs.current().get("provider")
    voice_input_prefs.remember("guid-1")
    out["voice_device"] = voice_input_prefs.device_guid()
    voice_input_prefs.remember_hotkey("Ctrl+Alt+V")
    out["voice_hotkey"] = voice_input_prefs.hotkey()
    text_prefs.remember("deepseek", "deepseek-flash")
    out["text_remembered"] = text_prefs.remembered("deepseek")
    return out


def engine_config_roundtrip():
    from app import engine_config
    home = TMP / "home"
    core = REPO / "src" / "engine"
    before = engine_config.read_engine_config(home)
    ok = engine_config.set_engine_config(core, home, {"memory.user_char_limit": 4321})
    after = engine_config.read_engine_config(home)
    limits = engine_config.memory_limits(core, home)
    return {"written": ok, "has_before": bool(before), "user_char_limit": after.get("memory", {}).get("user_char_limit"),
            "limits": limits}


def markdown_probe():
    from app import markdown_render as md
    table = md.block("| a | b |\n|---|---|\n| 1 | 2 |\n")
    fenced = md.block("```python\ndef f(x):\n    return x\n```\n")
    inline = md.inline("文字 **粗** `code` [链接](https://example.com)")
    segs = md.split_fences("前\n```sh\nls\n```\n后")
    return {"table_has_table": "<table" in table, "fence_has_pre": "<pre" in fenced or "background" in fenced,
            "inline_has_b": "<b>" in inline or "<strong>" in inline, "segments": len(segs),
            "structural": md.is_structural("| a | b |"), "fence_detect": md.is_fence("```py")}


def media_probe():
    from app import media_links as ml
    text, raw = ml.split_media_line(f"看这个 MEDIA:{REPO}/assets/preview.mp4")
    return {"text": text, "raw": raw, "kind": ml.kind_label(".mp4"),
            "human": ml.human_size(1536), "resolve": str(ml.resolve(str(REPO / "assets" / "preview.mp4"))),
            "html_has_img": "img" in ml.media_link_html(str(REPO / "assets" / "noesia_transparent_x.png"))}


def tool_view_probe():
    from app.tool_view.adapter import view_for
    cases = {
        "read": {"kind": "tool.complete", "name": "read_file", "args": {"path": "src/app/main.py", "offset": 1, "limit": 20},
                 "result": {"content": "line1\nline2", "total_lines": 120}, "is_error": False, "tool_call_id": "t1"},
        "search_empty": {"kind": "tool.complete", "name": "search_files", "args": {"pattern": "zzz", "target": "content"},
                         "result": {"matches": []}, "is_error": False, "tool_call_id": "t2"},
        "terminal": {"kind": "tool.complete", "name": "terminal", "args": {"command": "ls -la"},
                     "result": {"output": "a\nb", "exit_code": 0, "duration_seconds": 1.2}, "is_error": False, "tool_call_id": "t3"},
        "edit": {"kind": "tool.complete", "name": "write_file", "args": {"path": "x.py", "content": "print(1)"},
                 "result": {"ok": True, "inline_diff": "--- a\n+++ b\n@@\n-old\n+new"}, "is_error": False, "tool_call_id": "t4"},
        "error": {"kind": "tool.complete", "name": "terminal", "args": {"command": "false"},
                  "result": {"output": "boom", "exit_code": 1}, "is_error": True, "tool_call_id": "t5"},
        "running": {"kind": "tool.start", "name": "search_files", "args": {"pattern": "x"}, "tool_call_id": "t6"},
    }
    out = {}
    for name, ev in cases.items():
        view, part = view_for(ev)
        out[name] = {"title": view.title, "count": view.count_label, "status": view.status,
                     "detail_len": len(view.detail or ""), "detail_head": (view.detail or "")[:40],
                     "subtitle": view.subtitle}
    return out


def geometry_probe():
    from app import window_geometry as g
    size = QtCore.QSize(360, 480)
    return {"edge_top_left": int(g.edges_at(QtCore.QPoint(1, 1), size).value),
            "cursor": int(g.cursor_for(g.edges_at(QtCore.QPoint(1, 1), size)).value),
            "fit": (g.fit_width(360, 1.33).width(), g.fit_width(360, 1.33).height()),
            "resize": (g.rect_after_resize(QtCore.QRect(0, 0, 360, 480), 0, QtCore.QPoint(10, 10), 100, 200).size().toTuple())}


def vocab_probe():
    from app import voice_vocab
    words = voice_vocab.discover(["我们用了 PySide6 和 DeepSeek 的接口", "Noesia 的语音识别需要热词", "DeepSeek 很好用"])
    payload = voice_vocab.build(["terminal 里的 ffmpeg 命令"])
    return {"discovered": words[:8], "build_keys": sorted(payload)}


def menu_probe():
    from app import model_menu
    items = [
        {"id": "deepseek:deepseek-flash", "provider": "deepseek", "model": "deepseek-flash", "label": "DeepSeek"},
        {"id": "openrouter:z-model", "provider": "openrouter", "model": "z-model", "label": "OpenRouter"},
        {"id": "openrouter:a-model", "provider": "openrouter", "model": "a-model", "label": "OpenRouter"},
    ]
    groups = model_menu.group_models(items)
    label = model_menu.current_label(items, "deepseek:deepseek-flash")
    missing = model_menu.current_label(items, "x:y")
    return {"groups": [(g.get("provider"), [i.get("model") for i in g.get("items", [])]) for g in groups],
            "label": label, "missing": missing}


def status_probe():
    from app.status_stack import StatusStack
    stack = StatusStack()
    stack.resize(420, 200)
    stack.show()
    qt_app.processEvents()
    h0 = stack.sizeHint().height()
    stack.show_notice("turn", "出错了：HTTP 403", level="error")
    qt_app.processEvents()
    h1 = stack.sizeHint().height()
    stack.set_activity("正在调用 terminal…")
    qt_app.processEvents()
    stack.set_processes([{"session_id": "s1", "state": "running", "command": "pytest", "started_at": 0}])
    qt_app.processEvents()
    h2 = stack.sizeHint().height()
    stack.clear_notice("turn")
    stack.set_activity("")
    stack.set_processes([])
    qt_app.processEvents()
    h3 = stack.sizeHint().height()
    stack.clear_all()
    return {"h_empty": h0, "h_error": h1, "h_busy": h2, "h_cleared": h3}


def speech_bar_probe():
    from app.speech_bar import SpeechBar
    bar = SpeechBar()
    bar.resize(360, 60)
    bar.show()
    bar.show_preparing()
    qt_app.processEvents()
    bar.show_playing()
    bar.set_progress(1200, 5600)
    qt_app.processEvents()
    txt = bar.findChildren(QtWidgets.QLabel)
    out = [l.text() for l in txt]
    bar.dismiss()
    qt_app.processEvents()
    return {"labels": out}


run("prefs.roundtrip", persist_roundtrip)
run("engine_config.roundtrip", engine_config_roundtrip)
run("markdown", markdown_probe)
run("media_links", media_probe)
run("tool_view", tool_view_probe)
run("window_geometry", geometry_probe)
run("voice_vocab", vocab_probe)
run("model_menu", menu_probe)
run("status_stack", status_probe)
run("speech_bar", speech_bar_probe)

for name, state, value in results:
    print(f"RESULT {name} {state} {json.dumps(value, ensure_ascii=False, default=str)[:600]}")

for item in BACKUP.iterdir():
    target = SETTINGS / item.name
    if item.is_file():
        shutil.copy2(item, target)
for item in SETTINGS.iterdir():
    if not (BACKUP / item.name).exists() and item.is_file():
        item.unlink()
print("PROBE_DONE")

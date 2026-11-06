# 审计探针（可复跑）

`AUDIT.md` 里每一格判定都要能复算。这里是本轮审计用到的**本项目自写**探针，路径全部相对仓库根定位，输出不带时间戳，跑出来的行可以直接和 `AUDIT.md` 的证据表对（`AUDIT.md` / `BACKLOG.md` 在仓库根、已在 `.gitignore` 里，不入库）。

```bash
cd "D:/AI project/hermes/projects/noesia"
unset PYTHONPATH          # Hermes 宿主会把自身 site-packages 塞进 PYTHONPATH，先清掉
P=.venv/Scripts/python.exe
export QT_QPA_PLATFORM=offscreen
```

## 本目录的探针

| 脚本 | 对应证据 | 命令 | 该看到什么 |
|---|---|---|---|
| `import_smoke.py` | E1 | `$P scripts/audit/import_smoke.py` | `MODULES 68 FAILS 0` |
| `logic_probe.py` | E12 | `$P scripts/audit/logic_probe.py` | `RESULT <模块> OK {...}` 逐条 + `PROBE_DONE` |
| `data_probe.py` | E12 | `$P scripts/audit/data_probe.py` | `P2 <项> = <值>` 逐条 + `PROBE2_DONE` |
| `panel_probe.py` | E11 | `$P scripts/audit/panel_probe.py` | 一行 `PANEL send_idle=… media_raw_visible=False …` |
| `ui_probe.py <case>` | E11 | `$P scripts/audit/ui_probe.py chat_panel` | 一行 `PROBE_JSON {"case": …, "ok": true, "click_errors": []}` |
| `contract_check.py` | E13 | `$P scripts/audit/contract_check.py` | `CALLS 27 METHODS 18 CONTRACTS 255` / `UNKNOWN []` / `PARAM_MISMATCH 0` |
| `render_time.py` | E16 | `$P scripts/audit/render_time.py` | `TIME <控件> build=…ms render=…ms` |
| `shots.py` | E11 截图 | `$P scripts/audit/shots.py` | `SHOTS True True`，PNG 落在系统临时目录的 `noesia-audit/shots/` |

`ui_probe.py` 的要点：**一个进程只建一个控件**。同一个进程里连着建多个，模态 `QMenu.exec()` / `QDialog.exec()` 会卡住自己的事件循环（实测整批 300 秒超时）。它内部用 60ms 的 `QTimer` 把弹出的菜单/对话框逐个 `reject()`，所以 `chat_panel` 这类会弹菜单的控件也能跑完。

```bash
# 全部 UI 控件（每个 case 一个进程，卡住的那个只卡 45 秒）
for c in boot_splash chat_view status_stack tool_scroll speech_bar voice_orb chat_input \
         confirm approval engine_prompt name_dialog photo_view diff_preview avatar_status \
         token_checker model_dialog speaker stream_speaker recorder hotkey avatar_rtc \
         engine_process chat_panel floating_avatar; do
  printf "%s " "$c"; timeout 45 $P scripts/audit/ui_probe.py "$c" | grep -o 'PROBE_JSON.*'
done
```

探针都写进系统临时目录（`tempfile.gettempdir()/noesia-audit/`），不碰仓库里的 `data/`；会写 `data/` 的只有 `logic_probe.py`（它把 `data/settings` 备份到临时目录、跑完还原）。

## 机器扫描器（不在本仓库，跟着技能走）

这几个按约定放在 Hermes 技能目录（`%LOCALAPPDATA%\hermes\skills\software-development\…`），仓库里不复制一份：

| 脚本 | 技能 | 对应证据 | 用法 |
|---|---|---|---|
| `noesia-project/scripts/audit_static.py` | noesia-project | E9 | `$P <技能>/audit_static.py .` |
| `noesia-project/scripts/check_names.py` | noesia-project | E10 | `$P <技能>/check_names.py .` |
| `noesia-project/scripts/unreachable_stmts.py` | noesia-project | E3/E4 | `$P <技能>/unreachable_stmts.py . src/app src/assets src/backends scripts tools tests` |
| `codebase-inspection/scripts/orphan_scan.py` | codebase-inspection | E7 | `$P <技能>/orphan_scan.py src/app` |
| `codebase-inspection/scripts/strict_refs_scan.py` | codebase-inspection | E8 | `$P <技能>/strict_refs_scan.py src/app` |
| `codebase-inspection/scripts/broken_ref_scan.py` | codebase-inspection | E5/E6 | `$P <技能>/broken_ref_scan.py . --roots src/app,src/assets,src/backends` |

## 其余证据

```bash
$P -m compileall -q src                       # E2：无输出即 0 语法错误
$P scripts/run_tests.py                       # E15：21 个回归脚本，失败 0 个
$P scripts/check_cleanroom.py                 # E14：真起引擎、真回话（约 40 秒）
grep -rn 'setText("\[A-Za-z\]\|QPushButton("\[A-Za-z\]' src/app | wc -l   # E17：英文写死的界面文案
```

`ruff` / `vulture` / `pyflakes` 本机装不上（`pip --target` 退出码 2），`AUDIT.md` §5 写了用上面这些扫描器替代的逐条对应关系。

"""推理里真正干活的是引擎（`src/engine`）。

这个包只剩一样东西：`catalog`——把 `NOESIA_HOME` 钉在项目自己的引擎家目录上。
模型清单、供应商表都不在这里：唯一来源是引擎的 `model.options`（线上现取）。
"""

from __future__ import annotations

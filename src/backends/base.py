"""对话链路共用的数据结构。

只有两个东西：
- `LLMResult`：一轮回复的正文 + 情感（情感由我们自己的 `[[emotion:...]]` 标记解析出来，
  用来驱动表情/口型；推理本身在引擎里，这里不定义任何「后端」抽象）。
- 两个错误类型：配置错（改配置才有用）与请求错（可能值得重试）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResult:
    text: str
    emotion: str = "neutral"
    emotion_intensity: float = 0.5


class LLMConfigError(RuntimeError):
    """配置层面的问题（缺密钥、缺 base_url/model）：重试没用，要人改配置。"""


class LLMRequestError(RuntimeError):
    """服务端返回了错误响应。`status` 供调用方判断是否值得重试（0 = 流内 error 事件）。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

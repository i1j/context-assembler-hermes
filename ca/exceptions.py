"""
ca/exceptions.py — CA 异常定义

集中存放 CA 引擎内所有自定义异常，避免循环导入。
"""


class FctTruncatedException(Exception):
    """LLM 输出被截断时抛出的异常。

    携带 response_text 以便降级代码读取部分输出。
    """

    def __init__(self, message: str = "Fct output truncated", response_text: str = ""):
        super().__init__(message)
        self.message = message
        self.response_text = response_text

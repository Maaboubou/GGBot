"""mabowx 异常定义。"""


class MabowxError(Exception):
    """项目基础异常。"""


class MabowxUINotFoundError(MabowxError):
    """找不到目标 UIA 控件。"""


class MabowxOCRError(MabowxError):
    """OCR 相关异常。V1 不启用 OCR，先保留占位。"""


class MabowxNetworkError(MabowxError):
    """网络相关异常。V1 不依赖网络。"""


class MabowxNoteLoadTimeoutError(MabowxError):
    """笔记加载超时。V1 不实现笔记，先保留占位。"""

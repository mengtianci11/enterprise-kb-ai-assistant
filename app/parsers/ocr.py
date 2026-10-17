"""OCR 封装（惰性加载 + 线程安全）。

选型：RapidOCR（onnxruntime 后端）
- 原理：PP-OCR 检测/识别模型转 ONNX，用 onnxruntime 推理，无需 Paddle 框架；
- 优势：纯 pip 安装、模型内置、CPU 可跑、完全离线（契合「数据不出内网」目标）；
- 代价：精度略低于 PaddleOCR 全量版，单页中文约 1–3 秒（CPU）。

惰性加载：首次真正需要 OCR 时才导入并初始化引擎，
避免「不用 OCR 的场景也付出模型加载与内存开销」。
"""
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_engine: Any = None
_lock = threading.Lock()
_available: bool | None = None


def ocr_available() -> bool:
    """检查 OCR 依赖是否可用（不加载模型）。"""
    global _available
    if _available is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401

            _available = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("RapidOCR 不可用：%s", exc)
            _available = False
    return bool(_available)


def get_engine() -> Any:
    """获取（惰性初始化）OCR 引擎实例。"""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                logger.info("正在初始化 RapidOCR 引擎（首次调用，需数秒）...")
                _engine = RapidOCR()
                logger.info("RapidOCR 引擎就绪")
    return _engine


def recognize(image: Any, min_confidence: float = 0.6) -> tuple[str, list[str]]:
    """识别图像中的文字。

    image: 图片路径 / bytes / numpy 数组 / PIL.Image
    返回: (拼接后的文本, 告警列表)
    """
    warnings: list[str] = []
    arr = _to_array(image)
    engine = get_engine()
    result, _elapse = engine(arr)
    if not result:
        return "", ["OCR 未识别到文字"]

    lines: list[str] = []
    low_conf = 0
    for item in result:
        # item: [box, text, score]
        try:
            text, score = item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if score < min_confidence:
            low_conf += 1
        lines.append(text)

    if low_conf:
        warnings.append(f"{low_conf} 行文字识别置信度低于 {min_confidence}，建议人工校对")
    return "\n".join(lines), warnings


def _to_array(image: Any) -> Any:
    """统一转换为 numpy 数组（RapidOCR 最稳定的输入形式）。"""
    import numpy as np
    from PIL import Image

    if isinstance(image, np.ndarray):
        return image
    if isinstance(image, (bytes, bytearray)):
        import io

        return np.array(Image.open(io.BytesIO(image)).convert("RGB"))
    if isinstance(image, Image.Image):
        return np.array(image.convert("RGB"))
    # 文件路径
    return np.array(Image.open(image).convert("RGB"))


__all__ = ["ocr_available", "get_engine", "recognize"]

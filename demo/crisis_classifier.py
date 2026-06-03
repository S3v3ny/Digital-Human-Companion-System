"""
本地 TF-IDF 危机分类器（SOS-1K 训练）。

首次使用前运行 train_crisis_classifier.py 生成模型文件：
  python train_crisis_classifier.py

推理速度 <5ms，作为 LLM 精判前的快速初筛层：
  - 置信度 ≥ 0.75 的 high 预测：直接接受，跳过 LLM API 调用
  - 置信度 ≥ 0.75 的 none 预测（soft 信号）：直接排除，避免 LLM 误报
  - 其余情况：返回 available=True 并附上 hint，LLM 继续精判

模型文件不存在时静默返回 available=False，系统回退到纯 LLM 流程。
"""

import logging
import os
import pickle

logger = logging.getLogger(__name__)

_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "crisis-bert", "classifier.pkl")
_pipeline = None
_load_attempted = False


def _try_load() -> bool:
    """模块导入时已主动调用一次，后续命中缓存直接返回，不再阻塞事件循环。"""
    global _pipeline, _load_attempted
    if _load_attempted:
        return _pipeline is not None
    _load_attempted = True
    if not os.path.exists(_MODEL_PATH):
        logger.info("[CrisisClassifier] 模型文件不存在，跳过本地分类 (运行 train_crisis_classifier.py 可启用)")
        return False
    try:
        with open(_MODEL_PATH, "rb") as f:
            _pipeline = pickle.load(f)
        classes = list(_pipeline.classes_)
        logger.info(f"[CrisisClassifier] 本地分类器加载成功，类别: {classes}")
        return True
    except Exception as e:
        logger.warning(f"[CrisisClassifier] 加载失败: {e}")
        return False


# 模块导入时立即预加载，避免首次 predict() 调用在 async 上下文中阻塞事件循环
_try_load()


def predict(text: str) -> dict:
    """
    Returns:
        {
          "level":     "none" | "medium" | "high",
          "score":     float,       # 最大类别的置信度
          "probas":    dict,        # 各类别概率
          "available": bool,        # False 时调用方应回退到 LLM
        }
    """
    if not _try_load():
        return {"level": "none", "score": 0.0, "probas": {}, "available": False}
    try:
        proba_arr = _pipeline.predict_proba([text])[0]
        classes = list(_pipeline.classes_)
        probas = {c: round(float(p), 3) for c, p in zip(classes, proba_arr)}
        idx = int(proba_arr.argmax())
        level = classes[idx]
        score = float(proba_arr[idx])
        return {"level": level, "score": round(score, 3), "probas": probas, "available": True}
    except Exception as e:
        logger.warning(f"[CrisisClassifier] 预测失败: {e}")
        return {"level": "none", "score": 0.0, "probas": {}, "available": False}

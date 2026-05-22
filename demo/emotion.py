import cv2
from deepface import DeepFace
import numpy as np

# 仅在情绪发生变化时打印一次，避免每秒都刷日志
_last_emotion = None


def get_face_emotion(img_bytes):
    global _last_emotion
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            return "neutral"

        result = DeepFace.analyze(
            frame,
            actions=["emotion"],
            enforce_detection=False,
        )
        emotion = result[0]["dominant_emotion"]

        if emotion != _last_emotion:
            print(f"[emotion] 情绪变化: {_last_emotion} -> {emotion}")
            _last_emotion = emotion
        return emotion

    except Exception as e:
        print(f"[emotion] 识别出错: {e}")
        return "neutral"
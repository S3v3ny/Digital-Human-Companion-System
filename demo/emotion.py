import cv2
from deepface import DeepFace
import time
import numpy as np

def get_face_emotion(img_bytes): # 1. 添加参数接收字节流
    try:
        # 2. 将字节流转换为 OpenCV 格式的图片
        nparr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return "neutral"

        # 3. 识别情绪
        result = DeepFace.analyze(
            frame,
            actions=["emotion"],
            enforce_detection=False 
        )
        emotion = result[0]["dominant_emotion"]
        print(f"[emotion] 当前情绪: {emotion}")
        return emotion

    except Exception as e:
        print(f"识别出错: {e}")
        return "neutral"
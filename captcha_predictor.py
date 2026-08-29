from pathlib import Path

import numpy as np
from PIL import Image
import sys

_predictor = None


class CaptchaPredictor:
    def __init__(self, model_path: str | None = None):
        import onnxruntime as ort

        if model_path is None:
            root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            model_path = str(root / "models" / "captcha_model.onnx")

        model_file = Path(model_path)
        model_data_file = model_file.with_name(f"{model_file.name}.data")
        if not model_file.exists():
            raise FileNotFoundError(f"未找到验证码模型文件: {model_file}")
        if not model_data_file.exists():
            raise FileNotFoundError(f"未找到验证码模型权重文件: {model_data_file}")

        self.session = ort.InferenceSession(
            str(model_file), providers=["CPUExecutionProvider"]
        )

    def preprocess(self, image: Image.Image) -> np.ndarray:
        img = image.convert("RGB").resize((80, 20), Image.BILINEAR)
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = arr.transpose(2, 0, 1)
        return arr[np.newaxis, ...]

    def predict(self, image: Image.Image) -> str:
        outputs = self.session.run(None, {"image": self.preprocess(image)})[0]
        digits = outputs[0].argmax(axis=1)
        return "".join(str(digit) for digit in digits)


def predict_captcha(image: Image.Image) -> str:
    global _predictor
    if _predictor is None:
        _predictor = CaptchaPredictor()
    return _predictor.predict(image)

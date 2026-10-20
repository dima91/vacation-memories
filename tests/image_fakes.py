"""Deterministic synthetic images and the usual photo degradations, for the analysis tests."""

import io

import cv2
import numpy as np
from PIL import Image

from vacation_memories.frame_sampler import to_analysis_gray


def natural_image(seed: int, width: int = 640, height: int = 480) -> Image.Image:
    """Smooth random field plus fine texture: unlike the pattern scenes, it has photo-like statistics."""
    rng = np.random.RandomState(seed)
    coarse = cv2.resize(rng.rand(height // 16, width // 16).astype(np.float32), (width, height),
                        interpolation=cv2.INTER_CUBIC)
    fine = cv2.GaussianBlur(rng.rand(height, width).astype(np.float32), (0, 0), 1.5)
    pixels = np.clip(0.75 * coarse + 0.5 * fine - 0.1, 0, 1)
    return Image.fromarray((pixels * 255).astype(np.uint8)).convert("RGB")


def gray_of(image: Image.Image) -> np.ndarray:
    return to_analysis_gray(np.asarray(image.convert("L")))


def recompress(image: Image.Image, quality: int) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def scale_brightness(image: Image.Image, factor: float) -> Image.Image:
    return Image.fromarray(np.clip(np.asarray(image) * factor, 0, 255).astype(np.uint8))


def resize_slightly(image: Image.Image, factor: float) -> Image.Image:
    width, height = image.size
    return image.resize((round(width * factor), round(height * factor)), Image.BILINEAR)

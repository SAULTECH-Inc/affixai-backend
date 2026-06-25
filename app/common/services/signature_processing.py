"""Signature background-removal pipeline.

Multi-stage classical CV pipeline. Takes a photo or scan of a signature on
paper (any colored background, ruled lines, uneven lighting, dust, etc.) and
returns a PNG with the background made transparent.

Pipeline:
  1. Preprocess        — optional upscale, denoise, CLAHE illumination fix
  2. Background estim. — morphological close estimates the paper texture
  3. Ink isolation     — fuses Otsu + adaptive + local-contrast (majority vote)
  4. Cleanup           — removes ruled lines and small noise blobs
  5. Edge smoothing    — Gaussian blur on the mask as a soft alpha channel
  6. Compose RGBA      — original RGB + computed alpha → PNG

Refinements over the reference design:
  * Skips processing if input already has meaningful alpha transparency.
  * Adaptive morphological kernel scales with image size (instead of a hard
    51px) so thick or thin strokes both work.
  * Sanity check: if the mask ends up nearly empty, we return None and let the
    caller fall back to the unmodified upload.
  * Resizes the alpha back to the original dimensions if we upscaled, so the
    output isn't twice as large as the input.
"""
from __future__ import annotations

import io

import cv2
import numpy as np
from loguru import logger
from PIL import Image


# ---------- Stage 1: Preprocessing -------------------------------------------


def _preprocess(img_bgr: np.ndarray) -> tuple[np.ndarray, float]:
    """Returns (normalized image, scale factor applied)."""
    h, w = img_bgr.shape[:2]
    scale = 1.0
    if max(h, w) < 1000:
        scale = 2.0
        img_bgr = cv2.resize(
            img_bgr, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC
        )

    denoised = cv2.fastNlMeansDenoisingColored(
        img_bgr, None,
        h=10, hColor=10, templateWindowSize=7, searchWindowSize=21,
    )

    # CLAHE on the L channel of LAB to flatten illumination
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    normalized = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    return normalized, scale


# ---------- Stage 2: Background estimation -----------------------------------


def _adaptive_kernel_size(shape: tuple[int, int]) -> int:
    """Pick an odd kernel size proportional to the image, bounded [21, 81]."""
    base = max(shape) // 30
    base = max(21, min(81, base))
    if base % 2 == 0:
        base += 1
    return base


def _estimate_background(gray: np.ndarray) -> np.ndarray:
    k = _adaptive_kernel_size(gray.shape)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    return cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)


def _subtract_background(gray: np.ndarray, background: np.ndarray) -> np.ndarray:
    # Avoid divide-by-zero
    bg = np.clip(background.astype(np.float32), 1.0, 255.0)
    normalized = (gray.astype(np.float32) / bg) * 255.0
    return np.clip(normalized, 0, 255).astype(np.uint8)


# ---------- Stage 3: Ink isolation -------------------------------------------


def _isolate_ink(normalized_gray: np.ndarray, original_gray: np.ndarray) -> np.ndarray:
    """Three independent ink-detection methods, majority vote per pixel."""
    _, otsu = cv2.threshold(
        normalized_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )

    adaptive = cv2.adaptiveThreshold(
        normalized_gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=15, C=4,
    )

    blurred = cv2.GaussianBlur(original_gray, (21, 21), 0)
    local_contrast = cv2.subtract(blurred, original_gray)
    _, contrast_mask = cv2.threshold(local_contrast, 15, 255, cv2.THRESH_BINARY)

    votes = (
        (otsu // 255).astype(np.uint8)
        + (adaptive // 255).astype(np.uint8)
        + (contrast_mask // 255).astype(np.uint8)
    )
    return np.where(votes >= 2, 255, 0).astype(np.uint8)


# ---------- Stage 4: Cleanup -------------------------------------------------


def _remove_ruled_lines(ink_mask: np.ndarray, line_len: int = 40) -> np.ndarray:
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (line_len, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, line_len))
    h_lines = cv2.morphologyEx(ink_mask, cv2.MORPH_OPEN, h_kernel)
    v_lines = cv2.morphologyEx(ink_mask, cv2.MORPH_OPEN, v_kernel)
    return cv2.subtract(ink_mask, cv2.add(h_lines, v_lines))


def _remove_noise(ink_mask: np.ndarray, min_size: int = 50) -> np.ndarray:
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        ink_mask, connectivity=8
    )
    cleaned = np.zeros_like(ink_mask)
    for i in range(1, num_labels):
        if stats[i, cv2.CC_STAT_AREA] >= min_size:
            cleaned[labels == i] = 255
    return cleaned


def _smooth_edges(ink_mask: np.ndarray) -> np.ndarray:
    return cv2.GaussianBlur(ink_mask, (3, 3), 0)


# ---------- Stage 5: Pre-flight: already transparent? ------------------------


def _already_has_alpha(data: bytes) -> bool:
    """True if the input is RGBA/LA with >10% non-opaque pixels — meaning
    someone already cut it out. Re-running the pipeline would damage it."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGBA", "LA"):
            return False
        arr = np.array(img)
        alpha = arr[..., -1]
        non_opaque_ratio = (alpha < 250).sum() / alpha.size
        return non_opaque_ratio > 0.1
    except Exception:
        return False


# ---------- Public API -------------------------------------------------------


def remove_signature_background(
    data: bytes,
    *,
    force: bool = False,
) -> bytes | None:
    """Process a signature photo. Returns PNG bytes, or None if processing
    couldn't produce a usable result and the caller should fall back to the
    original.

    Args:
        data: Raw image bytes (PNG/JPG/etc).
        force: If True, run the pipeline even when the input already has
            meaningful alpha transparency. Default: skip (returns input
            unchanged, encoded as PNG).
    """
    if not force and _already_has_alpha(data):
        # Already cleanly cut out — just re-encode as PNG and return.
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    try:
        arr = np.frombuffer(data, np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            logger.warning("signature bg-removal: imdecode returned None")
            return None
    except Exception as exc:
        logger.warning(f"signature bg-removal: decode failed: {exc}")
        return None

    try:
        preprocessed, _scale = _preprocess(img_bgr.copy())
        gray = cv2.cvtColor(preprocessed, cv2.COLOR_BGR2GRAY)
        original_gray = cv2.cvtColor(
            cv2.resize(img_bgr, (gray.shape[1], gray.shape[0])) if gray.shape[:2] != img_bgr.shape[:2] else img_bgr,
            cv2.COLOR_BGR2GRAY,
        )

        background = _estimate_background(gray)
        normalized = _subtract_background(gray, background)

        ink_mask = _isolate_ink(normalized, original_gray)
        ink_mask = _remove_ruled_lines(ink_mask)
        ink_mask = _remove_noise(ink_mask)

        # Sanity check — did we actually find any signature?
        if int((ink_mask > 0).sum()) < 100:
            logger.warning("signature bg-removal: too few ink pixels — bailing")
            return None

        alpha = _smooth_edges(ink_mask)

        # Build RGBA from the *original* (un-upscaled, un-CLAHE'd) image so we
        # preserve ink colour. The alpha may be at the upscaled resolution, so
        # resize it to match.
        h_orig, w_orig = img_bgr.shape[:2]
        if alpha.shape[:2] != (h_orig, w_orig):
            alpha = cv2.resize(
                alpha, (w_orig, h_orig), interpolation=cv2.INTER_LINEAR
            )

        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        rgba = np.dstack([rgb, alpha])
        out = Image.fromarray(rgba, mode="RGBA")
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    except Exception as exc:
        logger.exception(f"signature bg-removal: pipeline failed: {exc}")
        return None

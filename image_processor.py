"""
Image processing pipeline — makes phone photos look like clean scans.
Auto-detects card edges, corrects perspective, enhances colors, removes background.
"""
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
import io


def _order_points(pts):
    """Order 4 points as: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]     # top-left has smallest sum
    rect[2] = pts[np.argmax(s)]     # bottom-right has largest sum
    d = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(d)]     # top-right has smallest difference
    rect[3] = pts[np.argmax(d)]     # bottom-left has largest difference
    return rect


def _find_card_contour(img_cv):
    """Find the largest rectangular contour (the card) in the image."""
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)

    # Try multiple edge detection approaches
    for thresh_method in ['adaptive', 'canny', 'otsu']:
        if thresh_method == 'adaptive':
            thresh = cv2.adaptiveThreshold(
                blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV, 11, 2
            )
        elif thresh_method == 'canny':
            thresh = cv2.Canny(blurred, 30, 150)
            thresh = cv2.dilate(thresh, None, iterations=2)
        else:
            _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Sort by area descending
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        for cnt in contours[:5]:
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
            area = cv2.contourArea(cnt)
            img_area = img_cv.shape[0] * img_cv.shape[1]

            # Must be a quadrilateral and at least 10% of image area
            if len(approx) == 4 and area > img_area * 0.10:
                return approx.reshape(4, 2)

    return None


def _perspective_transform(img_cv, pts):
    """Warp the card to a flat, straight rectangle."""
    rect = _order_points(pts.astype("float32"))
    (tl, tr, br, bl) = rect

    # Compute new image dimensions from the card's edges
    width_top = np.linalg.norm(tr - tl)
    width_bot = np.linalg.norm(br - bl)
    max_w = int(max(width_top, width_bot))

    height_left = np.linalg.norm(bl - tl)
    height_right = np.linalg.norm(br - tr)
    max_h = int(max(height_left, height_right))

    # Standard card aspect ratio is roughly 2.5 x 3.5 (5:7)
    # Use detected dimensions but enforce minimum quality
    max_w = max(max_w, 600)
    max_h = max(max_h, int(max_w * 7 / 5))

    dst = np.array([
        [0, 0],
        [max_w - 1, 0],
        [max_w - 1, max_h - 1],
        [0, max_h - 1]
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(img_cv, M, (max_w, max_h))
    return warped


def _enhance_scan(pil_img):
    """Apply color/contrast/sharpness enhancement to look like a clean scan."""
    # Boost contrast slightly
    enhancer = ImageEnhance.Contrast(pil_img)
    pil_img = enhancer.enhance(1.15)

    # Bump up color saturation
    enhancer = ImageEnhance.Color(pil_img)
    pil_img = enhancer.enhance(1.1)

    # Increase brightness just a touch
    enhancer = ImageEnhance.Brightness(pil_img)
    pil_img = enhancer.enhance(1.05)

    # Sharpen
    enhancer = ImageEnhance.Sharpness(pil_img)
    pil_img = enhancer.enhance(1.5)

    return pil_img


def _remove_background(img_cv):
    """Replace non-card background with clean white."""
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY)

    # Only remove background if it's clearly visible (edges of image)
    h, w = img_cv.shape[:2]
    border_size = min(h, w) // 20  # Check a thin border strip

    # Check if borders are mostly one color (background)
    borders = np.concatenate([
        gray[:border_size, :].flatten(),
        gray[-border_size:, :].flatten(),
        gray[:, :border_size].flatten(),
        gray[:, -border_size:].flatten(),
    ])
    border_std = np.std(borders)
    border_mean = np.mean(borders)

    # If borders are uniform (low std), it's likely background — make it white
    if border_std < 30:
        # Create mask of "not background" using flood fill from corners
        flood_mask = np.zeros((h + 2, w + 2), np.uint8)
        tolerance = 25
        corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
        result = img_cv.copy()

        for cx, cy in corners:
            cv2.floodFill(
                result, flood_mask, (cx, cy),
                (255, 255, 255),
                (tolerance, tolerance, tolerance),
                (tolerance, tolerance, tolerance),
                cv2.FLOODFILL_FIXED_RANGE
            )
        return result

    return img_cv


def process_card_scan(img_bytes: bytes) -> bytes:
    """
    Full scan processing pipeline:
    1. Detect card edges
    2. Perspective correction (straighten)
    3. Remove background
    4. Enhance colors/contrast/sharpness
    Returns processed JPEG bytes.
    """
    # Decode image
    nparr = np.frombuffer(img_bytes, np.uint8)
    img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img_cv is None:
        return img_bytes  # Can't decode — return original

    # Step 1 & 2: Find card and correct perspective
    card_pts = _find_card_contour(img_cv)
    if card_pts is not None:
        img_cv = _perspective_transform(img_cv, card_pts)

    # Step 3: Remove background
    img_cv = _remove_background(img_cv)

    # Convert to PIL for enhancement
    img_rgb = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(img_rgb)

    # Resize to standard scan quality (max 2000px)
    max_dim = 2000
    w, h = pil_img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        pil_img = pil_img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Step 4: Enhance
    pil_img = _enhance_scan(pil_img)

    # Output as high-quality JPEG
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()

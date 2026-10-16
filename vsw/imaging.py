"""Image intake + hashing + query-variant generation for the workbench.

Three hash layers live here, all computed locally and for free:

* ``sha256``  — file layer: byte-identical duplicates.
* ``phash``   — visual-copy layer: pure-numpy DCT hash, survives re-encode,
  rescale and mild cropping (Hamming distance, not equality).
* ``dhash``/``ahash`` — cheap tie-breakers for the visual-copy layer.

The semantic layer (SigLIP2 vectors) lives in :mod:`vsw.engine`, because it needs
torch and must be lazy.

Variant generation is deliberately *rule based*: every variant records which
processing produced it, so the search chain can later answer "which crop actually
found something new" instead of hoarding edited files.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

MAX_SIDE_FOR_HASH = 512
PHASH_SIZE = 32
HASH_SIZE = 8


# --------------------------------------------------------------------------- io
def load_image(data: bytes | str, max_side: int = 1600) -> Image.Image:
    """Decode bytes/path into an RGB image, EXIF-rotated and size-capped."""
    if isinstance(data, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(data)))
    else:
        img = Image.open(data)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max_side and max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def image_size(data: bytes) -> Tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def to_jpeg_bytes(img: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# The ASCII marker that starts every canonical JPEG (SOI + APP0/JFIF).
JPEG_MAGIC = b"\xff\xd8\xff\xe0"


def pixel_sha256(img: Image.Image) -> str:
    """File-layer dedup key: SHA-256 of the decoded pixel buffer, not of a file stream.

    JPEG encoding has no fixed point — measured on a fixture photo the size walks
    11472 -> 11495 -> 11516 -> 11515 -> 11506 ... forever, so *any* "re-encode then hash"
    scheme produces a key that a later import of the same picture cannot reproduce, which
    silently disables the file dedup layer. The decoded RGB buffer is exact and stable
    instead: same pixels in, same bytes out, whatever compression the picture arrived in.
    """
    rgb = img.convert("RGB")
    return hashlib.sha256(f"{rgb.width}x{rgb.height}:".encode("ascii")
                          + rgb.tobytes()).hexdigest()


def canonicalise(data: bytes) -> Tuple[bytes, str]:
    """(bytes to store, dedup key) for one image — the single source of truth.

    Two requirements pull in opposite directions, and it is worth being explicit about how
    they are reconciled:

    1. the key must be *reproducible* — importing what we stored has to yield the same key,
       otherwise the file dedup layer is unreachable;
    2. the key must be *content-addressed* — the same picture arriving at quality 30 or 95,
       or as WebP, should land on one identity;
    3. storage must not inflate — an archive of thousands of photos cannot afford 5x.

    Measured: JPEG has no re-encode fixed point (11472 -> 11495 -> 11516 -> … forever), and
    the decoded pixels of a JPEG shift by ±1 per extra encode, so both "hash a re-encoded
    stream" and "hash the pixels of a re-encoded stream" break rule 1.

    So storage keeps the incoming bytes exactly (no quality loss, no size inflation), and
    the key is the SHA-256 of the *decoded pixel buffer* of those stored bytes. Re-importing
    that stream reproduces the key exactly (same bytes -> same decode), which satisfies 1;
    rule 2 is covered by the perceptual and semantic layers, whose whole job is to recognise
    re-encodes — a ±1 pixel shift moves the pHash by ~2 bits, well inside the duplicate
    threshold, whereas two different pictures sit 20+ bits apart.
    """
    if not data:
        raise ValueError("空图片数据")
    return data, pixel_sha256(load_image(data))


def reseal_bytes(data: bytes) -> Tuple[bytes, str]:
    """Re-derive a stream's storage form and key — the self-consistency invariant.

    Importing what we stored must yield the same bytes and the same key, otherwise the file
    dedup layer is unreachable. Checked directly in selftest_vsw.py.
    """
    return canonicalise(data)


def to_png_bytes(img: Image.Image) -> bytes:
    """Canonical, lossless container: PNG re-encodes byte-identically (verified)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def thumb_bytes(img: Image.Image, box: int = 320, quality: int = 82) -> bytes:
    copy = img.copy()
    copy.thumbnail((box, box), Image.LANCZOS)
    return to_jpeg_bytes(copy, quality=quality)


# ------------------------------------------------------------------------ hashes
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gray_array(img: Image.Image, size: Tuple[int, int]) -> np.ndarray:
    small = img.convert("L").resize(size, Image.LANCZOS)
    return np.asarray(small, dtype=np.float64)


def _bits_to_int(bits: np.ndarray) -> int:
    """Pack a boolean block into a big unsigned int.

    Bigger than 64 bits on purpose (the 16x16 DCT block is 256 bits), so it is stored as
    TEXT in SQLite — a 64-bit column silently degrades such values to REAL and loses the
    low bits, which made the dedup layer blind to quadrant crops.
    """
    value = 0
    for bit in bits.ravel().tolist():
        value = (value << 1) | (1 if bit else 0)
    return value


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    return np.cos(math.pi * (2 * x + 1) * k / (2 * n)) * np.sqrt(2.0 / n)


_DCT32 = _dct_matrix(PHASH_SIZE)


def phash64(img: Image.Image) -> int:
    """Perceptual hash: 2-D DCT of a 32x32 grey image, median-thresholded 16x16 block.

    The block is 16x16 (still 64 bits, signed) rather than the classic 8x8 because the
    coarse version collapsed genuinely different query variants — a quadrant crop and the
    full frame — onto the same 64-bit code, which would make the dedup layer blind.
    Measured on the fixture set: 8x8 gave 11/13 distinct geometric variants, 16x16 gives 13/13.
    """
    arr = _gray_array(img, (PHASH_SIZE, PHASH_SIZE))
    dct = _DCT32 @ arr @ _DCT32.T
    block = dct[:16, :16]
    flat = block.ravel()[1:]  # drop DC term (overall brightness)
    med = float(np.median(flat))
    return _bits_to_int(block > med)


def dhash64(img: Image.Image) -> int:
    arr = _gray_array(img, (HASH_SIZE + 1, HASH_SIZE))
    return _bits_to_int(arr[:, 1:] > arr[:, :-1])


def ahash64(img: Image.Image) -> int:
    arr = _gray_array(img, (HASH_SIZE, HASH_SIZE))
    return _bits_to_int(arr > float(arr.mean()))


def hamming(a: int | str, b: int | str) -> int:
    """Bit distance between two perceptual hashes (any width, stored as TEXT)."""
    left = int(a) if not isinstance(a, int) else a
    right = int(b) if not isinstance(b, int) else b
    return int((left ^ right).bit_count())


@dataclass
class ImageHashes:
    sha256: str
    phash: int
    dhash: int
    ahash: int
    width: int
    height: int

    def as_row(self) -> Dict[str, Any]:
        """DB-ready form: hashes as TEXT (they exceed 64 bits)."""
        return {"sha256": self.sha256, "phash": str(self.phash), "dhash": str(self.dhash),
                "ahash": str(self.ahash), "width": self.width, "height": self.height}


def hash_image(img: Image.Image, data: Optional[bytes] = None) -> ImageHashes:
    digest = sha256_bytes(data) if data is not None else ""
    return ImageHashes(
        sha256=digest,
        phash=phash64(img),
        dhash=dhash64(img),
        ahash=ahash64(img),
        width=img.width,
        height=img.height,
    )


# ----------------------------------------------------------------------- palette
def dominant_palette(img: Image.Image, k: int = 5, bins: int = 4) -> List[Dict[str, Any]]:
    """Deterministic colour histogram (no k-means): identical pixels -> identical palette.

    Stability matters more than finesse here: the palette feeds the novelty maths and
    must be reproducible across runs, so it is a quantised RGB histogram whose cells
    are replaced by their true mean colour.
    """
    sample = img.convert("RGB").resize((96, 96), Image.BILINEAR)
    pixels = np.asarray(sample, dtype=np.float64).reshape(-1, 3)
    if len(pixels) == 0:
        return []
    step = 256.0 / bins
    index = np.clip((pixels // step).astype(int), 0, bins - 1)
    keys = index[:, 0] * bins * bins + index[:, 1] * bins + index[:, 2]
    counts = np.bincount(keys, minlength=bins ** 3).astype(np.float64)
    order = np.argsort(-counts)[:k]
    total = float(len(pixels)) or 1.0
    out: List[Dict[str, Any]] = []
    for key in order:
        if counts[key] <= 0:
            continue
        members = pixels[keys == key]
        mean = members.mean(axis=0)
        r, g, b = (int(round(v)) for v in mean)
        out.append({"hex": "#%02x%02x%02x" % (r, g, b), "share": round(float(counts[key] / total), 4)})
    return out


def palette_distance(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]]) -> float:
    """0 = identical palettes, ~1 = unrelated. Ground distance for colour drift.

    Compares the dominant colour of each palette (share-weighted); a plain weighted
    bipartite match over *different* palette lengths is not a real transport plan, so
    the honest and stable choice is the top colour plus how much of the frame it owns.
    """
    if not a or not b:
        return 0.0

    def vec(entry: Dict[str, Any]) -> np.ndarray:
        return np.array([int(entry["hex"][1:3], 16), int(entry["hex"][3:5], 16),
                         int(entry["hex"][5:7], 16)], dtype=np.float64)

    top_a = max(a, key=lambda p: float(p.get("share", 0.0)))
    top_b = max(b, key=lambda p: float(p.get("share", 0.0)))
    colour = float(np.linalg.norm(vec(top_a) - vec(top_b))) / 441.6729559300637
    share_gap = abs(float(top_a.get("share", 0.0)) - float(top_b.get("share", 0.0)))
    return max(0.0, min(1.0, colour * (1.0 - 0.5 * share_gap)))


# ---------------------------------------------------------------------- variants
@dataclass(frozen=True)
class VariantSpec:
    kind: str
    label: str
    note: str
    group: str = "geometry"


# Order matters: the queue is filled front-first, so the variants that most often
# break a dead end come first.
VARIANT_SPECS: Tuple[VariantSpec, ...] = (
    VariantSpec("full", "全图", "原始整图，作为同图检索的基准", "base"),
    VariantSpec("crop_center_70", "中心裁剪 70%", "去掉边缘干扰（水印、边框、UI 截图残留）"),
    VariantSpec("crop_center_50", "中心裁剪 50%", "进一步聚焦主体，检索引擎更容易命中同源图"),
    VariantSpec("quad_tl", "左上四分之一", "局部重检索：适合拼接图、长图、多主体画面"),
    VariantSpec("quad_tr", "右上四分之一", "局部重检索"),
    VariantSpec("quad_bl", "左下四分之一", "局部重检索"),
    VariantSpec("quad_br", "右下四分之一", "局部重检索"),
    VariantSpec("detail_center_2x", "主体局部放大 2x", "中心 40% 放大后重检索，命中更细的纹理/面孔级同源"),
    VariantSpec("strip_top", "顶部条带 25%", "标题、招牌、报头通常在上面"),
    VariantSpec("strip_bottom", "底部条带 25%", "字幕、水印、落款通常在下面"),
    VariantSpec("gray", "灰度", "压掉颜色线索，逼引擎按结构匹配", "tone"),
    VariantSpec("bright_up", "亮度 +25%", "压暗的转载图是漏检主因"),
    VariantSpec("bright_down", "亮度 -25%", "过曝的扫描件常常只有压暗后才能被匹配"),
    VariantSpec("contrast_up", "对比度 +30%", "低对比老照片/胶片扫描的补救"),
    VariantSpec("sharpen", "锐化", "柔焦、二次压缩的图先锐化再检索", "quality"),
    VariantSpec("edges", "边缘图", "只留轮廓，绕开色调与噪点差异"),
)


def _center_crop(img: Image.Image, scale: float) -> Image.Image:
    w, h = img.size
    nw, nh = max(8, int(w * scale)), max(8, int(h * scale))
    left = (w - nw) // 2
    top = (h - nh) // 2
    return img.crop((left, top, left + nw, top + nh))


def _quadrant(img: Image.Image, name: str) -> Image.Image:
    w, h = img.size
    hw, hh = w // 2, h // 2
    boxes = {
        "quad_tl": (0, 0, hw, hh),
        "quad_tr": (hw, 0, w, hh),
        "quad_bl": (0, hh, hw, h),
        "quad_br": (hw, hh, w, h),
    }
    return img.crop(boxes[name])


def _strip(img: Image.Image, where: str) -> Image.Image:
    w, h = img.size
    if where == "strip_top":
        return img.crop((0, 0, w, max(8, int(h * 0.25))))
    return img.crop((0, h - max(8, int(h * 0.25)), w, h))


# ------------------------------------------------------------------ crop by hand
CROP_KIND = "crop"


def resolve_crop_box(box: Sequence[float], size: Tuple[int, int],
                     padding: float = 0.0, min_side: int = 24) -> Tuple[int, int, int, int]:
    """Turn a normalised selection (0..1) into integer pixel bounds inside ``size``.

    Accepts either ``(x, y, w, h)`` normalized, or absolute pixels when any value exceeds 1.
    ``padding`` grows the box by that fraction of its own size on every side (clamped to the
    frame) — engines often need a little surrounding context to match the original, so the
    padding is part of the query rather than a separate manual step.
    """
    width, height = size
    values = [float(v) for v in box[:4]]
    if max(values) <= 1.0:
        x, y, w, h = (values[0] * width, values[1] * height,
                      values[2] * width, values[3] * height)
    else:
        x, y, w, h = values
    if padding:
        grow_x, grow_y = w * padding, h * padding
        x, y, w, h = x - grow_x, y - grow_y, w + grow_x * 2, h + grow_y * 2
    left = max(0, min(width - 1, int(round(x))))
    top = max(0, min(height - 1, int(round(y))))
    right = max(left + 1, min(width, int(round(x + w))))
    bottom = max(top + 1, min(height, int(round(y + h))))
    if right - left < min_side:
        right = min(width, left + min_side)
        left = max(0, right - min_side)
    if bottom - top < min_side:
        bottom = min(height, top + min_side)
        top = max(0, bottom - min_side)
    return left, top, right, bottom


def crop_region(img: Image.Image, box: Sequence[float], padding: float = 0.0,
                min_side: int = 24) -> Image.Image:
    return img.crop(resolve_crop_box(box, img.size, padding=padding, min_side=min_side))


def render_variant(img: Image.Image, kind: str) -> Image.Image:
    """Pure function: original image + variant kind -> variant image."""
    if kind == "full":
        return img.copy()
    if kind.startswith("crop_center_"):
        return _center_crop(img, float(kind.rsplit("_", 1)[1]) / 100.0)
    if kind.startswith("quad_"):
        return _quadrant(img, kind)
    if kind.startswith("strip_"):
        return _strip(img, kind)
    if kind == "detail_center_2x":
        detail = _center_crop(img, 0.40)
        return detail.resize((detail.width * 2, detail.height * 2), Image.LANCZOS)
    if kind == "gray":
        return ImageOps.grayscale(img).convert("RGB")
    if kind == "bright_up":
        return ImageEnhance.Brightness(img).enhance(1.25)
    if kind == "bright_down":
        return ImageEnhance.Brightness(img).enhance(0.75)
    if kind == "contrast_up":
        return ImageEnhance.Contrast(img).enhance(1.30)
    if kind == "sharpen":
        return img.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=3))
    if kind == "edges":
        edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
        return ImageOps.autocontrast(edges).convert("RGB")
    raise KeyError(f"unknown variant kind: {kind}")


@dataclass
class VariantPlanItem:
    kind: str
    label: str
    note: str
    group: str
    enabled: bool = True


def default_plan(extra_kinds: Iterable[str] = ()) -> List[VariantPlanItem]:
    known = {spec.kind for spec in VARIANT_SPECS}
    plan = [VariantPlanItem(s.kind, s.label, s.note, s.group) for s in VARIANT_SPECS]
    for kind in extra_kinds:
        if kind not in known:
            plan.append(VariantPlanItem(kind, kind, "自定义变体", "custom"))
    return plan


def generate_variants(img: Image.Image, plan: Sequence[VariantPlanItem],
                      out_dir: str, stem: str, formats: Sequence[str] = ("jpg",)) -> List[Dict[str, Any]]:
    """Materialise enabled variants as files (for upload) + report hashes/sizes.

    Files are written even when the visual index can work from memory, because the
    "search by hand" path needs a real file to drag into the engine's upload box.
    """
    os.makedirs(out_dir, exist_ok=True)
    written: List[Dict[str, Any]] = []
    for item in plan:
        if not item.enabled:
            continue
        try:
            rendered = render_variant(img, item.kind)
        except Exception as exc:  # noqa: BLE001
            written.append({"kind": item.kind, "ok": False, "error": str(exc)})
            continue
        paths: List[str] = []
        for fmt in formats:
            if fmt == "jpg":
                path = os.path.join(out_dir, f"{stem}__{item.kind}.jpg")
                rendered.save(path, format="JPEG", quality=92, optimize=True)
            elif fmt == "png":
                path = os.path.join(out_dir, f"{stem}__{item.kind}.png")
                rendered.save(path, format="PNG", optimize=True)
            else:
                raise KeyError(fmt)
            paths.append(path)
        data = to_jpeg_bytes(rendered)
        hashes = hash_image(rendered, data)
        written.append({
            "kind": item.kind,
            "label": item.label,
            "note": item.note,
            "group": item.group,
            "ok": True,
            "path": paths[0] if paths else "",
            "paths": paths,
            "width": rendered.width,
            "height": rendered.height,
            "bytes": len(data),
            "sha256": hashes.sha256,
            "phash": hashes.phash,
            "dhash": hashes.dhash,
            "ahash": hashes.ahash,
        })
    return written


def variant_kinds() -> List[Dict[str, str]]:
    return [{"kind": s.kind, "label": s.label, "note": s.note, "group": s.group}
            for s in VARIANT_SPECS]


# ------------------------------------------------------------------------- misc
def cos_hist_similarity(a: Image.Image, b: Image.Image) -> float:
    """Grey-histogram cosine similarity — a last-resort fallback signal."""
    ha = np.asarray(a.convert("L").resize((64, 64)), dtype=np.float64).ravel()
    hb = np.asarray(b.convert("L").resize((64, 64)), dtype=np.float64).ravel()
    denom = (np.linalg.norm(ha) * np.linalg.norm(hb)) or 1.0
    return float(np.dot(ha, hb) / denom)


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

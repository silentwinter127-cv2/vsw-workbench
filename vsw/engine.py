"""Local vision engine: SigLIP2 image/text vectors + BLIP captions.

Everything here runs on this machine against the local model checkouts
(``D:\\siglip2-base-patch16-224``, ``D:\\blip-image-captioning-base``), so the
semantic layer of dedup/ranking costs nothing and never leaves the box. If torch
or the weights are missing the engine degrades to "unavailable" and every caller
must keep working with the file/perceptual layers only.

Public surface:

* :class:`VisionEngine` — lazy singleton, thread-safe.
  * ``embed_images``/``embed_image`` -> normalised 768-d numpy vectors
  * ``embed_texts``/``embed_text`` -> same space (text -> image search)
  * ``similarity`` -> calibrated 0..1 visual similarity
  * ``tags`` -> zero-shot labels for the compact workbench vocabulary
  * ``caption`` -> one-line BLIP description (optional)
* :func:`vector_to_blob` / :func:`blob_to_vector` -> SQLite storage helpers.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

VECTOR_DIM = 768
SIGLIP_FOLDER = "siglip2-base-patch16-224"
BLIP_FOLDER = "blip-image-captioning-base"
MODEL_LABEL = "SigLIP2-base-patch16-224"

# Raw SigLIP cosine similarities collapse into a narrow high band; the workbench
# needs a spread-out number to rank with. Calibrated on the local model:
#   1.00 -> identical, 0.92 -> same scene/object restaged, 0.85 -> same theme.
SIM_FLOOR = 0.72
SIM_CEIL = 0.99

# Compact, archaeology-oriented vocabulary (Chinese label, English prompt).
VOCAB: List[Tuple[str, str]] = [
    ("人物", "a photo of a person"),
    ("合影", "a group photo of people"),
    ("人像特写", "a close-up portrait of a face"),
    ("食物", "a photo of food"),
    ("餐厅", "a photo of a restaurant or dining room"),
    ("菜单招牌", "a photo of a shop sign or menu board"),
    ("街景", "a photo of a street scene"),
    ("夜景", "a photo of a night scene"),
    ("建筑", "a photo of a building"),
    ("老照片", "an old faded vintage photograph"),
    ("黑白照片", "a black and white photograph"),
    ("胶片质感", "a grainy analog film photograph"),
    ("扫描件", "a scanned document or printed page"),
    ("海报传单", "a poster, flyer or printed advertisement"),
    ("书籍报刊", "a book, magazine or newspaper page"),
    ("文字截图", "a screenshot containing text"),
    ("界面截图", "a screenshot of a software interface"),
    ("地图", "a map or plan"),
    ("标志图案", "a logo or graphic symbol"),
    ("图表", "a chart or diagram"),
    ("插画", "an illustration or drawing"),
    ("漫画动画", "an anime or comic drawing"),
    ("玩具模型", "a toy, figurine or model"),
    ("产品商品", "a product photo of merchandise"),
    ("包装", "product packaging"),
    ("服饰", "clothing or textile"),
    ("车辆", "a photo of a vehicle"),
    ("动物", "a photo of an animal"),
    ("植物花卉", "a photo of a plant or flower"),
    ("风景", "a landscape photo of scenery"),
    ("室内空间", "a photo of an indoor space"),
    ("人群集会", "a crowd of people in public"),
    ("舞台演出", "a stage performance or concert"),
    ("运动", "a sports scene"),
    ("水域", "a photo of water, sea or lake"),
    ("雪景", "a photo of snow"),
    ("废墟破败", "a ruined or abandoned place"),
    ("旧网页像素风", "a low resolution pixelated retro graphic"),
    ("拼贴画", "a collage of several images"),
    ("二维码条形码", "a QR code or barcode"),
]


class VisionUnavailable(RuntimeError):
    """Raised when torch/transformers or the local weights cannot be used."""


def find_siglip_dir(explicit: Optional[str] = None) -> str:
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("VSW_SIGLIP_DIR")
    if env:
        candidates.append(env)
    for drive in ("D:\\", "E:\\", "C:\\"):
        candidates.append(os.path.join(drive, SIGLIP_FOLDER))
        candidates.append(os.path.join(drive, "models", SIGLIP_FOLDER))
    for path in candidates:
        if path and os.path.isfile(os.path.join(path, "model.safetensors")):
            return path
    raise VisionUnavailable(
        "找不到本地 SigLIP2 模型（需要 model.safetensors / config.json / tokenizer.json）。"
        f"已尝试：{', '.join(p for p in candidates if p)}。可用 VSW_SIGLIP_DIR 指定目录。")


def find_blip_dir(explicit: Optional[str] = None) -> str:
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("VSW_BLIP_DIR")
    if env:
        candidates.append(env)
    for drive in ("D:\\", "E:\\", "C:\\"):
        candidates.append(os.path.join(drive, BLIP_FOLDER))
    for path in candidates:
        if path and (os.path.isfile(os.path.join(path, "pytorch_model.bin"))
                     or os.path.isfile(os.path.join(path, "model.safetensors"))):
            return path
    raise VisionUnavailable("找不到本地 BLIP 描述模型目录（D:\\blip-image-captioning-base）。")


def vector_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def similarity_from_cosine(cos: float) -> float:
    """Map raw SigLIP cosine to a 0..1 workbench similarity."""
    return float(min(1.0, max(0.0, (cos - SIM_FLOOR) / (SIM_CEIL - SIM_FLOOR))))


class VisionEngine:
    """Lazy, thread-safe wrapper around SigLIP2 (+ optional BLIP captioning)."""

    _lock = threading.Lock()
    _instance: Optional["VisionEngine"] = None

    def __init__(self, model_dir: Optional[str] = None, log: Optional[Callable[[str], None]] = None,
                 threads: int = 0):
        self.log = log or (lambda msg: None)
        self.threads = threads or max(4, min(12, (os.cpu_count() or 8)))
        self._explicit_dir = model_dir
        self._dir: Optional[str] = None
        self._model = None
        self._proc = None
        self._torch = None
        self._vocab_cache: Optional[Tuple[List[str], Any]] = None
        self._blip = None
        self._blip_proc = None
        self._blip_dir: Optional[str] = None
        self._infer_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def instance(cls, model_dir: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None) -> "VisionEngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = VisionEngine(model_dir, log=log)
            return cls._instance

    @property
    def torch(self):
        self.load()
        return self._torch

    @property
    def model_dir(self) -> str:
        self.load()
        return self._dir or ""

    def available(self) -> Tuple[bool, str]:
        """Cheap pre-flight check: explicit dir must be valid, otherwise lazily load.

        An explicitly configured directory that is wrong means "semantic layer off" —
        it must not silently fall through to auto-discovery (that would make the
        degraded-mode behaviour untestable and surprising).
        """
        if self._explicit_dir:
            if not os.path.isfile(os.path.join(self._explicit_dir, "model.safetensors")):
                return False, f"指定的模型目录无效：{self._explicit_dir}"
        try:
            self.load()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, ""

    def load(self) -> None:
        if self._model is not None:
            return
        with self._infer_lock:
            if self._model is not None:
                return
            try:
                import torch  # noqa: PLC0415
                from transformers import AutoModel, AutoProcessor  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover
                raise VisionUnavailable(f"缺少 torch/transformers：{exc}") from None
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            if self._explicit_dir and not os.path.isfile(
                    os.path.join(self._explicit_dir, "model.safetensors")):
                raise VisionUnavailable(f"指定的模型目录无效：{self._explicit_dir}")
            path = find_siglip_dir(self._explicit_dir)
            t0 = time.time()
            self.log(f"加载 SigLIP2：{path}")
            model = AutoModel.from_pretrained(path, local_files_only=True).eval()
            proc = AutoProcessor.from_pretrained(path, local_files_only=True)
            torch.set_num_threads(self.threads)
            self._model, self._proc, self._torch, self._dir = model, proc, torch, path
            self.log(f"SigLIP2 就绪（{time.time() - t0:.1f}s）")

    # -------------------------------------------------------------- encoding
    @staticmethod
    def _pooled(out: Any):
        if hasattr(out, "pooler_output") and out.pooler_output is not None:
            return out.pooler_output
        if hasattr(out, "last_hidden_state") and out.last_hidden_state is not None:
            return out.last_hidden_state[:, 0]
        return out

    def embed_images(self, images: Sequence[Image.Image], batch_size: int = 8) -> np.ndarray:
        self.load()
        torch = self._torch
        chunks = []
        with torch.no_grad():
            for start in range(0, len(images), batch_size):
                batch = [img.convert("RGB") for img in images[start:start + batch_size]]
                if not batch:
                    continue
                inputs = self._proc(images=batch, return_tensors="pt")
                out = self._model.get_image_features(**inputs)
                chunks.append(torch.nn.functional.normalize(self._pooled(out), dim=-1))
        if not chunks:
            return np.zeros((0, VECTOR_DIM), dtype=np.float32)
        return torch.cat(chunks).cpu().numpy().astype(np.float32)

    def embed_image(self, image: Image.Image) -> np.ndarray:
        return self.embed_images([image])[0]

    def embed_texts(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        self.load()
        torch = self._torch
        chunks = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start:start + batch_size])
            if not batch:
                continue
            try:
                inputs = self._proc(text=batch, padding="max_length", return_tensors="pt")
                with torch.no_grad():
                    out = self._model.get_text_features(**inputs)
            except Exception:  # noqa: BLE001 - manual padding fallback
                tok = self._proc.tokenizer
                raw = [tok(t)["input_ids"] for t in batch]
                width = max(len(ids) for ids in raw)
                pad = tok.pad_token_id if tok.pad_token_id is not None else 0
                ids = torch.tensor([x + [pad] * (width - len(x)) for x in raw])
                mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in raw])
                with torch.no_grad():
                    out = self._model.get_text_features(input_ids=ids, attention_mask=mask)
            chunks.append(torch.nn.functional.normalize(self._pooled(out), dim=-1))
        if not chunks:
            return np.zeros((0, VECTOR_DIM), dtype=np.float32)
        return torch.cat(chunks).cpu().numpy().astype(np.float32)

    def embed_text(self, text: str) -> np.ndarray:
        return self.embed_texts([text])[0]

    # ------------------------------------------------------------- similarity
    @staticmethod
    def similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        if vec_a is None or vec_b is None or len(vec_a) == 0 or len(vec_b) == 0:
            return 0.0
        cos = float(np.dot(vec_a, vec_b))
        return similarity_from_cosine(cos)

    @staticmethod
    def raw_cosine(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        if vec_a is None or vec_b is None or len(vec_a) == 0 or len(vec_b) == 0:
            return 0.0
        return float(np.dot(vec_a, vec_b))

    def similarity_to_matrix(self, vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        if matrix is None or len(matrix) == 0 or vec is None or len(vec) == 0:
            return np.zeros((0,), dtype=np.float32)
        return matrix.astype(np.float32) @ vec.astype(np.float32)

    # ------------------------------------------------------------------ tags
    def vocab_matrix(self) -> Tuple[List[str], np.ndarray]:
        if self._vocab_cache is None:
            names = [name for name, _ in VOCAB]
            prompts = [prompt for _, prompt in VOCAB]
            self._vocab_cache = (names, self.embed_texts(prompts))
        return self._vocab_cache

    def tags(self, vec: np.ndarray, top_k: int = 6) -> List[Dict[str, Any]]:
        """Zero-shot labels for one image vector, relative to that image's best tag."""
        names, matrix = self.vocab_matrix()
        if vec is None or len(vec) == 0:
            return []
        scores = matrix @ vec
        order = np.argsort(-scores)[:max(1, top_k)]
        best = float(scores[order[0]]) or 1.0
        return [{"name": names[i], "score": round(float(scores[i]), 4),
                 "relative": round(float(scores[i]) / best, 3)} for i in order]

    def text_query_ranking(self, text: str, vectors: Sequence[np.ndarray]) -> List[float]:
        """Rank stored vectors against a text query (text -> image search)."""
        if not vectors:
            return []
        query = self.embed_text(text)
        matrix = np.vstack([v for v in vectors])
        cos = matrix @ query
        return [similarity_from_cosine(float(v)) for v in cos]

    # --------------------------------------------------------------- caption
    def caption_available(self) -> Tuple[bool, str]:
        try:
            find_blip_dir(self._blip_dir)
            import torch  # noqa: F401,PLC0415
            import transformers  # noqa: F401,PLC0415
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, ""

    def caption(self, image: Image.Image, max_new_tokens: int = 28) -> str:
        """One-line BLIP caption; used for metadata scoring and for humans reading lists."""
        self.load()
        if self._blip is None:
            path = find_blip_dir(self._blip_dir)
            from transformers import BlipForConditionalGeneration, BlipProcessor  # noqa: PLC0415
            self.log(f"加载 BLIP：{path}")
            self._blip_proc = BlipProcessor.from_pretrained(path, local_files_only=True)
            self._blip = BlipForConditionalGeneration.from_pretrained(path, local_files_only=True).eval()
            self._blip_dir = path
        torch = self._torch
        inputs = self._blip_proc(images=[image.convert("RGB")], return_tensors="pt")
        with torch.no_grad():
            ids = self._blip.generate(**inputs, max_new_tokens=max_new_tokens, num_beams=3)
        return self._blip_proc.decode(ids[0], skip_special_tokens=True).strip()

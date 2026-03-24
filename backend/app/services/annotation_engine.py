"""
智能标注引擎。

图片：
- 优先使用 YOLO 定位
- 若当前模型支持视觉输入，则用同一个 OpenAI 兼容模型对裁剪目标做类别细分
- 若不可用，则退回 YOLO 原始类别

视频：
- 使用 YOLO 做逐帧检测
- 使用简化的卡尔曼 + IoU 跟踪器生成稳定 track_id
- 输出预览视频与跟踪 JSON
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.core.config import settings
from app.core.logging import get_logger
from app.core.openai_compat import ensure_v1_path
from app.services.annotation_constants import (
    CLASS_NAMES_CN,
    TARGET_CLASSES,
    TRACK_COLORS,
    get_track_color,
)
from app.services.media_models import media_model_client

logger = get_logger(__name__)

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("KMP_INIT_AT_FORK", "FALSE")
os.environ.setdefault("KMP_USE_SHM", "0")


YOLO_CLASS_MAPPING: dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    8: "van",
    11: "excavator",
    12: "bulldozer",
    24: "dump truck",
    25: "tractor",
    26: "trailer",
}

BOX_COLORS: dict[str, tuple[int, int, int]] = {
    "person": (0, 255, 0),
    "car": (0, 128, 255),
    "truck": (255, 128, 0),
    "bus": (128, 0, 255),
    "van": (135, 206, 250),
    "motorcycle": (255, 0, 128),
    "bicycle": (0, 255, 255),
    "excavator": (255, 165, 0),
    "bulldozer": (0, 206, 209),
    "dump truck": (255, 20, 147),
    "tractor": (147, 112, 219),
    "trailer": (255, 215, 0),
}


def _candidate_yolo_paths() -> list[str]:
    explicit = (settings.ANNOTATION_YOLO_MODEL_PATH or "").strip()
    model_root = Path(__file__).resolve().parents[2] / "data" / "model"
    candidates = [
        explicit,
        str((model_root / "yolo26x.pt").resolve()),
        str((model_root / "yolov8x-worldv2.pt").resolve()),
    ]
    return [path for path in candidates if path]


def _peek_engine_info() -> dict[str, Any]:
    base_url = (settings.LLM_BASE_URL or "").strip()
    api_key = (settings.LLM_API_KEY or "").strip()
    model = (settings.LLM_MODEL or "").strip()
    yolo_model_path = next((path for path in _candidate_yolo_paths() if Path(path).exists()), None)
    return {
        "vision_available": bool(base_url and api_key and model),
        "yolo_available": bool(yolo_model_path),
        "vision_model": model or None,
        "yolo_model_path": yolo_model_path,
        "confidence_threshold": 0.25,
    }

def _image_to_data_url(image_path: str) -> str:
    suffix = Path(image_path).suffix.lower().lstrip(".") or "jpeg"
    with open(image_path, "rb") as file_obj:
        encoded = base64.b64encode(file_obj.read()).decode("utf-8")
    return f"data:image/{suffix};base64,{encoded}"


def _load_font(font_size: int = 16) -> ImageFont.ImageFont:
    font_paths = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/PingFangSC.ttc",
        "/System/Library/Fonts/Hiragino Sans GB W3.otf",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _normalize_bbox(bbox: list[float], width: int, height: int) -> list[float]:
    x1, y1, x2, y2 = bbox
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.0:
        nx1, ny1, nx2, ny2 = x1, y1, x2, y2
    else:
        nx1, ny1 = x1 / max(width, 1), y1 / max(height, 1)
        nx2, ny2 = x2 / max(width, 1), y2 / max(height, 1)
    return [
        round(_clamp(nx1, 0.0, 1.0), 6),
        round(_clamp(ny1, 0.0, 1.0), 6),
        round(_clamp(nx2, 0.0, 1.0), 6),
        round(_clamp(ny2, 0.0, 1.0), 6),
    ]


def _denormalize_bbox(bbox: list[float], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.0:
        x1, y1, x2, y2 = x1 * width, y1 * height, x2 * width, y2 * height
    return (
        int(_clamp(x1, 0, max(width - 1, 0))),
        int(_clamp(y1, 0, max(height - 1, 0))),
        int(_clamp(x2, 0, max(width - 1, 0))),
        int(_clamp(y2, 0, max(height - 1, 0))),
    )


def _compute_iou(box_a: tuple[float, float, float, float], box_b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - intersection
    if union <= 0:
        return 0.0
    return float(intersection / union)


@dataclass
class VisionClientConfig:
    base_url: Optional[str]
    api_key: Optional[str]
    model: Optional[str]


class _KalmanTrack:
    def __init__(self, bbox: tuple[float, float, float, float]) -> None:
        self.id = -1
        self.hits = 0
        self.age = 0
        self.time_since_update = 0
        self.det_class = "unknown"
        self.det_class_id = -1
        self.det_confidence = 0.0

        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        self.state = np.array([cx, cy, width, height, 0.0, 0.0], dtype=np.float32)

        self.F = np.eye(6, dtype=np.float32)
        self.F[0, 4] = 1.0
        self.F[1, 5] = 1.0
        self.H = np.zeros((4, 6), dtype=np.float32)
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0
        self.H[2, 2] = 1.0
        self.H[3, 3] = 1.0
        self.P = np.eye(6, dtype=np.float32) * 10.0
        self.Q = np.eye(6, dtype=np.float32) * 0.1
        self.Q[4, 4] = 0.5
        self.Q[5, 5] = 0.5
        self.R = np.eye(4, dtype=np.float32)

    def predict(self) -> tuple[float, float, float, float]:
        self.age += 1
        self.time_since_update += 1
        self.state = self.F @ self.state
        self.P = self.F @ self.P @ self.F.T + self.Q
        cx, cy, width, height = self.state[:4]
        return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)

    def update(self, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        self.time_since_update = 0
        self.hits += 1
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        observation = np.array([cx, cy, width, height], dtype=np.float32)
        residual = observation - self.H @ self.state
        s_matrix = self.H @ self.P @ self.H.T + self.R
        k_gain = self.P @ self.H.T @ np.linalg.inv(s_matrix)
        self.state = self.state + k_gain @ residual
        self.P = (np.eye(6, dtype=np.float32) - k_gain @ self.H) @ self.P
        return self.predict()

    def bbox(self) -> tuple[float, float, float, float]:
        cx, cy, width, height = self.state[:4]
        return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)


class MultiObjectTracker:
    def __init__(self, *, iou_threshold: float = 0.3, max_age: int = 30, min_hits: int = 1) -> None:
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self.min_hits = min_hits
        self._tracks: list[_KalmanTrack] = []
        self._next_id = 0

    def update(self, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not detections:
            for track in self._tracks:
                track.predict()
            self._tracks = [track for track in self._tracks if track.time_since_update <= self.max_age]
            return []

        for track in self._tracks:
            track.predict()

        matches, unmatched_det_indices, unmatched_track_indices = self._match(detections)

        for det_index, track_index in matches:
            track = self._tracks[track_index]
            detection = detections[det_index]
            bbox_tuple = tuple(float(value) for value in detection["bbox"])
            track.det_class = str(detection.get("class") or "unknown")
            track.det_class_id = int(detection.get("class_id", -1))
            track.det_confidence = float(detection.get("confidence", 0.0))
            track.update(bbox_tuple)

        for det_index in unmatched_det_indices:
            detection = detections[det_index]
            track = _KalmanTrack(tuple(float(value) for value in detection["bbox"]))
            track.id = self._next_id
            track.hits = 1
            track.det_class = str(detection.get("class") or "unknown")
            track.det_class_id = int(detection.get("class_id", -1))
            track.det_confidence = float(detection.get("confidence", 0.0))
            self._next_id += 1
            self._tracks.append(track)

        self._tracks = [
            track
            for index, track in enumerate(self._tracks)
            if track.time_since_update <= self.max_age or index not in unmatched_track_indices
        ]

        tracked: list[dict[str, Any]] = []
        for track in self._tracks:
            if track.time_since_update == 0 and track.hits >= self.min_hits:
                x1, y1, x2, y2 = track.bbox()
                tracked.append(
                    {
                        "track_id": track.id,
                        "class": track.det_class,
                        "class_id": track.det_class_id,
                        "confidence": track.det_confidence,
                        "bbox": [float(x1), float(y1), float(x2), float(y2)],
                        "age": track.age,
                        "hits": track.hits,
                    }
                )
        return tracked

    def _match(self, detections: list[dict[str, Any]]) -> tuple[list[tuple[int, int]], list[int], list[int]]:
        if not self._tracks:
            return [], list(range(len(detections))), []

        scores: list[tuple[float, int, int]] = []
        for det_index, detection in enumerate(detections):
            det_box = tuple(float(value) for value in detection["bbox"])
            for track_index, track in enumerate(self._tracks):
                iou = _compute_iou(det_box, track.bbox())
                if iou >= self.iou_threshold:
                    scores.append((iou, det_index, track_index))

        scores.sort(reverse=True)
        matched_detections: set[int] = set()
        matched_tracks: set[int] = set()
        matches: list[tuple[int, int]] = []
        for _, det_index, track_index in scores:
            if det_index in matched_detections or track_index in matched_tracks:
                continue
            matched_detections.add(det_index)
            matched_tracks.add(track_index)
            matches.append((det_index, track_index))

        unmatched_det_indices = [index for index in range(len(detections)) if index not in matched_detections]
        unmatched_track_indices = [index for index in range(len(self._tracks)) if index not in matched_tracks]
        return matches, unmatched_det_indices, unmatched_track_indices


class AutoAnnotationEngine:
    def __init__(self, confidence_threshold: float = 0.25) -> None:
        self.confidence_threshold = confidence_threshold
        self._client = None
        self._vision_config = self._load_vision_config()
        self._yolo_model = None
        self._yolo_model_path = None
        self._init_openai_client()
        self._init_yolo_model()

    def _load_vision_config(self) -> VisionClientConfig:
        return VisionClientConfig(
            base_url=(settings.LLM_BASE_URL or "").strip() or None,
            api_key=(settings.LLM_API_KEY or "").strip() or None,
            model=(settings.LLM_MODEL or "").strip() or None,
        )

    def _init_openai_client(self) -> None:
        if not (self._vision_config.base_url and self._vision_config.api_key and self._vision_config.model):
            return
        try:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=ensure_v1_path(self._vision_config.base_url),
                api_key=self._vision_config.api_key,
            )
        except Exception as exc:
            logger.warning("初始化视觉模型客户端失败: %s", exc)
            self._client = None

    def _init_yolo_model(self) -> None:
        try:
            from ultralytics import YOLO
        except Exception as exc:
            logger.warning("ultralytics 不可用: %s", exc)
            return

        for candidate in _candidate_yolo_paths():
            if not Path(candidate).exists():
                continue
            try:
                model = YOLO(candidate)
                model.fuse()
                self._yolo_model = model
                self._yolo_model_path = candidate
                return
            except Exception as exc:
                logger.warning("加载 YOLO 模型失败: path=%s error=%s", candidate, exc)

    def is_available(self) -> bool:
        return self._yolo_model is not None or self._client is not None

    def get_engine_info(self) -> dict[str, Any]:
        return {
            "vision_available": self._client is not None,
            "yolo_available": self._yolo_model is not None,
            "vision_model": self._vision_config.model,
            "yolo_model_path": self._yolo_model_path,
            "confidence_threshold": self.confidence_threshold,
        }

    def detect_image(self, image_path: str, *, preview_dir: Optional[str] = None) -> list[dict[str, Any]]:
        image = Image.open(image_path).convert("RGB")
        width, height = image.size

        yolo_detections = self._detect_image_with_yolo(image_path)
        if yolo_detections:
            detections = self._refine_detections_with_vision(image_path, yolo_detections, width, height)
        else:
            detections = self._detect_image_with_vision(image_path, width, height)

        if preview_dir:
            self.save_image_preview(image_path, detections, preview_dir=preview_dir)
        return detections

    def describe_image(self, image_path: str, *, cache_path: Optional[str] = None) -> str:
        if cache_path and Path(cache_path).exists():
            return Path(cache_path).read_text(encoding="utf-8").strip()

        description = ""
        if self._client is not None and self._vision_config.model:
            prompt = (
                "请用中文简洁描述图片中的场景、主要目标和动作，2到3句话，不要输出 JSON。"
            )
            description = self._vision_prompt([{"type": "image", "path": image_path}], prompt, max_tokens=256)

        if not description:
            description = media_model_client.caption_image(image_path) or ""

        if cache_path and description:
            Path(cache_path).write_text(description, encoding="utf-8")
        return description

    def describe_video(self, video_path: str, *, cache_path: Optional[str] = None, max_frames: int = 3) -> str:
        if cache_path and Path(cache_path).exists():
            return Path(cache_path).read_text(encoding="utf-8").strip()

        sampled_frames = self._sample_video_frames(video_path, max_frames=max_frames)
        description = ""
        temp_paths: list[str] = []
        try:
            if self._client is not None and self._vision_config.model and sampled_frames:
                inputs: list[dict[str, str]] = []
                for frame in sampled_frames:
                    temp_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    temp_file.close()
                    Image.fromarray(frame).save(temp_file.name, quality=85)
                    temp_paths.append(temp_file.name)
                    inputs.append({"type": "image", "path": temp_file.name})
                description = self._vision_prompt(
                    inputs,
                    "观察这些视频关键帧，用中文描述场景、主要目标、动作与事件，2到3句话，不要输出 JSON。",
                    max_tokens=256,
                )

            if not description and sampled_frames:
                captions: list[str] = []
                for frame in sampled_frames:
                    temp_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    temp_file.close()
                    Image.fromarray(frame).save(temp_file.name, quality=85)
                    temp_paths.append(temp_file.name)
                    caption = media_model_client.caption_image(temp_file.name)
                    if caption:
                        captions.append(caption)
                description = "；".join(captions[:3])
        finally:
            for temp_path in temp_paths:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        if cache_path and description:
            Path(cache_path).write_text(description, encoding="utf-8")
        return description

    def detect_video(
        self,
        video_path: str,
        *,
        preview_video_path: Optional[str] = None,
        tracking_json_path: Optional[str] = None,
        use_tracking: bool = True,
        frame_interval: int = 1,
        detect_size: int = 640,
    ) -> dict[str, Any]:
        if self._yolo_model is None:
            raise RuntimeError("YOLO 模型不可用，无法执行视频标注")

        capture = cv2.VideoCapture(video_path)
        if not capture.isOpened():
            raise RuntimeError(f"无法打开视频: {video_path}")

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        tracker = MultiObjectTracker() if use_tracking else None
        preview_writer = None
        if preview_video_path:
            Path(preview_video_path).parent.mkdir(parents=True, exist_ok=True)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            preview_writer = cv2.VideoWriter(preview_video_path, fourcc, fps or 25.0, (width, height))

        frame_results: dict[str, Any] = {}
        peak_tracks = 0
        frame_index = 0
        try:
            while True:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break

                if frame_index % max(frame_interval, 1) == 0 or frame_index == 0:
                    detections = self._detect_frame(frame, detect_size=detect_size)
                else:
                    detections = []

                tracks = tracker.update(detections) if tracker else []
                peak_tracks = max(peak_tracks, len(tracks))

                if tracker:
                    frame_results[str(frame_index)] = {
                        "detections": detections,
                        "tracks": tracks,
                        "total_tracks": len(tracks),
                    }
                else:
                    frame_results[str(frame_index)] = detections

                if preview_writer is not None:
                    render_frame = frame.copy()
                    if tracker:
                        render_frame = self.draw_tracking_preview(render_frame, tracks, frame_index=frame_index)
                    else:
                        render_frame = self.draw_annotation_preview(render_frame, detections, show_conf=True)
                    preview_writer.write(render_frame)

                frame_index += 1
        finally:
            capture.release()
            if preview_writer is not None:
                preview_writer.release()

        if tracking_json_path:
            Path(tracking_json_path).parent.mkdir(parents=True, exist_ok=True)
            with open(tracking_json_path, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {
                        "metadata": {
                            "fps": fps,
                            "width": width,
                            "height": height,
                            "total_frames": total_frames,
                            "has_tracking": bool(tracker),
                        },
                        "frames": frame_results,
                    },
                    file_obj,
                    ensure_ascii=False,
                    indent=2,
                )

        return {
            "fps": fps,
            "width": width,
            "height": height,
            "total_frames": total_frames,
            "peak_tracks": peak_tracks,
            "frames": frame_results,
        }

    def save_image_preview(self, image_path: str, detections: list[dict[str, Any]], *, preview_dir: str) -> dict[str, str]:
        preview_root = Path(preview_dir)
        preview_root.mkdir(parents=True, exist_ok=True)
        stem = Path(image_path).stem
        preview_image_path = preview_root / f"{stem}_preview.jpg"
        preview_result_path = preview_root / f"{stem}_result.json"

        frame = cv2.imread(image_path)
        if frame is not None:
            rendered = self.draw_annotation_preview(frame, detections, show_conf=True)
            cv2.imwrite(str(preview_image_path), rendered)

        with open(preview_result_path, "w", encoding="utf-8") as file_obj:
            json.dump(detections, file_obj, ensure_ascii=False, indent=2)

        return {
            "preview_image_path": str(preview_image_path.resolve()),
            "preview_result_path": str(preview_result_path.resolve()),
        }

    def draw_annotation_preview(
        self,
        frame: np.ndarray,
        detections: list[dict[str, Any]],
        *,
        show_conf: bool = False,
        thickness: int = 2,
    ) -> np.ndarray:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        draw = ImageDraw.Draw(image, "RGBA")
        font = _load_font(16)
        height, width = frame.shape[:2]

        for detection in detections:
            bbox = detection.get("bbox") or [0, 0, 0, 0]
            x1, y1, x2, y2 = _denormalize_bbox([float(value) for value in bbox], width, height)
            if x2 <= x1 or y2 <= y1:
                continue
            cls = str(detection.get("class") or "unknown")
            confidence = float(detection.get("confidence", 0.0))
            manual = bool(detection.get("manual"))
            label = f"[{'手' if manual else '自'}] {CLASS_NAMES_CN.get(cls, cls)}"
            if show_conf:
                label = f"{label} {confidence:.2f}"
            color = BOX_COLORS.get(cls, (255, 128, 0))

            draw.rectangle([x1, y1, x2, y2], outline=(*color, 255), width=max(1, int(thickness)))
            text_box = draw.textbbox((0, 0), label, font=font)
            text_w = text_box[2] - text_box[0]
            text_h = text_box[3] - text_box[1]
            tx1 = x1
            ty1 = max(0, y1 - text_h - 8)
            tx2 = min(width - 1, tx1 + text_w + 8)
            ty2 = min(height - 1, ty1 + text_h + 8)
            draw.rectangle([tx1, ty1, tx2, ty2], fill=(*color, 200))
            draw.text((tx1 + 4, ty1 + 4), label, font=font, fill=(255, 255, 255, 255))

        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    def draw_tracking_preview(self, frame: np.ndarray, tracks: list[dict[str, Any]], *, frame_index: int) -> np.ndarray:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        draw = ImageDraw.Draw(image, "RGBA")
        font = _load_font(16)
        height, width = frame.shape[:2]

        draw.text((10, 10), f"帧: {frame_index}", font=font, fill=(255, 255, 255, 255))
        for track in tracks:
            bbox = track.get("bbox") or [0, 0, 0, 0]
            x1, y1, x2, y2 = _denormalize_bbox([float(value) for value in bbox], width, height)
            if x2 <= x1 or y2 <= y1:
                continue
            track_id = int(track.get("track_id", 0))
            cls = str(track.get("class") or "unknown")
            confidence = float(track.get("confidence", 0.0))
            color = get_track_color(track_id)
            label = f"#{track_id} {CLASS_NAMES_CN.get(cls, cls)}"
            if confidence > 0:
                label = f"{label} {confidence:.2f}"
            draw.rectangle([x1, y1, x2, y2], outline=(*color, 255), width=3)
            text_box = draw.textbbox((0, 0), label, font=font)
            text_w = text_box[2] - text_box[0]
            text_h = text_box[3] - text_box[1]
            tx1 = x1
            ty1 = max(0, y1 - text_h - 8)
            tx2 = min(width - 1, tx1 + text_w + 8)
            ty2 = min(height - 1, ty1 + text_h + 8)
            draw.rectangle([tx1, ty1, tx2, ty2], fill=(*color, 200))
            draw.text((tx1 + 4, ty1 + 4), label, font=font, fill=(255, 255, 255, 255))
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2
            draw.ellipse([center_x - 4, center_y - 4, center_x + 4, center_y + 4], fill=(*color, 255))

        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    def export_yolo_labels(self, output_path: str, annotations: list[dict[str, Any]], width: int, height: int) -> None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for annotation in annotations:
            cls = str(annotation.get("class") or "")
            if cls not in TARGET_CLASSES:
                continue
            x1, y1, x2, y2 = _denormalize_bbox([float(value) for value in annotation.get("bbox", [0, 0, 0, 0])], width, height)
            if x2 <= x1 or y2 <= y1:
                continue
            center_x = ((x1 + x2) / 2.0) / max(width, 1)
            center_y = ((y1 + y2) / 2.0) / max(height, 1)
            box_w = (x2 - x1) / max(width, 1)
            box_h = (y2 - y1) / max(height, 1)
            lines.append(
                f"{TARGET_CLASSES[cls]} {center_x:.6f} {center_y:.6f} {box_w:.6f} {box_h:.6f}"
            )
        output.write_text("\n".join(lines), encoding="utf-8")

    def _detect_image_with_yolo(self, image_path: str) -> list[dict[str, Any]]:
        if self._yolo_model is None:
            return []
        try:
            image = Image.open(image_path).convert("RGB")
            width, height = image.size
            results = self._yolo_model(image_path, conf=self.confidence_threshold, verbose=False)
            if not results or results[0].boxes is None:
                return []
            detections: list[dict[str, Any]] = []
            for box in results[0].boxes:
                class_id = int(box.cls.item())
                cls = YOLO_CLASS_MAPPING.get(class_id)
                if not cls:
                    continue
                xywhn = box.xywhn.cpu().numpy()[0]
                cx, cy, box_w, box_h = [float(value) for value in xywhn]
                bbox = [
                    cx - box_w / 2,
                    cy - box_h / 2,
                    cx + box_w / 2,
                    cy + box_h / 2,
                ]
                detections.append(
                    {
                        "class": cls,
                        "class_id": TARGET_CLASSES[cls],
                        "confidence": float(box.conf.item()),
                        "bbox": _normalize_bbox(bbox, width, height),
                        "manual": False,
                    }
                )
            return detections
        except Exception as exc:
            logger.warning("YOLO 图片检测失败: path=%s error=%s", image_path, exc)
            return []

    def _detect_image_with_vision(self, image_path: str, width: int, height: int) -> list[dict[str, Any]]:
        if self._client is None or self._vision_config.model is None:
            return []
        prompt = """
你是一个目标检测助手。请识别图片中的目标，只允许以下类别：
person, car, truck, bus, van, motorcycle, bicycle, excavator, bulldozer, dump truck, tractor, trailer。

输出要求：
1. 仅输出 JSON 数组。
2. 每个元素格式为 {"class":"car","confidence":0.85,"bbox":[x1,y1,x2,y2]}。
3. bbox 使用 0 到 1 的归一化坐标。
4. 不允许输出类别列表之外的标签。
"""
        raw = self._vision_prompt([{"type": "image", "path": image_path}], prompt, max_tokens=2048)
        return self._parse_detection_json(raw, width=width, height=height)

    def _refine_detections_with_vision(
        self,
        image_path: str,
        yolo_detections: list[dict[str, Any]],
        width: int,
        height: int,
        crop_size: int = 384,
    ) -> list[dict[str, Any]]:
        if self._client is None or self._vision_config.model is None:
            return yolo_detections

        image = Image.open(image_path).convert("RGB")
        crops: list[Image.Image] = []
        for detection in yolo_detections:
            x1, y1, x2, y2 = _denormalize_bbox([float(v) for v in detection["bbox"]], width, height)
            pad = 10
            x1 = max(0, x1 - pad)
            y1 = max(0, y1 - pad)
            x2 = min(width, x2 + pad)
            y2 = min(height, y2 + pad)
            crop = image.crop((x1, y1, x2, y2))
            if crop.width > 0 and crop.height > 0:
                crops.append(crop.resize((crop_size, crop_size), Image.LANCZOS))

        if not crops:
            return yolo_detections

        cols = min(4, len(crops))
        rows = math.ceil(len(crops) / cols)
        grid = Image.new("RGB", (cols * crop_size, rows * crop_size), (255, 255, 255))
        draw = ImageDraw.Draw(grid)
        font = _load_font(24)

        for index, crop in enumerate(crops):
            row = index // cols
            col = index % cols
            offset = (col * crop_size, row * crop_size)
            grid.paste(crop, offset)
            draw.text((offset[0] + 8, offset[1] + 8), f"#{index}", font=font, fill=(255, 0, 0))

        temp_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_file.close()
        try:
            grid.save(temp_file.name, quality=92)
            prompt = """
请识别这张拼图里每个编号框所对应的类别，只允许以下类别：
person, car, truck, bus, van, motorcycle, bicycle, excavator, bulldozer, dump truck, tractor, trailer。

仅输出 JSON 数组，例如：
[{"index":0,"class":"car"},{"index":1,"class":"truck"}]
"""
            raw = self._vision_prompt([{"type": "image", "path": temp_file.name}], prompt, max_tokens=512)
            match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", raw)
            if not match:
                return yolo_detections
            payload = json.loads(match.group(0))
            index_to_class = {
                int(item["index"]): str(item["class"])
                for item in payload
                if isinstance(item, dict)
                and str(item.get("class") or "") in TARGET_CLASSES
                and str(item.get("index", "")).isdigit()
            }
            refined: list[dict[str, Any]] = []
            for index, detection in enumerate(yolo_detections):
                cls = index_to_class.get(index, detection["class"])
                refined.append(
                    {
                        **detection,
                        "class": cls,
                        "class_id": TARGET_CLASSES.get(cls, detection.get("class_id")),
                    }
                )
            return refined
        except Exception as exc:
            logger.warning("视觉模型裁剪分类失败，回退 YOLO 标签: %s", exc)
            return yolo_detections
        finally:
            try:
                os.unlink(temp_file.name)
            except Exception:
                pass

    def _vision_prompt(
        self,
        inputs: list[dict[str, str]],
        prompt: str,
        *,
        max_tokens: int,
    ) -> str:
        if self._client is None or self._vision_config.model is None:
            return ""

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for item in inputs:
            if item.get("type") != "image":
                continue
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _image_to_data_url(item["path"])},
                }
            )

        try:
            response = self._client.chat.completions.create(
                model=self._vision_config.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("调用视觉模型失败: %s", exc)
            return ""

    def _parse_detection_json(self, raw: str, *, width: int, height: int) -> list[dict[str, Any]]:
        if not raw:
            return []
        match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", raw)
        payload_text = match.group(0) if match else raw
        try:
            payload = json.loads(payload_text)
        except Exception:
            return []

        detections: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            cls = str(item.get("class") or "")
            bbox = item.get("bbox")
            if cls not in TARGET_CLASSES or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            try:
                normalized = _normalize_bbox([float(value) for value in bbox], width, height)
            except Exception:
                continue
            detections.append(
                {
                    "class": cls,
                    "class_id": TARGET_CLASSES[cls],
                    "confidence": float(item.get("confidence", 0.5)),
                    "bbox": normalized,
                    "manual": False,
                }
            )
        return detections

    def _sample_video_frames(self, video_path: str, *, max_frames: int) -> list[np.ndarray]:
        capture = cv2.VideoCapture(video_path)
        if not capture.isOpened():
            return []
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            capture.release()
            return []

        indices = sorted({0, max(total_frames // 2, 0), max(total_frames - 1, 0)})
        indices = indices[:max_frames]
        frames: list[np.ndarray] = []
        try:
            for frame_index in indices:
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = capture.read()
                if not ok or frame is None:
                    continue
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        finally:
            capture.release()
        return frames

    def _detect_frame(self, frame_bgr: np.ndarray, *, detect_size: int) -> list[dict[str, Any]]:
        if self._yolo_model is None:
            return []

        height, width = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        scale = min(detect_size / max(height, 1), detect_size / max(width, 1))
        if scale < 1.0:
            resized = cv2.resize(rgb, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_LINEAR)
        else:
            resized = rgb

        try:
            results = self._yolo_model(resized, conf=self.confidence_threshold, verbose=False)
        except Exception as exc:
            logger.warning("YOLO 帧检测失败: %s", exc)
            return []

        if not results or results[0].boxes is None:
            return []

        detections: list[dict[str, Any]] = []
        resized_height, resized_width = resized.shape[:2]
        for box in results[0].boxes:
            class_id = int(box.cls.item())
            cls = YOLO_CLASS_MAPPING.get(class_id)
            if not cls:
                continue
            xywhn = box.xywhn.cpu().numpy()[0]
            center_x, center_y, box_w, box_h = [float(value) for value in xywhn]
            x1 = (center_x - box_w / 2) * resized_width
            y1 = (center_y - box_h / 2) * resized_height
            x2 = (center_x + box_w / 2) * resized_width
            y2 = (center_y + box_h / 2) * resized_height
            if scale < 1.0:
                x1 /= scale
                y1 /= scale
                x2 /= scale
                y2 /= scale
            detections.append(
                {
                    "class": cls,
                    "class_id": TARGET_CLASSES[cls],
                    "confidence": float(box.conf.item()),
                    "bbox": [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)],
                    "manual": False,
                }
            )
        return detections


_annotation_engine_singleton: Optional[AutoAnnotationEngine] = None


def get_auto_annotation_engine() -> AutoAnnotationEngine:
    global _annotation_engine_singleton
    if _annotation_engine_singleton is None:
        _annotation_engine_singleton = AutoAnnotationEngine()
    return _annotation_engine_singleton


def get_annotation_engine_boot_info() -> dict[str, Any]:
    return _peek_engine_info()

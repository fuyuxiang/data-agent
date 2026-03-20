"""
智能标注常量与共享配置。
"""
from __future__ import annotations

from pathlib import Path


TARGET_CLASSES: dict[str, int] = {
    "person": 0,
    "car": 2,
    "truck": 7,
    "bus": 5,
    "van": 8,
    "motorcycle": 3,
    "bicycle": 1,
    "excavator": 11,
    "bulldozer": 12,
    "dump truck": 24,
    "tractor": 25,
    "trailer": 26,
}

CLASS_NAMES_CN: dict[str, str] = {
    "person": "人",
    "car": "汽车",
    "truck": "卡车",
    "bus": "公交车",
    "van": "面包车",
    "motorcycle": "摩托车",
    "bicycle": "自行车",
    "excavator": "挖掘机",
    "bulldozer": "推土机",
    "dump truck": "卸土车",
    "tractor": "拖拉机",
    "trailer": "挂车",
}

TRACK_COLORS: list[tuple[int, int, int]] = [
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 255),
    (255, 128, 0),
    (128, 255, 0),
    (0, 128, 255),
    (255, 0, 128),
    (128, 0, 255),
    (0, 255, 128),
    (255, 128, 128),
    (128, 255, 128),
    (128, 128, 255),
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm"}

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_ROOT = Path("/Users/fuyuxiang/Desktop/多模态检索-铁塔")


def get_track_color(track_id: int) -> tuple[int, int, int]:
    return TRACK_COLORS[track_id % len(TRACK_COLORS)]


def get_default_source_dir(media_type: str) -> str:
    if media_type == "video":
        candidate = REFERENCE_ROOT / "warning_file"
    else:
        candidate = REFERENCE_ROOT / "warning_img"
    return str(candidate) if candidate.exists() else ""


def get_default_output_dir() -> str:
    return str((BACKEND_ROOT / "data" / "annotation_exports" / "auto").resolve())

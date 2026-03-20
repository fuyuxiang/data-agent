"""
智能标注业务编排服务。
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import cv2

from app.core.logging import get_logger
from app.services.annotation_constants import (
    CLASS_NAMES_CN,
    TARGET_CLASSES,
    get_default_output_dir,
    get_default_source_dir,
)
from app.services.annotation_engine import get_annotation_engine_boot_info, get_auto_annotation_engine
from app.services.annotation_store import (
    build_item_artifact_paths,
    build_item_id,
    load_session,
    load_session_for_source,
    normalize_input_path,
    sanitize_session,
    save_session,
    scan_source_files,
)

logger = get_logger(__name__)


class AnnotationService:
    @property
    def engine(self):
        return get_auto_annotation_engine()

    def get_meta(self) -> dict[str, Any]:
        classes = [
            {
                "key": key,
                "class_name": key,
                "label": CLASS_NAMES_CN.get(key, key),
                "class_id": class_id,
            }
            for key, class_id in TARGET_CLASSES.items()
        ]
        return {
            "classes": classes,
            "engine": get_annotation_engine_boot_info(),
            "defaults": {
                "image_dir": get_default_source_dir("image"),
                "video_dir": get_default_source_dir("video"),
                "output_dir": get_default_output_dir(),
            },
        }

    def restore_session(self, workspace_id: int, media_type: str, source_dir: str) -> Optional[dict[str, Any]]:
        source_dir = normalize_input_path(source_dir)
        if not source_dir:
            return None
        session = load_session_for_source(workspace_id, media_type, source_dir)
        if not session:
            return None
        try:
            current_files = scan_source_files(source_dir, media_type)
        except Exception:
            return None
        if [item.get("source_path") for item in session.get("items", [])] != current_files:
            return None
        return sanitize_session(session)

    def prepare_session(
        self,
        *,
        workspace_id: int,
        media_type: str,
        source_dir: str,
        output_dir: Optional[str] = None,
        use_tracking: bool = True,
        frame_interval: int = 1,
        detect_size: int = 640,
        force_reprocess: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        normalized_source_dir = normalize_input_path(source_dir)
        if not normalized_source_dir:
            raise ValueError("请输入目录路径")

        files = scan_source_files(normalized_source_dir, media_type)
        if not files:
            raise ValueError("未找到可处理的文件")

        normalized_output_dir = normalize_input_path(output_dir or get_default_output_dir())
        session_id = build_session_id(workspace_id, media_type, normalized_source_dir)
        existing = load_session(session_id)
        if existing and not force_reprocess:
            existing_files = [item.get("source_path") for item in existing.get("items", [])]
            if existing_files == files and existing.get("status") in {"ready", "processing", "pending"}:
                existing["output_dir"] = normalized_output_dir
                existing["options"] = {
                    "use_tracking": bool(use_tracking),
                    "frame_interval": max(int(frame_interval), 1),
                    "detect_size": max(int(detect_size), 160),
                }
                saved = save_session(existing)
                return sanitize_session(saved), True

        items: list[dict[str, Any]] = []
        for order_index, file_path in enumerate(files):
            path_obj = Path(file_path)
            artifact_paths = build_item_artifact_paths(session_id, build_item_id(file_path), path_obj.stem)
            items.append(
                {
                    "id": build_item_id(file_path),
                    "order_index": order_index,
                    "file_name": path_obj.name,
                    "source_path": file_path,
                    "artifact_dir": str(Path(artifact_paths["preview_image_path"]).parent),
                    "preview_image_path": artifact_paths["preview_image_path"],
                    "preview_result_path": artifact_paths["preview_result_path"],
                    "preview_video_path": artifact_paths["preview_video_path"],
                    "tracking_json_path": artifact_paths["tracking_json_path"],
                    "description_path": artifact_paths["description_path"],
                    "status": "pending",
                    "error_message": None,
                    "description": "",
                    "annotations": [],
                    "stats": {},
                    "width": None,
                    "height": None,
                }
            )

        session = {
            "id": session_id,
            "workspace_id": workspace_id,
            "media_type": media_type,
            "source_dir": normalized_source_dir,
            "output_dir": normalized_output_dir,
            "status": "pending",
            "current_index": 0,
            "progress": {
                "total": len(items),
                "processed": 0,
                "failed": 0,
                "percent": 0.0,
                "message": "等待开始",
            },
            "options": {
                "use_tracking": bool(use_tracking),
                "frame_interval": max(int(frame_interval), 1),
                "detect_size": max(int(detect_size), 160),
            },
            "engine": get_annotation_engine_boot_info(),
            "items": items,
        }
        return sanitize_session(save_session(session)), False

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = load_session(session_id)
        if not session:
            raise ValueError("标注会话不存在")
        return sanitize_session(session)

    def update_cursor(self, session_id: str, current_index: int) -> dict[str, Any]:
        session = self._require_session(session_id)
        if not session.get("items"):
            raise ValueError("当前会话没有文件")
        session["current_index"] = max(0, min(int(current_index), len(session["items"]) - 1))
        return sanitize_session(save_session(session))

    def update_annotations(self, session_id: str, item_id: str, annotations: list[dict[str, Any]]) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.get("media_type") != "image":
            raise ValueError("视频标注结果不支持手工编辑")

        item = self._require_item(session, item_id)
        item["annotations"] = [self._normalize_annotation_payload(annotation) for annotation in annotations]
        item["status"] = "ready"
        item["stats"] = self._summarize_image_annotations(item["annotations"])
        saved = save_session(session)
        return self._sanitize_item(self._require_item(saved, item_id))

    def export_item(self, session_id: str, item_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.get("media_type") != "image":
            raise ValueError("视频暂不支持导出 YOLO 标签")

        item = self._require_item(session, item_id)
        output_dir = Path(session.get("output_dir") or get_default_output_dir())
        output_path = output_dir / f"{Path(item['file_name']).stem}.txt"
        image = cv2.imread(item["source_path"])
        if image is None:
            raise ValueError("图片读取失败")
        height, width = image.shape[:2]
        self.engine.export_yolo_labels(str(output_path), item.get("annotations", []), width, height)
        return {
            "message": "保存成功",
            "saved_paths": [str(output_path.resolve())],
        }

    def export_session(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.get("media_type") != "image":
            raise ValueError("视频暂不支持导出 YOLO 标签")

        saved_paths: list[str] = []
        for item in session.get("items", []):
            if not item.get("annotations"):
                continue
            result = self.export_item(session_id, item["id"])
            saved_paths.extend(result["saved_paths"])
        return {
            "message": f"已保存 {len(saved_paths)} 个标签文件",
            "saved_paths": saved_paths,
        }

    def get_item_file_path(self, session_id: str, item_id: str, file_kind: str) -> tuple[str, str]:
        session = self._require_session(session_id)
        item = self._require_item(session, item_id)

        if file_kind == "source":
            path = item["source_path"]
        elif file_kind == "preview":
            path = item["preview_video_path"] if session.get("media_type") == "video" else item["preview_image_path"]
        elif file_kind == "tracking":
            path = item["tracking_json_path"]
        else:
            raise ValueError("不支持的文件类型")

        if not path or not Path(path).exists():
            raise ValueError("文件不存在")
        return path, Path(path).name

    def process_session(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        items = session.get("items", [])
        session["status"] = "processing"
        session["progress"] = {
            "total": len(items),
            "processed": 0,
            "failed": 0,
            "percent": 0.0,
            "message": "开始标注",
        }
        session["engine"] = self.engine.get_engine_info()
        session = save_session(session)

        processed = 0
        failed = 0
        for index, item in enumerate(session.get("items", [])):
            item["status"] = "processing"
            session["progress"]["message"] = f"处理中 {index + 1}/{len(items)}: {item['file_name']}"
            session = save_session(session)
            try:
                if session.get("media_type") == "image":
                    self._process_image_item(session, item)
                else:
                    self._process_video_item(session, item)
                item["status"] = "ready"
                item["error_message"] = None
                processed += 1
            except Exception as exc:
                logger.error("处理标注项失败: session=%s item=%s error=%s", session_id, item.get("file_name"), exc, exc_info=True)
                item["status"] = "failed"
                item["error_message"] = str(exc)
                failed += 1
            session["progress"] = {
                "total": len(items),
                "processed": processed,
                "failed": failed,
                "percent": round(((processed + failed) / max(len(items), 1)) * 100, 2),
                "message": f"已完成 {processed + failed}/{len(items)}",
            }
            session = save_session(session)

        session["status"] = "failed" if processed == 0 and failed > 0 else "ready"
        if failed and processed:
            session["status"] = "ready"
        session["progress"]["message"] = "标注完成" if processed else "标注失败"
        return save_session(session)

    def _process_image_item(self, session: dict[str, Any], item: dict[str, Any]) -> None:
        source_path = item["source_path"]
        preview_dir = item["artifact_dir"]
        image = cv2.imread(source_path)
        if image is None:
            raise ValueError("图片读取失败")
        height, width = image.shape[:2]
        annotations = self.engine.detect_image(source_path, preview_dir=preview_dir)
        description = self.engine.describe_image(source_path, cache_path=item["description_path"])
        item["annotations"] = [self._normalize_annotation_payload(annotation) for annotation in annotations]
        item["description"] = description
        item["width"] = width
        item["height"] = height
        item["stats"] = self._summarize_image_annotations(item["annotations"])

    def _process_video_item(self, session: dict[str, Any], item: dict[str, Any]) -> None:
        result = self.engine.detect_video(
            item["source_path"],
            preview_video_path=item["preview_video_path"],
            tracking_json_path=item["tracking_json_path"],
            use_tracking=bool(session["options"].get("use_tracking", True)),
            frame_interval=max(int(session["options"].get("frame_interval", 1)), 1),
            detect_size=max(int(session["options"].get("detect_size", 640)), 160),
        )
        description = self.engine.describe_video(item["source_path"], cache_path=item["description_path"])
        item["description"] = description
        item["width"] = result.get("width")
        item["height"] = result.get("height")
        item["stats"] = {
            "total_frames": result.get("total_frames", 0),
            "tracked_frames": len(result.get("frames", {})),
            "peak_tracks": result.get("peak_tracks", 0),
            "fps": result.get("fps", 0),
        }
        item["annotations"] = []

    def _normalize_annotation_payload(self, annotation: dict[str, Any]) -> dict[str, Any]:
        cls = str(annotation.get("class") or "")
        if cls not in TARGET_CLASSES:
            raise ValueError(f"非法类别: {cls}")
        bbox = annotation.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("bbox 格式不合法")
        normalized_bbox = []
        for value in bbox:
            number = float(value)
            normalized_bbox.append(round(max(0.0, min(1.0, number)), 6))
        if normalized_bbox[2] <= normalized_bbox[0] or normalized_bbox[3] <= normalized_bbox[1]:
            raise ValueError("bbox 尺寸不合法")
        return {
            "class": cls,
            "class_id": int(annotation.get("class_id", TARGET_CLASSES[cls])),
            "confidence": float(annotation.get("confidence", 1.0)),
            "bbox": normalized_bbox,
            "manual": bool(annotation.get("manual", False)),
        }

    def _summarize_image_annotations(self, annotations: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(annotations)
        auto_count = sum(1 for item in annotations if not item.get("manual"))
        manual_count = total - auto_count
        by_class: dict[str, int] = {}
        for item in annotations:
            cls = str(item.get("class") or "unknown")
            by_class[cls] = by_class.get(cls, 0) + 1
        return {
            "total": total,
            "auto": auto_count,
            "manual": manual_count,
            "by_class": by_class,
        }

    def _require_session(self, session_id: str) -> dict[str, Any]:
        session = load_session(session_id)
        if not session:
            raise ValueError("标注会话不存在")
        return session

    def _require_item(self, session: dict[str, Any], item_id: str) -> dict[str, Any]:
        for item in session.get("items", []):
            if item.get("id") == item_id:
                return item
        raise ValueError("标注文件不存在")

    def _sanitize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        data = deepcopy(item)
        data.pop("source_path", None)
        data.pop("preview_image_path", None)
        data.pop("preview_result_path", None)
        data.pop("preview_video_path", None)
        data.pop("tracking_json_path", None)
        data.pop("description_path", None)
        data.pop("artifact_dir", None)
        return data


annotation_service = AnnotationService()

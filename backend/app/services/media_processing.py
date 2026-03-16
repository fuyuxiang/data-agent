"""
媒体离线处理与状态汇总。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import (
    Dataset,
    DatasetMediaResource,
    ImageIndex,
    MediaResourceType,
    ProcessingStatus,
    VideoSegmentIndex,
)
from app.services.media_models import media_model_client
from app.services.media_utils import (
    MEDIA_KEYFRAME_ROOT,
    ensure_media_dirs,
)

logger = get_logger(__name__)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    left_vec = np.asarray(left, dtype=np.float32)
    right_vec = np.asarray(right, dtype=np.float32)
    if left_vec.size != right_vec.size:
        size = min(left_vec.size, right_vec.size)
        left_vec = left_vec[:size]
        right_vec = right_vec[:size]
    denom = float(np.linalg.norm(left_vec) * np.linalg.norm(right_vec))
    if denom <= 0:
        return 0.0
    return float(np.dot(left_vec, right_vec) / denom)


def text_match_score(query_text: str, *fields: Optional[str]) -> float:
    if not query_text:
        return 0.0
    lowered_query = query_text.lower()
    corpus = " ".join(field for field in fields if field).lower()
    if not corpus:
        return 0.0
    tokens = [token for token in lowered_query.replace("/", " ").split() if token]
    if not tokens:
        tokens = [lowered_query]
    hits = sum(1 for token in tokens if token in corpus)
    return hits / max(len(tokens), 1)


async def process_dataset_resources(db: AsyncSession, dataset_id: int) -> None:
    result = await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    dataset = result.scalar_one_or_none()
    if not dataset:
        return

    resources_result = await db.execute(
        select(DatasetMediaResource)
        .where(DatasetMediaResource.dataset_id == dataset_id)
        .order_by(DatasetMediaResource.id.asc())
    )
    resources = resources_result.scalars().all()

    if not resources:
        await reconcile_dataset_processing_status(db, dataset_id)
        return

    dataset.processing_status = ProcessingStatus.PROCESSING
    dataset.error_message = None
    dataset.progress = 0.0
    await db.commit()

    for resource in resources:
        if resource.status == ProcessingStatus.READY:
            continue
        try:
            if resource.resource_type == MediaResourceType.IMAGE:
                await process_image_resource(db, resource)
            elif resource.resource_type == MediaResourceType.VIDEO:
                await process_video_resource(db, resource)
            else:
                raise RuntimeError(f"unsupported resource type: {resource.resource_type}")
        except Exception as exc:
            logger.error(f"处理媒体资源失败: dataset={dataset_id}, resource={resource.id}, error={exc}", exc_info=True)
            resource.status = ProcessingStatus.FAILED
            resource.error_message = str(exc)
            resource.last_processed_at = datetime.utcnow()
            await db.commit()
        finally:
            await reconcile_dataset_processing_status(db, dataset_id)


async def process_image_resource(db: AsyncSession, resource: DatasetMediaResource) -> None:
    image_path = resource.stored_path or resource.original_path
    if not image_path:
        raise RuntimeError("image path missing")

    resource.status = ProcessingStatus.PROCESSING
    resource.error_message = None
    await db.commit()

    image = Image.open(image_path).convert("RGB")
    metadata = {
        "width": image.width,
        "height": image.height,
        "mode": image.mode,
        "format": image.format,
    }
    ensure_media_dirs()
    preview_dir = MEDIA_KEYFRAME_ROOT / f"dataset_{resource.dataset_id}"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = str(preview_dir / f"resource_{resource.id}_preview.jpg")
    preview_image = image.copy()
    preview_image.thumbnail((768, 768))
    preview_image.save(preview_path, format="JPEG")

    embedding = media_model_client.embed_image(image_path)
    caption = media_model_client.caption_image(image_path)
    tags = media_model_client.extract_tags(caption)

    await db.execute(delete(ImageIndex).where(ImageIndex.resource_id == resource.id))
    image_index = ImageIndex(
        dataset_id=resource.dataset_id,
        resource_id=resource.id,
        preview_path=preview_path,
        embedding=embedding,
        ocr_text=None,
        caption_text=caption,
        tags=tags,
        index_metadata=metadata,
    )
    db.add(image_index)

    resource.media_metadata = metadata
    resource.status = ProcessingStatus.READY
    resource.error_message = None
    resource.last_processed_at = datetime.utcnow()
    await db.commit()


async def process_video_resource(db: AsyncSession, resource: DatasetMediaResource) -> None:
    video_path = resource.stored_path or resource.original_path
    if not video_path:
        raise RuntimeError("video path missing")

    resource.status = ProcessingStatus.PROCESSING
    resource.error_message = None
    await db.commit()

    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if fps <= 0 or frame_count <= 0:
        capture.release()
        raise RuntimeError(f"invalid video metadata: {video_path}")

    duration = frame_count / fps
    metadata = {
        "fps": fps,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "duration_sec": duration,
    }

    await db.execute(delete(VideoSegmentIndex).where(VideoSegmentIndex.resource_id == resource.id))
    ensure_media_dirs()
    keyframe_dir = MEDIA_KEYFRAME_ROOT / f"dataset_{resource.dataset_id}"
    keyframe_dir.mkdir(parents=True, exist_ok=True)

    window_sec = max(float(settings.VIDEO_SEGMENT_WINDOW_SEC), 1.0)
    stride_sec = max(float(settings.VIDEO_SEGMENT_STRIDE_SEC), 1.0)

    segments: list[VideoSegmentIndex] = []
    segment_index = 0
    start_sec = 0.0
    while start_sec < duration:
        end_sec = min(start_sec + window_sec, duration)
        frame_sec = min((start_sec + end_sec) / 2.0, max(duration - 0.001, 0.0))
        frame_no = min(int(frame_sec * fps), max(frame_count - 1, 0))
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        ok, frame = capture.read()
        if not ok or frame is None:
            start_sec += stride_sec
            segment_index += 1
            continue

        keyframe_path = str(keyframe_dir / f"resource_{resource.id}_seg_{segment_index}.jpg")
        cv2.imwrite(keyframe_path, frame)
        caption = media_model_client.caption_image(keyframe_path)
        tags = media_model_client.extract_tags(caption)
        embedding = media_model_client.embed_image(keyframe_path)

        segments.append(
            VideoSegmentIndex(
                dataset_id=resource.dataset_id,
                resource_id=resource.id,
                video_id=resource.id,
                segment_index=segment_index,
                start_sec=float(round(start_sec, 3)),
                end_sec=float(round(end_sec, 3)),
                keyframe_path=keyframe_path,
                embedding=embedding,
                caption_text=caption,
                asr_text=None,
                ocr_text=None,
                scene_tags=tags,
                object_tags=tags,
                index_metadata={
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "duration_sec": duration,
                },
            )
        )
        start_sec += stride_sec
        segment_index += 1

    capture.release()

    if not segments:
        raise RuntimeError("no valid video segments extracted")

    db.add_all(segments)
    resource.media_metadata = metadata
    resource.status = ProcessingStatus.READY
    resource.error_message = None
    resource.last_processed_at = datetime.utcnow()
    await db.commit()


async def reconcile_dataset_processing_status(db: AsyncSession, dataset_id: int) -> None:
    result = await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    dataset = result.scalar_one_or_none()
    if not dataset:
        return

    resource_rows = await db.execute(
        select(
            func.count(DatasetMediaResource.id),
            func.sum(case((DatasetMediaResource.status == ProcessingStatus.READY, 1), else_=0)),
            func.sum(case((DatasetMediaResource.status == ProcessingStatus.FAILED, 1), else_=0)),
            func.sum(case((DatasetMediaResource.status == ProcessingStatus.PROCESSING, 1), else_=0)),
            func.sum(case((DatasetMediaResource.status == ProcessingStatus.PENDING, 1), else_=0)),
        ).where(DatasetMediaResource.dataset_id == dataset_id)
    )
    total, ready_count, failed_count, processing_count, pending_count = resource_rows.one()
    total = int(total or 0)
    ready_count = int(ready_count or 0)
    failed_count = int(failed_count or 0)
    processing_count = int(processing_count or 0)
    pending_count = int(pending_count or 0)

    dataset.media_count = total
    dataset.processed_count = ready_count
    dataset.failed_count = failed_count

    if total == 0:
        dataset.processing_status = ProcessingStatus.READY
        dataset.progress = 100.0
        dataset.error_message = None
    elif processing_count > 0:
        dataset.processing_status = ProcessingStatus.PROCESSING
        dataset.progress = round(((ready_count + failed_count) / total) * 100, 2)
    elif pending_count > 0:
        dataset.processing_status = ProcessingStatus.PENDING
        dataset.progress = round((ready_count / total) * 100, 2)
    elif failed_count > 0:
        dataset.processing_status = ProcessingStatus.FAILED
        dataset.progress = 100.0
    else:
        dataset.processing_status = ProcessingStatus.READY
        dataset.progress = 100.0

    if total > 0:
        resource_result = await db.execute(
            select(DatasetMediaResource)
            .where(DatasetMediaResource.dataset_id == dataset_id)
            .order_by(DatasetMediaResource.updated_at.desc())
        )
        resources = resource_result.scalars().all()
        dataset.last_processed_at = max(
            (resource.last_processed_at for resource in resources if resource.last_processed_at),
            default=dataset.last_processed_at,
        )
        first_error = next((resource.error_message for resource in resources if resource.error_message), None)
        dataset.error_message = first_error if failed_count > 0 else None

    await db.commit()

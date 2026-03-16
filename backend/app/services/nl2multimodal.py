"""
多模态检索规划与执行接口。

search intent 仍统一从这里进入，再按当前数据集内的 image / video_segment 索引执行。
若当前数据集不存在媒体索引，则回退到既有 `vector_search`。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine
from app.models.models import Dataset, DatasetMediaResource, ImageIndex, ProcessingStatus, VideoSegmentIndex
from app.services.media_models import media_model_client
from app.services.media_processing import cosine_similarity, text_match_score
from app.services.media_utils import resolve_preview_path
from app.services.query_plan import QueryPlan
from app.services.vector_search import vector_search

VIDEO_KEYWORDS = {"视频", "片段", "时段", "时间段", "监控", "录像", "回放"}
IMAGE_KEYWORDS = {"图片", "图像", "照片", "截图", "画面"}


def parse_top_k(text: str, default: int = 20) -> int:
    """解析检索数量。"""
    match = re.search(r"(前|top|TOP)\s*(\d+)", text)
    if match:
        return int(match.group(2))
    match = re.search(r"(\d+)\s*条", text)
    if match:
        return int(match.group(1))
    return default


def build_multimodal_query_plan(
    text: str,
    intent_meta: Optional[Dict[str, Any]] = None,
    request_context: Optional[Dict[str, Any]] = None,
) -> QueryPlan:
    """构建 search intent 对应的多模态检索计划。"""
    intent_meta = intent_meta or {}
    request_context = request_context or {}
    top_k = parse_top_k(text, default=10)
    query_mode = "image" if request_context.get("query_image_path") else "text"
    if request_context.get("query_video_path"):
        query_mode = "video"
    return QueryPlan(
        intent="search",
        sql="",
        params=[],
        filters={
            "query_text": text,
            "top_k": top_k,
            "query_mode": query_mode,
            "plan_source": "llm",
            "agent_mode": "multi_agent",
            "confidence": float(intent_meta.get("confidence", 1.0)),
            "intent_agent": intent_meta,
        },
    )


def run_multimodal_search(
    query_text: str,
    *,
    top_k: int = 10,
    config: Optional[Dict[str, Any]] = None,
    dataset_id: Optional[int] = None,
    query_image_path: Optional[str] = None,
    query_video_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """执行多模态检索。"""
    if query_video_path:
        # TODO: 预留视频搜视频接口。当前阶段不完整，后续可引入视频级表征与时序匹配。
        return []

    if not dataset_id:
        effective_config = config or {"search": {"lancedb_dir": "data/lancedb"}}
        return vector_search(effective_config, query_text, top_k=top_k)

    with Session(engine) as session:
        dataset = session.get(Dataset, dataset_id)
        if not dataset:
            return []

        image_rows = session.execute(
            select(ImageIndex, DatasetMediaResource)
            .join(DatasetMediaResource, DatasetMediaResource.id == ImageIndex.resource_id)
            .where(ImageIndex.dataset_id == dataset_id)
        ).all()
        segment_rows = session.execute(
            select(VideoSegmentIndex, DatasetMediaResource)
            .join(DatasetMediaResource, DatasetMediaResource.id == VideoSegmentIndex.resource_id)
            .where(VideoSegmentIndex.dataset_id == dataset_id)
        ).all()

        if not image_rows and not segment_rows:
            effective_config = config or {"search": {"lancedb_dir": "data/lancedb"}}
            return vector_search(effective_config, query_text, top_k=top_k)

        target_mode = _resolve_target_mode(query_text, query_image_path)
        query_embedding = None
        if query_image_path:
            query_embedding = media_model_client.embed_image(query_image_path)
        else:
            query_embedding = media_model_client.embed_text(query_text)

        candidate_k = max(top_k * 5, 20)
        results: list[dict[str, Any]] = []
        if target_mode in {"image", "both"}:
            results.extend(_search_images(image_rows, query_text, query_embedding, query_image_path, candidate_k))
        if target_mode in {"video", "both"}:
            results.extend(_search_videos(segment_rows, query_text, query_embedding, query_image_path, candidate_k))

        rerank_query = _build_rerank_query_text(query_text, query_image_path)
        reranked_results = _apply_rerank(rerank_query, results, top_k=top_k)
        reranked_results.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return reranked_results[:top_k]


def _resolve_target_mode(query_text: str, query_image_path: Optional[str]) -> str:
    if any(keyword in query_text for keyword in VIDEO_KEYWORDS):
        return "video"
    if any(keyword in query_text for keyword in IMAGE_KEYWORDS):
        return "image"
    if query_image_path:
        return "both"
    return "both"


def _search_images(
    rows: list,
    query_text: str,
    query_embedding: list[float],
    query_image_path: Optional[str],
    top_k: int,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for image_index, resource in rows:
        if resource.status not in {ProcessingStatus.READY, ProcessingStatus.FAILED}:
            continue
        vector_score = cosine_similarity(query_embedding, image_index.embedding or [])
        text_score = 0.0 if query_image_path else text_match_score(
            query_text,
            image_index.caption_text,
            image_index.ocr_text,
            " ".join(image_index.tags or []),
        )
        score = vector_score if query_image_path else (0.35 * vector_score + 0.65 * text_score)
        if score <= 0:
            continue
        hits.append(
            {
                "type": "image",
                "image_id": image_index.id,
                "score": round(float(score), 4),
                "preview_url": resolve_preview_path(image_index.preview_path or resource.stored_path or resource.original_path),
                "dataset_id": image_index.dataset_id,
                "resource_id": resource.id,
                "extra": {
                    "caption_text": image_index.caption_text,
                    "ocr_text": image_index.ocr_text,
                    "tags": image_index.tags or [],
                },
            }
        )
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits[:top_k]


def _search_videos(
    rows: list,
    query_text: str,
    query_embedding: list[float],
    query_image_path: Optional[str],
    top_k: int,
) -> list[dict[str, Any]]:
    segment_hits: list[dict[str, Any]] = []
    for segment, resource in rows:
        if resource.status not in {ProcessingStatus.READY, ProcessingStatus.FAILED}:
            continue
        vector_score = cosine_similarity(query_embedding, segment.embedding or [])
        text_score = 0.0 if query_image_path else text_match_score(
            query_text,
            segment.caption_text,
            segment.asr_text,
            segment.ocr_text,
            " ".join(segment.scene_tags or []),
            " ".join(segment.object_tags or []),
        )
        score = vector_score if query_image_path else (0.4 * vector_score + 0.6 * text_score)
        if score <= 0:
            continue
        segment_hits.append(
            {
                "video_id": segment.video_id,
                "segment_id": segment.id,
                "resource_id": resource.id,
                "dataset_id": segment.dataset_id,
                "start_sec": float(segment.start_sec),
                "end_sec": float(segment.end_sec),
                "score": float(score),
                "preview_frame": resolve_preview_path(segment.keyframe_path),
                "caption_text": segment.caption_text,
                "asr_text": segment.asr_text,
                "ocr_text": segment.ocr_text,
                "scene_tags": segment.scene_tags or [],
            }
        )

    segment_hits.sort(key=lambda item: (item["video_id"], item["start_sec"]))
    merged = _merge_adjacent_video_hits(segment_hits)
    merged.sort(key=lambda item: item["score"], reverse=True)
    return merged[:top_k]


def _merge_adjacent_video_hits(segment_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for hit in segment_hits:
        if not merged:
            merged.append(_to_video_result(hit))
            continue
        last = merged[-1]
        if (
            last["video_id"] == hit["video_id"]
            and hit["start_sec"] <= float(last["end_sec"]) + 1.0
        ):
            last["end_sec"] = max(float(last["end_sec"]), float(hit["end_sec"]))
            last["score"] = round(max(float(last["score"]), float(hit["score"])), 4)
            extra = last.setdefault("extra", {})
            for field in ("caption_text", "asr_text", "ocr_text"):
                if not extra.get(field) and hit.get(field):
                    extra[field] = hit.get(field)
            if not last.get("preview_frame") and hit.get("preview_frame"):
                last["preview_frame"] = hit["preview_frame"]
        else:
            merged.append(_to_video_result(hit))
    return merged


def _to_video_result(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "video",
        "video_id": hit["video_id"],
        "start_sec": round(float(hit["start_sec"]), 3),
        "end_sec": round(float(hit["end_sec"]), 3),
        "score": round(float(hit["score"]), 4),
        "preview_frame": hit.get("preview_frame"),
        "dataset_id": hit["dataset_id"],
        "resource_id": hit["resource_id"],
        "extra": {
            "caption_text": hit.get("caption_text"),
            "asr_text": hit.get("asr_text"),
            "ocr_text": hit.get("ocr_text"),
            "tags": hit.get("scene_tags") or [],
        },
    }


def _build_rerank_query_text(query_text: str, query_image_path: Optional[str]) -> str:
    if query_image_path:
        caption = media_model_client.caption_image(query_image_path)
        if caption:
            return caption
    return query_text


def _apply_rerank(
    query_text: str,
    results: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    if not results:
        return []

    documents = [_build_rerank_document(item) for item in results]
    reranked = media_model_client.rerank_documents(query_text, documents, top_k=len(documents))
    result_by_index = {idx: item for idx, item in enumerate(results)}

    reranked_results: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for item in reranked:
        index = int(item["index"])
        if index not in result_by_index or index in seen_indices:
            continue
        seen_indices.add(index)
        result = dict(result_by_index[index])
        base_score = float(result.get("score", 0.0))
        rerank_score = float(item.get("score", 0.0))
        final_score = round(0.75 * rerank_score + 0.25 * base_score, 4)
        result["score"] = final_score
        extra = dict(result.get("extra") or {})
        extra["retrieval_score"] = round(base_score, 4)
        extra["rerank_score"] = round(rerank_score, 4)
        result["extra"] = extra
        reranked_results.append(result)

    if len(reranked_results) < len(results):
        for idx, result in enumerate(results):
            if idx in seen_indices:
                continue
            reranked_results.append(result)

    return reranked_results


def _build_rerank_document(result: dict[str, Any]) -> str:
    if result.get("type") == "image":
        extra = result.get("extra") or {}
        tags = " ".join(extra.get("tags") or [])
        return " ".join(
            part
            for part in [
                "image",
                str(extra.get("caption_text") or ""),
                str(extra.get("ocr_text") or ""),
                tags,
            ]
            if part
        ).strip()

    extra = result.get("extra") or {}
    tags = " ".join(extra.get("tags") or [])
    return " ".join(
        part
        for part in [
            "video",
            f"time {result.get('start_sec', 0)} {result.get('end_sec', 0)}",
            str(extra.get("caption_text") or ""),
            str(extra.get("asr_text") or ""),
            str(extra.get("ocr_text") or ""),
            tags,
        ]
        if part
    ).strip()

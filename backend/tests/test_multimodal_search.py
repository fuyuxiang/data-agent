from __future__ import annotations

import asyncio

from app.services.media_models import media_model_client
from app.services.media_processing import process_dataset_resources
from app.services.nl2multimodal import run_multimodal_search

from .test_media_dataset_flow import create_image_file, create_test_video, post_dataset


def run_async(coro):
    return asyncio.run(coro)


def prepare_ready_video_dataset(client, test_context):
    query_image = create_image_file(test_context["tmp_path"] / "query_red.png", (255, 0, 0))
    video_path = create_test_video(test_context["tmp_path"] / "red_blue_video.avi")
    response = post_dataset(client, test_context["workspace_id"], "search-video", video_paths=[video_path])
    dataset_id = response.json()["id"]

    async def _process():
        async with test_context["async_session_maker"]() as session:
            await process_dataset_resources(session, dataset_id)

    run_async(_process())

    return dataset_id, query_image


def prepare_ready_image_dataset(client, test_context):
    red_image = create_image_file(test_context["tmp_path"] / "rank_red.png", (255, 0, 0))
    green_image = create_image_file(test_context["tmp_path"] / "rank_green.png", (0, 255, 0))
    response = post_dataset(
        client,
        test_context["workspace_id"],
        "search-image",
        image_paths=[red_image, green_image],
    )
    dataset_id = response.json()["id"]

    async def _process():
        async with test_context["async_session_maker"]() as session:
            await process_dataset_resources(session, dataset_id)

    run_async(_process())
    return dataset_id


def test_text_to_video_search_returns_time_range(client, test_context):
    dataset_id, _ = prepare_ready_video_dataset(client, test_context)
    results = run_multimodal_search("查找 red image 视频", dataset_id=dataset_id, top_k=5)
    assert results
    top = results[0]
    assert top["type"] == "video"
    assert isinstance(top["video_id"], int)
    assert top["start_sec"] < top["end_sec"]
    assert top["preview_frame"].startswith("/media-files/")


def test_image_to_video_search_returns_time_range(client, test_context):
    dataset_id, query_image = prepare_ready_video_dataset(client, test_context)
    results = run_multimodal_search(
        "查找相似视频",
        dataset_id=dataset_id,
        query_image_path=str(query_image),
        top_k=5,
    )
    assert results
    top = results[0]
    assert top["type"] == "video"
    assert isinstance(top["video_id"], int)
    assert top["start_sec"] < top["end_sec"]
    assert top["score"] > 0


def test_video_to_video_search_returns_time_range(client, test_context):
    dataset_id, _ = prepare_ready_video_dataset(client, test_context)
    query_video = create_test_video(test_context["tmp_path"] / "query_video.avi")
    results = run_multimodal_search(
        "查找相似视频",
        dataset_id=dataset_id,
        query_video_path=str(query_video),
        top_k=5,
    )
    assert results
    top = results[0]
    assert top["type"] == "video"
    assert isinstance(top["video_id"], int)
    assert top["start_sec"] < top["end_sec"]
    assert top["score"] > 0


def test_multimodal_search_applies_rerank_order(client, test_context, monkeypatch):
    dataset_id = prepare_ready_image_dataset(client, test_context)

    def fake_rerank(query, documents, top_k=None):
        assert query == "red image"
        assert len(documents) >= 2
        return [
            {"index": 1, "score": 1.0, "document": documents[1]},
            {"index": 0, "score": 0.2, "document": documents[0]},
        ]

    monkeypatch.setattr(media_model_client, "rerank_documents", fake_rerank)

    results = run_multimodal_search("red image", dataset_id=dataset_id, top_k=2)

    assert len(results) == 2
    assert results[0]["type"] == "image"
    assert "green image" in (results[0]["extra"].get("caption_text") or "")
    assert results[0]["extra"]["rerank_score"] == 1.0
    assert results[1]["extra"]["rerank_score"] == 0.2

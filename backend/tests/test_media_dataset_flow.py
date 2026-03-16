from __future__ import annotations

import asyncio
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from sqlalchemy.orm import Session

import app.api.queries as queries_api
from app.models.models import (
    Dataset,
    DatasetMediaResource,
    DatasetStatus,
    ImageIndex,
    MediaResourceType,
    MediaSourceType,
    ProcessingStatus,
    VideoSegmentIndex,
)
from app.services.media_processing import process_dataset_resources


def create_csv_file(path: Path) -> Path:
    path.write_text("name,value\nfoo,1\nbar,2\n", encoding="utf-8")
    return path


def create_image_file(path: Path, color: tuple[int, int, int]) -> Path:
    Image.new("RGB", (96, 96), color=color).save(path)
    return path


def create_test_video(path: Path) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 2.0, (64, 64))
    for _ in range(10):
        writer.write(_solid_frame((0, 0, 255)))
    for _ in range(10):
        writer.write(_solid_frame((255, 0, 0)))
    writer.release()
    return path


def _solid_frame(color_bgr: tuple[int, int, int]):
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    frame[:, :] = color_bgr
    return frame


def create_dataset_payload(workspace_id: int, name: str) -> dict:
    return {
        "workspace_id": workspace_id,
        "name": name,
        "description": "test dataset",
        "status": "active",
    }


def post_dataset(client, workspace_id: int, name: str, *, csv_paths=None, image_paths=None, video_paths=None):
    csv_paths = csv_paths or []
    image_paths = image_paths or []
    video_paths = video_paths or []
    files = []
    handles = []
    try:
        for csv_path in csv_paths:
            handle = open(csv_path, "rb")
            handles.append(handle)
            files.append(("csv_files", (csv_path.name, handle, "text/csv")))
        for image_path in image_paths:
            handle = open(image_path, "rb")
            handles.append(handle)
            files.append(("image_files", (image_path.name, handle, "image/png")))
        for video_path in video_paths:
            handle = open(video_path, "rb")
            handles.append(handle)
            files.append(("video_files", (video_path.name, handle, "video/avi")))

        return client.post(
            "/api/v1/datasets",
            data={"payload": json.dumps(create_dataset_payload(workspace_id, name))},
            files=files,
        )
    finally:
        for handle in handles:
            handle.close()


def run_async(coro):
    return asyncio.run(coro)


def test_create_csv_dataset(client, test_context):
    csv_path = create_csv_file(test_context["tmp_path"] / "sample.csv")
    response = post_dataset(client, test_context["workspace_id"], "csv-only", csv_paths=[csv_path])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["processing_status"] == "ready"
    assert data["media_count"] == 0
    assert len(data["data_source_ids"]) == 1
    assert test_context["queued_dataset_ids"] == []


def test_create_image_dataset(client, test_context):
    image_path = create_image_file(test_context["tmp_path"] / "red_image.png", (255, 0, 0))
    response = post_dataset(client, test_context["workspace_id"], "image-only", image_paths=[image_path])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["processing_status"] == "pending"
    assert data["media_count"] == 1
    assert test_context["queued_dataset_ids"] == [data["id"]]

    with Session(test_context["sync_engine"]) as session:
        resources = session.query(DatasetMediaResource).filter_by(dataset_id=data["id"]).all()
        assert len(resources) == 1
        assert resources[0].resource_type == MediaResourceType.IMAGE


def test_create_video_dataset(client, test_context):
    video_path = create_test_video(test_context["tmp_path"] / "segment_video.avi")
    response = post_dataset(client, test_context["workspace_id"], "video-only", video_paths=[video_path])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["processing_status"] == "pending"
    assert data["media_count"] == 1
    assert test_context["queued_dataset_ids"] == [data["id"]]


def test_create_mixed_dataset(client, test_context):
    csv_path = create_csv_file(test_context["tmp_path"] / "mixed.csv")
    image_path = create_image_file(test_context["tmp_path"] / "mixed_image.png", (0, 255, 0))
    response = post_dataset(
        client,
        test_context["workspace_id"],
        "mixed-dataset",
        csv_paths=[csv_path],
        image_paths=[image_path],
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["media_count"] == 1
    assert data["processing_status"] == "pending"
    assert len(data["data_source_ids"]) == 1


def test_image_processing_success(client, test_context):
    image_path = create_image_file(test_context["tmp_path"] / "process_image.png", (255, 0, 0))
    response = post_dataset(client, test_context["workspace_id"], "image-process", image_paths=[image_path])
    dataset_id = response.json()["id"]

    async def _process():
        async with test_context["async_session_maker"]() as session:
            await process_dataset_resources(session, dataset_id)

    run_async(_process())

    with Session(test_context["sync_engine"]) as session:
        dataset = session.get(Dataset, dataset_id)
        image_index = session.query(ImageIndex).filter_by(dataset_id=dataset_id).one()
        assert dataset.processing_status == ProcessingStatus.READY
        assert dataset.processed_count == 1
        assert image_index.caption_text
        assert image_index.preview_path


def test_video_processing_success(client, test_context):
    video_path = create_test_video(test_context["tmp_path"] / "red_video.avi")
    response = post_dataset(client, test_context["workspace_id"], "video-process", video_paths=[video_path])
    dataset_id = response.json()["id"]

    async def _process():
        async with test_context["async_session_maker"]() as session:
            await process_dataset_resources(session, dataset_id)

    run_async(_process())

    with Session(test_context["sync_engine"]) as session:
        dataset = session.get(Dataset, dataset_id)
        segments = session.query(VideoSegmentIndex).filter_by(dataset_id=dataset_id).all()
        assert dataset.processing_status == ProcessingStatus.READY
        assert dataset.processed_count == 1
        assert len(segments) >= 1
        assert segments[0].start_sec < segments[0].end_sec
        assert segments[0].keyframe_path


def test_processing_failure_updates_status(test_context):
    with Session(test_context["sync_engine"]) as session:
        dataset = Dataset(
            workspace_id=test_context["workspace_id"],
            name="broken-video",
            status=DatasetStatus.ACTIVE,
            processing_status=ProcessingStatus.PENDING,
            media_count=1,
            progress=0.0,
        )
        session.add(dataset)
        session.flush()
        session.add(
            DatasetMediaResource(
                dataset_id=dataset.id,
                resource_type=MediaResourceType.VIDEO,
                source_type=MediaSourceType.PATH,
                original_path="/missing/not-found.avi",
                stored_path="/missing/not-found.avi",
                status=ProcessingStatus.PENDING,
            )
        )
        session.commit()
        dataset_id = dataset.id

    async def _process():
        async with test_context["async_session_maker"]() as session:
            await process_dataset_resources(session, dataset_id)

    run_async(_process())

    with Session(test_context["sync_engine"]) as session:
        dataset = session.get(Dataset, dataset_id)
        resource = session.query(DatasetMediaResource).filter_by(dataset_id=dataset_id).one()
        assert dataset.processing_status == ProcessingStatus.FAILED
        assert dataset.failed_count == 1
        assert resource.error_message


def test_query_warning_when_dataset_processing(client, test_context, monkeypatch):
    with Session(test_context["sync_engine"]) as session:
        dataset = Dataset(
            workspace_id=test_context["workspace_id"],
            name="processing-dataset",
            status=DatasetStatus.ACTIVE,
            processing_status=ProcessingStatus.PENDING,
            media_count=1,
            progress=0.0,
        )
        session.add(dataset)
        session.commit()
        dataset_id = dataset.id

    def fake_run_stream(question, selected_tables=None, request_context=None):
        return {
            "intent": "search",
            "sql_result": [],
            "final_answer": {
                "type": "search",
                "value": [],
                "message": "为您找到 0 条相关结果",
            },
            "filters": {"plan_source": "llm", "confidence": 0.9},
            "logs": [],
        }

    monkeypatch.setattr(queries_api, "run_stream", fake_run_stream)

    response = client.post(
        "/api/v1/queries",
        json={
            "question": "搜索相关视频",
            "workspace_id": test_context["workspace_id"],
            "dataset_id": dataset_id,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert any("仍在处理中" in warning for warning in (data.get("warnings") or []))

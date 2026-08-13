"""Semantic management API. Role gates are added in S1 Task 3 — wiring up
`Depends(get_principal)` here so the call site is ready when those tasks
land.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_principal
from app.auth.principal import PrincipalContext
from app.core.db import get_meta_session
from app.semantic.loader import list_datasets, load_dataset
from app.semantic.model import SemanticError
from app.semantic.schemas import (
    DatasetDetailOut,
    DatasetSummaryOut,
    LintReportOut,
    PublishResultOut,
)
from app.semantic.service import get_lint_report, publish_dataset

router = APIRouter(prefix="/api/semantic", tags=["semantic"])


@router.get("/datasets", response_model=list[DatasetSummaryOut])
def get_datasets(
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    return [DatasetSummaryOut.model_validate(item) for item in list_datasets(session)]


@router.get("/datasets/{name}", response_model=DatasetDetailOut)
def get_dataset(
    name: str,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    try:
        dataset = load_dataset(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    return DatasetDetailOut.model_validate(dataset)


@router.get("/datasets/{name}/lint", response_model=LintReportOut)
def get_dataset_lint(
    name: str,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    try:
        return get_lint_report(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")


@router.post("/datasets/{name}/publish", response_model=PublishResultOut)
def post_dataset_publish(
    name: str,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    try:
        result = publish_dataset(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    if not result.published:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "语义体检未通过，无法发布", "issues": [
                item.model_dump() for item in result.issues
            ]},
        )
    return result
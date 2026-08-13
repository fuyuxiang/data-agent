"""Semantic management API.

Auth rules (S1 Task 3):
- `GET /datasets` returns the principal's visible datasets (no admin fields).
- `GET /datasets/{name}` returns the business view by default; admin roles
  (`semantic_editor`, `semantic_approver`, `security_admin`) get the admin
  view that includes physical_table, physical_column and sensitivity.
- `GET /datasets/{name}/lint` is gated on `semantic_editor` or
  `semantic_approver` — the lint report itself discloses physical reality.
- `POST /datasets/{name}/publish` is gated on `semantic_approver`.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_principal, require_roles
from app.auth.principal import PrincipalContext
from app.core.db import get_meta_session
from app.semantic.loader import list_datasets_for_principal, load_dataset
from app.semantic.model import SemanticError
from app.semantic.schemas import (
    DatasetDetailOut,
    DatasetDetailOutPublic,
    DatasetSummaryOut,
    DatasetSummaryOutPublic,
    LintReportOut,
    PublishResultOut,
)
from app.semantic.service import get_lint_report, publish_dataset

router = APIRouter(prefix="/api/semantic", tags=["semantic"])


_ADMIN_ROLES = ("semantic_editor", "semantic_approver", "security_admin")


def _can_see_admin_view(principal: PrincipalContext) -> bool:
    return principal.has_role(*_ADMIN_ROLES)


@router.get("/datasets", response_model=list[DatasetSummaryOutPublic])
def get_datasets(
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    return [
        DatasetSummaryOutPublic.model_validate(item)
        for item in list_datasets_for_principal(session, principal)
    ]


@router.get("/datasets/{name}")
def get_dataset(
    name: str,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    try:
        dataset = load_dataset(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    if _can_see_admin_view(principal):
        return DatasetDetailOut.model_validate(dataset)
    return DatasetDetailOutPublic.model_validate(dataset)


@router.get("/datasets/{name}/lint", response_model=LintReportOut)
def get_dataset_lint(
    name: str,
    principal: PrincipalContext = Depends(
        require_roles("semantic_editor", "semantic_approver")
    ),
    session: Session = Depends(get_meta_session),
):
    try:
        return get_lint_report(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")


@router.post("/datasets/{name}/publish", response_model=PublishResultOut)
def post_dataset_publish(
    name: str,
    principal: PrincipalContext = Depends(require_roles("semantic_approver")),
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
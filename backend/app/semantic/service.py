from sqlalchemy import select
from sqlalchemy.orm import Session

from app.semantic.lint import LintSeverity, lint_dataset
from app.semantic.loader import load_dataset
from app.semantic.orm import DatasetRow
from app.semantic.schemas import LintIssueOut, LintReportOut, PublishResultOut


def get_lint_report(session: Session, name: str) -> LintReportOut:
    dataset = load_dataset(session, name)
    issues = lint_dataset(dataset)
    return LintReportOut(
        dataset=name,
        publishable=not any(item.severity == LintSeverity.ERROR.value for item in issues),
        issues=[LintIssueOut.model_validate(item) for item in issues],
    )


def publish_dataset(session: Session, name: str) -> PublishResultOut:
    """Flip is_published only when lint reports no ERROR (spec M-07)."""
    report = get_lint_report(session, name)
    if not report.publishable:
        return PublishResultOut(dataset=name, published=False, issues=report.issues)

    row = session.execute(
        select(DatasetRow).where(DatasetRow.name == name)
    ).scalar_one()
    row.is_published = True
    session.flush()
    return PublishResultOut(dataset=name, published=True, issues=[])

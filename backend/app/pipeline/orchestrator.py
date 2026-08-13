"""The seven-stage pipeline (spec 3.2, M-09).

The orchestrator owns flow control and tracing; stages stay unaware of their
position. Three exits: answered, clarifying, refused. Nothing reaches an answer
without passing security rewriting — including Verified Query hits.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.compiler.errors import CompileError
from app.compiler.query import Citation, compile_intent
from app.core.config import Settings
from app.execution.runner import ExecutionFailedError, execute
from app.execution.validation import validate_result
from app.intent.recognizer import IntentRecognitionError, LlmClient, recognize
from app.intent.schema import IntentKind, QueryIntent
from app.observability.orm import ConversationRow, TurnRow
from app.observability.trace import Stage, TraceRecorder
from app.pipeline.answer import Answer, ResultNotAnswerableError, build_answer
from app.pipeline.clarify import ClarifyRequest
from app.pipeline.resolve import resolve_intent
from app.pipeline.verified import recall
from app.security.columns import PermissionDeniedError, assert_intent_permitted, visible_dataset
from app.security.guardrails import QueryTooExpensiveError
from app.security.pipeline import SecuredQuery, secure_compiled, secure_verified_sql
from app.security.principal import PrincipalNotFoundError, load_principal
from app.security.whitelist import AstRejectedError
from app.semantic.loader import load_dataset
from app.semantic.model import SemanticError

_GENERIC_REFUSAL = "你没有该数据的访问权限"
_OUT_OF_SCOPE = "这个问题超出了当前数据集能回答的范围，我无法据此给出数字"


class TurnStatus(str, Enum):
    ANSWERED = "answered"
    CLARIFYING = "clarifying"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    status: TurnStatus
    turn_id: int
    conversation_id: int
    answer: Answer | None = None
    clarifications: tuple[ClarifyRequest, ...] = ()
    refusal_reason: str = ""
    slot_state: dict | None = None


class QueryOrchestrator:
    def __init__(
        self,
        *,
        meta_session: Session,
        sample_connection: Connection,
        llm_client: LlmClient,
        settings: Settings,
    ) -> None:
        self._session = meta_session
        self._connection = sample_connection
        self._client = llm_client
        self._settings = settings

    def ask(
        self,
        *,
        username: str,
        question: str,
        dataset_name: str,
        conversation_id: int | None = None,
    ) -> TurnOutcome:
        try:
            conversation = self._conversation(conversation_id, username, question, dataset_name)
            turn = TurnRow(conversation_id=conversation.id, question=question)
            self._session.add(turn)
            self._session.flush()
        except PrincipalNotFoundError:
            # An inactive or unknown user is denied without revealing anything.
            stub = TurnRow(conversation_id=0, question=question)
            stub.conversation_id = 0  # never persisted, never read
            return TurnOutcome(
                status=TurnStatus.REFUSED,
                turn_id=0,
                conversation_id=0,
                refusal_reason=_GENERIC_REFUSAL,
            )

        recorder = TraceRecorder(self._session, turn.id)

        try:
            return self._run(recorder, conversation, turn, username, question, dataset_name)
        except (PermissionDeniedError, PrincipalNotFoundError):
            return self._refuse(turn, _GENERIC_REFUSAL)
        except AstRejectedError:
            return self._fail(turn, "查询无法安全执行，已阻止")
        except QueryTooExpensiveError as error:
            return self._fail(turn, error.estimate.message)
        except CompileError:
            return self._fail(turn, "语义配置存在问题，已记录待管理员处理")
        except IntentRecognitionError:
            return self._fail(turn, "没有理解这个问题，请换一种说法")
        except ExecutionFailedError:
            return self._fail(turn, "查询执行失败，已记录详情")
        except ResultNotAnswerableError as error:
            return self._fail(turn, str(error))
        # SemanticError (including UnknownDatasetError from load_dataset) is
        # intentionally propagated: chat.py maps it to HTTP 404.

    # --- stages -----------------------------------------------------------

    def _run(
        self,
        recorder: TraceRecorder,
        conversation: ConversationRow,
        turn: TurnRow,
        username: str,
        question: str,
        dataset_name: str,
    ) -> TurnOutcome:
        principal = load_principal(self._session, username)
        full_dataset = load_dataset(self._session, dataset_name)
        if not full_dataset.is_published:
            return self._refuse(turn, _OUT_OF_SCOPE)

        # Denied columns disappear before the model ever sees the dataset.
        dataset = visible_dataset(full_dataset, principal)

        # Stage 1
        with recorder.stage_timer(Stage.VERIFIED_RECALL, {"question": question}) as span:
            hit = recall(self._session, dataset_name, question)
            span.output = {"hit": hit.id if hit else None}

        if hit is not None:
            secured = self._secure_verified(recorder, hit.fixed_sql, dataset, principal)
            return self._finish(
                recorder,
                conversation,
                turn,
                dataset,
                secured,
                self._verified_citation(dataset),
            )

        # Stage 2
        slot_state = conversation.slot_state or None
        with recorder.stage_timer(
            Stage.INTENT, {"question": question, "slots": slot_state}
        ) as span:
            # Recognizer validates against the full dataset so hallucinated
            # metric names are still rejected; the prompt itself is built on
            # visible_dataset so the model never sees forbidden fields.
            intent, completion = recognize(
                self._client, dataset, question, slot_state, full_dataset=full_dataset
            )
            span.model = completion.model
            span.prompt_tokens = completion.prompt_tokens
            span.completion_tokens = completion.completion_tokens
            span.output = intent.to_payload()

        # Spec M-12: if the model referenced anything that the permission view
        # stripped, treat it as a permission refusal, not a recognition failure.
        # Validating against the full dataset above catches hallucinated names;
        # anything that survives validation but is absent from visible_dataset
        # is exactly what the model should not have known.
        permission_violated = False
        for name in intent.metrics:
            if not dataset.has_metric(name):
                permission_violated = True
                break
        if not permission_violated:
            for name in intent.dimensions:
                if not dataset.has_field(name):
                    permission_violated = True
                    break
        if not permission_violated:
            for condition in intent.filters:
                if not dataset.has_field(condition.field):
                    permission_violated = True
                    break
        if permission_violated:
            raise PermissionDeniedError()

        if slot_state:
            # Slot carry-over: drop stored keys that aren't part of intent.to_payload.
            intent_payload = {
                key: value
                for key, value in slot_state.items()
                if key in intent.to_payload()
            }
            intent = QueryIntent.from_payload(intent_payload).merge_followup(intent)

        turn.intent_snapshot = intent.to_payload()

        if intent.kind == IntentKind.UNSUPPORTED:
            return self._refuse(turn, _OUT_OF_SCOPE)

        # Stage 3
        round_index = (
            conversation.slot_state.get("clarify_rounds", 0)
            if conversation.slot_state
            else 0
        )
        with recorder.stage_timer(Stage.SEMANTIC_RESOLVE, intent.to_payload()) as span:
            outcome = resolve_intent(
                dataset, intent, self._settings, round_index=round_index
            )
            span.output = {
                "intent": outcome.intent.to_payload(),
                "clarifications": [item.target for item in outcome.clarifications],
                "assumptions": list(outcome.assumptions),
            }

        if outcome.clarifications:
            conversation.slot_state = {
                **outcome.intent.to_payload(),
                "clarify_rounds": round_index + 1,
            }
            turn.status = TurnStatus.CLARIFYING.value
            return TurnOutcome(
                status=TurnStatus.CLARIFYING,
                turn_id=turn.id,
                conversation_id=conversation.id,
                clarifications=outcome.clarifications,
                slot_state=conversation.slot_state,
            )

        resolved = outcome.intent
        assert_intent_permitted(dataset, resolved, principal)

        # Stage 4
        with recorder.stage_timer(Stage.COMPILE, resolved.to_payload()) as span:
            compiled = compile_intent(dataset, resolved)
            span.output = {"sql": compiled.sql, "metrics": list(compiled.metric_names)}

        # Stage 5
        with recorder.stage_timer(Stage.SECURITY, {"sql": compiled.sql}) as span:
            secured = secure_compiled(
                compiled, dataset, principal, self._connection, self._settings
            )
            span.output = {
                "sql": secured.sql,
                "row_filters": [
                    item.field_business_name for item in secured.applied_row_filters
                ],
                "masked": list(secured.masked_field_names),
                "estimated_rows": secured.cost.estimated_rows,
            }

        return self._finish(
            recorder,
            conversation,
            turn,
            dataset,
            secured,
            compiled.citation,
            intent=resolved,
            assumptions=outcome.assumptions,
            metric_names=compiled.metric_names,
            comparison_metric_names=compiled.comparison_metric_names,
            dimension_names=compiled.dimension_names,
        )

    def _secure_verified(self, recorder, fixed_sql, dataset, principal) -> SecuredQuery:
        with recorder.stage_timer(Stage.SECURITY, {"sql": fixed_sql}) as span:
            secured = secure_verified_sql(
                fixed_sql, dataset, principal, self._connection, self._settings
            )
            span.output = {
                "sql": secured.sql,
                "row_filters": [
                    item.field_business_name for item in secured.applied_row_filters
                ],
                "masked": list(secured.masked_field_names),
                "estimated_rows": secured.cost.estimated_rows,
            }
        return secured

    def _finish(
        self,
        recorder: TraceRecorder,
        conversation: ConversationRow,
        turn: TurnRow,
        dataset,
        secured: SecuredQuery,
        citation: Citation,
        *,
        intent: QueryIntent | None = None,
        assumptions: tuple[str, ...] = (),
        metric_names: tuple[str, ...] = (),
        comparison_metric_names: tuple[str, ...] = (),
        dimension_names: tuple[str, ...] = (),
    ) -> TurnOutcome:
        # Stage 6
        with recorder.stage_timer(Stage.EXECUTE, {"sql": secured.sql}) as span:
            result = execute(secured, self._settings, connection=self._connection)
            issues = validate_result(
                result,
                has_filters=bool(intent.filters) if intent else False,
                comparison_columns=dict(zip(metric_names, comparison_metric_names)),
            )
            span.output = {
                "row_count": result.row_count,
                "elapsed_ms": result.elapsed_ms,
                "issues": [item.code.value for item in issues],
            }

        # Stage 7
        with recorder.stage_timer(Stage.ANSWER, {"row_count": result.row_count}) as span:
            answer = build_answer(
                citation=citation,
                result=result,
                metric_names=metric_names or (citation.metric_name,),
                comparison_metric_names=comparison_metric_names,
                dimension_names=dimension_names,
                applied_row_filters=secured.applied_row_filters,
                masked_field_names=secured.masked_field_names,
                issues=issues,
                assumptions=assumptions,
                data_updated_at=self._data_updated_at(dataset),
                available_dimensions=tuple(
                    field.name for field in dataset.fields if field.is_groupable
                ),
            )
            span.output = {"headline": answer.headline}

        turn.status = TurnStatus.ANSWERED.value
        turn.answer = {"headline": answer.headline, "conclusion": answer.conclusion}
        if intent is not None:
            conversation.slot_state = {**intent.to_payload(), "clarify_rounds": 0}

        return TurnOutcome(
            status=TurnStatus.ANSWERED,
            turn_id=turn.id,
            conversation_id=conversation.id,
            answer=answer,
            slot_state=conversation.slot_state,
        )

    # --- helpers ----------------------------------------------------------

    def _conversation(
        self, conversation_id: int | None, username: str, question: str, dataset_name: str
    ) -> ConversationRow:
        if conversation_id is not None:
            existing = self._session.get(ConversationRow, conversation_id)
            if existing is not None:
                return existing

        try:
            principal = load_principal(self._session, username)
        except PrincipalNotFoundError:
            raise
        row = ConversationRow(
            user_id=principal.user_id,
            title=question[:64],
            dataset_name=dataset_name,
            slot_state={},
        )
        self._session.add(row)
        self._session.flush()
        return row

    def _verified_citation(self, dataset) -> Citation:
        """Verified entries carry their own intent snapshot; fall back to the
        dataset's primary metric for the citation shape."""
        metric = dataset.metrics[0]
        return Citation(
            metric_name=metric.name,
            metric_business_name=metric.business_name,
            metric_version=metric.version,
            metric_description=metric.description,
            time_field_business_name=(
                dataset.field(metric.time_field).business_name or metric.time_field
            ),
            time_start=datetime.now().date(),
            time_end=datetime.now().date(),
        )

    def _data_updated_at(self, dataset) -> datetime | None:
        """Freshness shown in the citation: the latest business date in scope."""
        column = dataset.metrics[0].time_field
        physical = dataset.field(column).physical_column
        statement = text(f"SELECT MAX({physical}) FROM {dataset.physical_table}")
        value = self._connection.execute(statement).scalar()
        if value is None:
            return None
        return datetime.combine(value, datetime.min.time())

    def _refuse(self, turn: TurnRow, reason: str) -> TurnOutcome:
        turn.status = TurnStatus.REFUSED.value
        return TurnOutcome(
            status=TurnStatus.REFUSED,
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
            refusal_reason=reason,
        )

    def _fail(self, turn: TurnRow, reason: str) -> TurnOutcome:
        turn.status = TurnStatus.FAILED.value
        return TurnOutcome(
            status=TurnStatus.FAILED,
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
            refusal_reason=reason,
        )
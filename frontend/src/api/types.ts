// Mirrors the backend Pydantic models. Renaming a field there must break tsc here.

export type TurnStatus = 'answered' | 'clarifying' | 'refused' | 'failed'

export interface CitationLine {
  label: string
  value: string
  /** 'permission' lines are appended by row-level policy, never typed by the user. */
  source: 'user' | 'permission'
}

export interface Citation {
  metric: string
  time: string
  filters: CitationLine[]
  data_updated_at: string
}

export interface DrillDown {
  label: string
  kind: string
  target: string
}

export interface Answer {
  headline: string
  conclusion: string
  assumptions: string[]
  warnings: string[]
  citation: Citation | null
  drill_downs: DrillDown[]
  columns: string[]
  rows: unknown[][]
}

export interface ClarifyOption {
  value: string
  label: string
  hint: string
}

export interface Clarification {
  kind: string
  target: string
  question: string
  options: ClarifyOption[]
}

export interface FilterSlot {
  field: string
  operator: string
  values: string[]
  spoken_values: string[]
}

export interface TimeSlot {
  start: string
  end: string
  grain: string
  expression: string
}

export interface SlotState {
  kind: string
  dataset: string
  metrics: string[]
  dimensions: string[]
  filters: FilterSlot[]
  time: TimeSlot | null
  comparison: string
  sort: { by: string; descending: boolean; limit: number | null } | null
  assumptions: string[]
}

export interface AskResponse {
  status: TurnStatus
  conversation_id: number
  turn_id: number
  answer: Answer | null
  clarifications: Clarification[]
  refusal_reason: string
  slot_state: SlotState | null
}

export interface Conversation {
  id: number
  title: string
  dataset_name: string
  updated_at: string
}

export interface Turn {
  id: number
  question: string
  status: TurnStatus
  answer: { headline: string; conclusion: string } | null
  created_at: string
}

export type FeedbackCategory = 'metric' | 'time' | 'sql' | 'calculation' | 'conclusion'

export interface TraceStage {
  stage: string
  sequence: number
  input_payload: Record<string, unknown> | null
  output_payload: Record<string, unknown> | null
  model: string | null
  prompt_tokens: number
  completion_tokens: number
  elapsed_ms: number
  error: string | null
}

export interface Trace {
  turn_id: number
  question: string
  status: TurnStatus
  intent_snapshot: Record<string, unknown> | null
  stages: TraceStage[]
}

export interface Replay {
  sql: string
  display_sql: string
  matches_original: boolean
  applied_row_filters: string[]
  masked_field_names: string[]
}

export interface EnumValue {
  physical_value: string
  business_value: string
  aliases: string[]
  description: string
}

export interface Field {
  name: string
  business_name: string
  synonyms: string[]
  semantic_type: string
  unit: string
  display_format: string
  default_aggregation: string
  allowed_aggregations: string[]
  is_filterable: boolean
  is_groupable: boolean
  sensitivity: string
  enum_values: EnumValue[]
}

export interface Metric {
  name: string
  business_name: string
  version: number
  kind: string
  aggregation_behavior: string
  description: string
  unit: string
  display_format: string
}

export interface DatasetSummary {
  name: string
  business_name: string
  physical_table: string
  grain: string
  is_published: boolean
  updated_at: string | null
}

export interface DatasetDetail extends DatasetSummary {
  aliases: string[]
  description: string
  applicable_scenario: string
  forbidden_scenario: string
  fields: Field[]
  metrics: Metric[]
}

export interface LintIssue {
  severity: 'error' | 'warning'
  target: string
  message: string
}

export interface LintReport {
  dataset: string
  publishable: boolean
  issues: LintIssue[]
}
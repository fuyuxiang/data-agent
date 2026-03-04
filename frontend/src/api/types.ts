// 用户相关
export interface User {
  id: number
  username: string
  email?: string
  full_name?: string
  is_active: boolean
  created_at: string
}

export interface LoginParams {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
}

// 工作空间
export interface Workspace {
  id: number
  name: string
  description?: string
  created_at: string
}

// 数据源
export interface DataSource {
  id: number
  workspace_id: number
  name: string
  type: 'mysql' | 'postgresql' | 'sqlserver' | 'csv' | 'duckdb'
  host?: string
  port?: number
  database?: string
  username?: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface DataSourceCreate {
  name: string
  type: string
  host?: string
  port?: number
  database?: string
  username?: string
  password?: string
  connection_string?: string
  workspace_id: number
}

export interface DataSourceTestRequest {
  type: string
  host?: string
  port?: number
  database?: string
  username?: string
  password?: string
  connection_string?: string
}

export interface SchemaColumn {
  table_name: string
  column_name: string
  column_type?: string
  description?: string
  is_primary_key: boolean
  is_foreign_key: boolean
  is_nullable: boolean
  sample_values?: any[]
  row_count_estimate?: number
}

// 数据集
export interface Dataset {
  id: number
  workspace_id: number
  data_source_id?: number
  data_source_ids?: number[]
  name: string
  description?: string
  status: 'draft' | 'active' | 'deprecated'
  metrics?: MetricDefinition[]
  dimensions?: DimensionDefinition[]
  aliases?: AliasMapping[]
  business_rules?: string
  version: number
  created_at: string
}

export interface MetricDefinition {
  name: string
  expression: string
  description?: string
  aggregation_type: string
}

export interface DimensionDefinition {
  name: string
  column: string
  description?: string
}

export interface AliasMapping {
  alias: string
  column: string
  description?: string
}

export interface DatasetCreate {
  name: string
  description?: string
  data_source_ids?: number[]
  data_source_id?: number
  metrics?: MetricDefinition[]
  dimensions?: DimensionDefinition[]
  aliases?: AliasMapping[]
  business_rules?: string
  status?: string
}

// 查询
export interface QueryRequest {
  question: string
  workspace_id: number
  dataset_id?: number
  table_names?: string[]  // 用户选择的表名列表
  context?: Record<string, any>
}

export interface QueryResponse {
  question: string
  normalized_question?: string
  intent?: string
  intent_text?: string
  sql?: string
  matched_dataset?: { id: number; name: string }
  semantic_sql?: string
  executable_sql?: string
  reasoning_summary?: string
  result_schema?: { name: string; type: string }[]
  result_rows?: Record<string, any>[]
  row_count: number
  chart_suggestion?: string
  cost_time_ms?: number
  cost_rows?: number
  warnings?: string[]
  status: string
  error?: string
  trace_id: string
  audit_id: string
  agent_steps?: AgentStep[]
  answer?: string
}

export interface AgentStep {
  step: number
  type: 'thought' | 'action' | 'observation'
  content?: string
  action?: string
  input?: string
}

// 历史记录
export interface QueryHistory {
  id: number
  question: string
  normalized_question?: string
  intent?: string
  semantic_sql?: string
  executable_sql?: string
  row_count: number
  execution_time_ms?: number
  status: string
  trace_id: string
  created_at: string
}

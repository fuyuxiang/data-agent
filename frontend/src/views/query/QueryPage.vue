<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick, computed, watch, reactive } from 'vue'
import { useUserStore } from '@/stores/user'
import { queryApi, datasetApi, dataSourceApi } from '@/api'
import type { QueryResponse, Dataset, SchemaColumn } from '@/api/types'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import * as echarts from 'echarts'

const userStore = useUserStore()

// 类型定义
interface Message {
  role: string
  content: string
  data?: QueryResponse
  thinkingLines?: string[]
}

interface TableItem {
  id: number
  name: string
  label: string
  checked: boolean
}

// 状态
const loading = ref(false)
const question = ref('')
const messages = ref<Message[]>([])
const datasets = ref<Dataset[]>([])
const selectedDataset = ref<Dataset | null>(null)
const tableSearch = ref('')
const selectedTables = ref<TableItem[]>([])

// 数据表（初始为空，选择数据集后加载）
const allTables = ref<TableItem[]>([])

// 快捷示例（基于 dm_m_kd_charge_info_sichuan 表的实际字段）
const quickExamples = [
  '统计2025年12月各一级发展渠道的用户数排行',
  '查询2025年12月各二级发展渠道的收入分布',
  '统计2025年11月各渠道类型的渠道数量',
  '查询最近10条渠道收入明细',
]

// 过滤后的数据表
const filteredTables = computed(() => {
  if (!tableSearch.value) return allTables.value
  const search = tableSearch.value.toLowerCase()
  return allTables.value.filter(
    t => t.name.toLowerCase().includes(search) || t.label.toLowerCase().includes(search)
  )
})

// 已选数据表
const selectedTableLabels = computed(() => {
  return allTables.value.filter(t => t.checked).map(t => t.label)
})

type ResultViewMode = 'detail' | 'chart'

const resultViewModeMap = ref<Record<number, ResultViewMode>>({})
const messageChartInstances = new Map<number, echarts.ECharts>()

const intentLabelMap: Record<string, string> = {
  chat: '普通问答',
  search: '内容检索',
  count: '统计分析',
  list: '明细查询',
  analysis: '分析查询',
  compare: '对比分析',
  trend: '趋势分析',
  skip: '无需查询',
}

const stepLabelMap: Record<string, string> = {
  parse_question: '问题理解',
  validate_sql: '查询检查',
  execute_sql: '数据查询',
  format_answer: '结果整理',
  semantic_enhance: '结果优化',
  vector_search: '内容检索',
  fix_sql: '自动修正',
  intent_node: '意图识别',
  semantic_node: '语义理解',
  dispatcher_node: '意图分流',
  sql_gen_node: '生成查询语句',
  sql_validate_node: '查询检查',
  sql_execute_node: '执行查询',
  format_node: '结果整理',
}

const getIntentLabel = (intent?: string) => {
  if (!intent) return '查询需求'
  return intentLabelMap[intent] || intent
}

const getFriendlyStepStartText = (step?: string) => {
  const stepText = stepLabelMap[step || ''] || '处理中'
  if (!step) return `${stepText}中`

  const startTextMap: Record<string, string> = {
    parse_question: '正在理解你的问题',
    validate_sql: '正在检查查询条件',
    execute_sql: '正在查询数据',
    format_answer: '正在整理结果',
    semantic_enhance: '正在优化结果',
    vector_search: '正在查找相关内容',
    fix_sql: '检测到异常，正在自动修正',
    intent_node: '正在识别你的问题类型',
    semantic_node: '正在理解问题语义',
    dispatcher_node: '正在规划处理路径',
    sql_gen_node: '正在生成查询语句',
    sql_validate_node: '正在检查查询语句',
    sql_execute_node: '正在执行查询',
    format_node: '正在整理结果',
  }
  return `[${stepText}] ${startTextMap[step] || '正在处理'}`
}

const getFriendlyStepEndLines = (step?: string, outputs?: any): string[] => {
  if (!step || !outputs) return []

  const lines: string[] = []
  const stepText = stepLabelMap[step] || step
  const errorMessage = outputs?.error_message

  if (step === 'parse_question' || step === 'intent_node') {
    if (outputs?.intent) {
      lines.push(`[${stepText}] 已理解需求：${getIntentLabel(outputs.intent)}`)
    } else {
      lines.push(`[${stepText}] 已完成`)
    }
    if (outputs?.sql) {
      lines.push('[查询准备] 已生成可执行的查询条件')
    }
    return lines
  }

  if (step === 'validate_sql' || step === 'sql_validate_node') {
    if (errorMessage) {
      lines.push('[查询检查] 检查未通过，系统将自动修正')
    } else {
      lines.push('[查询检查] 检查通过')
    }
    return lines
  }

  if (step === 'execute_sql' || step === 'sql_execute_node' || step === 'vector_search') {
    const resultRows = outputs?.result?.rows || outputs?.rows || outputs?.sql_result || []
    const rowCount = outputs?.row_count || (Array.isArray(resultRows) ? resultRows.length : 0)
    if (errorMessage) {
      lines.push('[数据查询] 执行遇到问题，系统将自动重试')
    } else {
      lines.push(`[数据查询] 已完成，共返回 ${rowCount} 条结果`)
    }
    return lines
  }

  if (step === 'semantic_enhance') {
    const matched = outputs?.semantic_scores ? Object.keys(outputs.semantic_scores).length : 0
    const recommended = Array.isArray(outputs?.vector_only_results) ? outputs.vector_only_results.length : 0
    lines.push(`[结果优化] 已完成，匹配 ${matched} 条，补充 ${recommended} 条`)
    return lines
  }

  if (step === 'fix_sql') {
    lines.push('[自动修正] 已修正查询条件，继续执行')
    return lines
  }

  if (step === 'format_answer' || step === 'format_node') {
    const finalMessage = outputs?.final_answer?.message || outputs?.final_answer?.answer_text || outputs?.answer_text
    if (finalMessage) {
      lines.push(`[结果整理] ${finalMessage}`)
    } else {
      lines.push('[结果整理] 已完成')
    }
    return lines
  }

  lines.push(`[${stepText}] 已完成`)
  return lines
}

const toNumber = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const normalized = value.replace(/,/g, '')
    const num = Number(normalized)
    if (Number.isFinite(num)) return num
  }
  return null
}

const isLikelyRankingView = (data?: QueryResponse | null): boolean => {
  if (!data || data.intent === 'search') return false
  const rows = data.result_rows || []
  if (rows.length < 2) return false

  const sample = rows.slice(0, 10)
  const keys = Object.keys(sample[0] || {})
  if (keys.length < 2) return false

  let numericKeyCount = 0
  for (const key of keys) {
    let valid = 0
    for (const row of sample) {
      if (toNumber((row as any)[key]) !== null) valid += 1
    }
    if (valid >= Math.ceil(sample.length * 0.6)) {
      numericKeyCount += 1
    }
  }

  return numericKeyCount >= 1
}

const getRankingMeta = (data?: QueryResponse | null) => {
  if (!data) return null
  const rows = data.result_rows || []
  if (!rows.length) return null

  const sample = rows.slice(0, 10)
  const keys = Object.keys(sample[0] || {})
  if (!keys.length) return null

  const numericKeys = keys.filter((key) => {
    let valid = 0
    for (const row of sample) {
      if (toNumber((row as any)[key]) !== null) valid += 1
    }
    return valid >= Math.ceil(sample.length * 0.6)
  })
  if (!numericKeys.length) return null

  const schemaNames = (data.result_schema || []).map((s) => s.name)
  const preferredMetric = schemaNames.find((name) => {
    const lower = name.toLowerCase()
    return (
      lower.includes('数') ||
      lower.includes('count') ||
      lower.includes('收入') ||
      lower.includes('金额') ||
      lower.includes('fee') ||
      lower.includes('值')
    ) && numericKeys.includes(name)
  })
  const metricKey = preferredMetric || numericKeys[0]

  const textKeys = keys.filter((k) => !numericKeys.includes(k))
  const preferredDimension = schemaNames.find((name) => {
    const lower = name.toLowerCase()
    return (
      lower.includes('城市') ||
      lower.includes('地区') ||
      lower.includes('渠道') ||
      lower.includes('名称') ||
      lower.includes('name')
    ) && textKeys.includes(name)
  })
  const dimensionKey = preferredDimension || textKeys[0] || keys[0]

  return {
    dimensionKey,
    metricKey,
    dimensionLabel: dimensionKey || '维度',
    metricLabel: metricKey || '指标值',
  }
}

const getRankingRows = (data?: QueryResponse | null) => {
  const rows = data?.result_rows || []
  const meta = getRankingMeta(data)
  if (!meta) return []

  const normalized = rows
    .map((row, index) => {
      const value = toNumber((row as any)[meta.metricKey]) ?? 0
      return {
        rank: index + 1,
        name: String((row as any)[meta.dimensionKey] ?? '-'),
        value,
      }
    })
    .sort((a, b) => b.value - a.value)
    .slice(0, 10)
    .map((row, index) => ({ ...row, rank: index + 1 }))

  const maxValue = normalized[0]?.value || 0
  return normalized.map((row) => ({
    ...row,
    ratio: maxValue > 0 ? Math.max(0.08, row.value / maxValue) : 0.08,
  }))
}

const formatMetricValue = (value: number) => {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
}

const extractCountValue = (data?: QueryResponse | null): number | null => {
  if (!data || data.intent !== 'count') return null

  const rows = data.result_rows || []
  if (rows.length > 0) {
    const firstRow = rows[0] as Record<string, unknown>
    for (const key of Object.keys(firstRow)) {
      const val = toNumber(firstRow[key])
      if (val !== null) return val
    }
  }

  const text = data.answer || ''
  const match = text.match(/共\s*([\d,]+(?:\.\d+)?)\s*条/)
  if (match?.[1]) {
    const parsed = toNumber(match[1])
    if (parsed !== null) return parsed
  }

  return null
}

const getSuccessSummaryText = (data?: QueryResponse | null): string => {
  if (!data) return '0 条结果'
  if (data.intent === 'count') {
    const countValue = extractCountValue(data)
    if (countValue !== null) {
      return `统计值 ${formatMetricValue(countValue)}`
    }
    return '统计已完成'
  }
  return `${data.row_count || 0} 条结果`
}

const getResultViewMode = (idx: number): ResultViewMode => {
  return resultViewModeMap.value[idx] || 'detail'
}

const getChartDomId = (idx: number) => `query-result-chart-${idx}`

const getChartFieldMeta = (data?: QueryResponse | null) => {
  if (!data) return null
  const rows = data.result_rows || []
  if (!rows.length) return null

  const sample = rows.slice(0, 20)
  const keys = Object.keys(sample[0] || {})
  if (!keys.length) return null

  const numericKeys = keys.filter((key) => {
    let valid = 0
    for (const row of sample) {
      if (toNumber((row as any)[key]) !== null) valid += 1
    }
    return valid >= Math.ceil(sample.length * 0.6)
  })
  if (!numericKeys.length) return null

  const schemaNames = (data.result_schema || []).map((s) => s.name)
  const preferredMetric = schemaNames.find((name) => {
    const lower = name.toLowerCase()
    return (
      lower.includes('数') ||
      lower.includes('count') ||
      lower.includes('收入') ||
      lower.includes('金额') ||
      lower.includes('fee') ||
      lower.includes('值')
    ) && numericKeys.includes(name)
  })
  const metricKey = preferredMetric || numericKeys[0]
  const candidateDimensionKeys = keys.filter((key) => key !== metricKey)

  const preferredDimension = schemaNames.find((name) => {
    const lower = name.toLowerCase()
    return (
      lower.includes('城市') ||
      lower.includes('地区') ||
      lower.includes('渠道') ||
      lower.includes('名称') ||
      lower.includes('name')
    ) && candidateDimensionKeys.includes(name)
  })

  const textDimension = candidateDimensionKeys.find((key) => {
    let stringCount = 0
    for (const row of sample) {
      const value = (row as any)[key]
      if (typeof value === 'string' && value.trim() !== '' && toNumber(value) === null) {
        stringCount += 1
      }
    }
    return stringCount >= Math.ceil(sample.length * 0.4)
  })

  const dimensionKey = preferredDimension || textDimension || candidateDimensionKeys[0] || metricKey

  return {
    dimensionKey,
    metricKey,
    metricLabel: metricKey || '数值',
  }
}

const canRenderChart = (data?: QueryResponse | null): boolean => {
  if (!data || data.intent === 'search') return false
  if (isLikelyRankingView(data)) return true
  return !!getChartFieldMeta(data)
}

const getChartSuggestionType = (data?: QueryResponse | null): 'bar' | 'line' | 'pie' => {
  const suggestion = String(data?.chart_suggestion || '').toLowerCase()
  if (suggestion.includes('pie')) return 'pie'
  if (suggestion.includes('line')) return 'line'
  return 'bar'
}

const buildChartOption = (data?: QueryResponse | null): echarts.EChartsOption | null => {
  if (!data) return null
  const rows = data.result_rows || []
  const fieldMeta = getChartFieldMeta(data)
  if (!rows.length || !fieldMeta) return null

  const slicedRows = rows.slice(0, 10)
  const chartData = slicedRows.map((row, index) => {
    const rawName = (row as any)[fieldMeta.dimensionKey]
    const fallbackName = slicedRows.length === 1 ? '统计值' : `第${index + 1}项`
    const name =
      fieldMeta.dimensionKey === fieldMeta.metricKey
        ? fallbackName
        : String(rawName ?? '').trim() || fallbackName
    const value = toNumber((row as any)[fieldMeta.metricKey]) ?? 0
    return { name, value }
  })

  const chartType = getChartSuggestionType(data)
  if (chartType === 'pie') {
    return {
      tooltip: { trigger: 'item' },
      legend: { bottom: 0, left: 'center' },
      series: [
        {
          name: fieldMeta.metricLabel,
          type: 'pie',
          radius: ['40%', '68%'],
          data: chartData,
          avoidLabelOverlap: true,
          itemStyle: { borderRadius: 8, borderColor: '#fff', borderWidth: 2 },
        },
      ],
    }
  }

  const xData = chartData.map((item) => item.name)
  const yData = chartData.map((item) => item.value)

  return {
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: xData, axisLabel: { interval: 0 } },
    yAxis: { type: 'value' },
    series: [
      {
        type: chartType,
        data: yData,
        smooth: chartType === 'line',
        itemStyle: {
          color: '#2f66f6',
        },
      },
    ],
    grid: { left: 40, right: 24, top: 20, bottom: 40 },
  }
}

const disposeMessageChart = (idx: number) => {
  const existing = messageChartInstances.get(idx)
  if (existing) {
    existing.dispose()
    messageChartInstances.delete(idx)
  }
}

const renderMessageChart = (idx: number, data?: QueryResponse | null) => {
  if (!data || !canRenderChart(data) || isLikelyRankingView(data)) return
  const option = buildChartOption(data)
  if (!option) return

  const chartEl = document.getElementById(getChartDomId(idx))
  if (!chartEl) return

  let chart = messageChartInstances.get(idx)
  if (!chart) {
    chart = echarts.init(chartEl)
    messageChartInstances.set(idx, chart)
  } else if (chart.getDom() !== chartEl) {
    chart.dispose()
    chart = echarts.init(chartEl)
    messageChartInstances.set(idx, chart)
  }

  chart.setOption(option, true)
  chart.resize()
}

const handleResultViewModeChange = (
  idx: number,
  mode: string | number | boolean | undefined,
  data?: QueryResponse | null
) => {
  const nextMode: ResultViewMode = mode === 'chart' ? 'chart' : 'detail'
  if (nextMode === 'chart' && !canRenderChart(data)) {
    ElMessage.warning('当前结果暂不支持图表展示')
    resultViewModeMap.value[idx] = 'detail'
    return
  }

  resultViewModeMap.value[idx] = nextMode
  if (nextMode === 'detail') {
    disposeMessageChart(idx)
    return
  }

  nextTick(() => {
    renderMessageChart(idx, data)
  })
}

const resizeAllCharts = () => {
  messageChartInstances.forEach((chart) => chart.resize())
}

// 根据日志内容返回颜色 - 类似 tieta-multi 风格
const getLogColor = (line: string): string => {
  if (line.includes('成功') || line.includes('success') || line.toLowerCase().includes('success')) {
    return '#4ade80' // 绿色
  }
  if (line.includes('失败') || line.includes('error') || line.includes('Error')) {
    return '#f87171' // 红色
  }
  if (line.startsWith('[') && line.includes(']')) {
    return '#60a5fa' // 蓝色
  }
  if (line.startsWith('===')) {
    return '#6b7280' // 灰色
  }
  return '#d1d5db' // 浅灰色
}

// 加载数据集
const loadDatasets = async () => {
  if (userStore.currentWorkspace) {
    try {
      const res = await datasetApi.list(userStore.currentWorkspace.id)
      datasets.value = res.data.filter((d: Dataset) => d.status !== 'deprecated')
      if (datasets.value.length > 0 && !selectedDataset.value) {
        selectedDataset.value = datasets.value[0]
        // 加载第一个数据集的表结构
        await handleDatasetChange(datasets.value[0])
      } else if (selectedDataset.value) {
        // 刷新当前选中数据集的表结构
        await handleDatasetChange(selectedDataset.value)
      }
    } catch (e) {
      console.error('加载数据集失败', e)
    }
  }
}

onMounted(loadDatasets)
onMounted(() => {
  window.addEventListener('resize', resizeAllCharts)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeAllCharts)
  messageChartInstances.forEach((chart) => chart.dispose())
  messageChartInstances.clear()
})

// 监听工作空间变化
watch(() => userStore.currentWorkspace, loadDatasets)

// 切换数据集
const handleDatasetChange = async (dataset: Dataset) => {
  if (!dataset) return

  selectedDataset.value = dataset
  // 切换数据集时清空已选数据表
  allTables.value.forEach(t => t.checked = false)
  selectedTables.value = []

  // 获取数据源 IDs（支持多个数据源）
  const dataSourceIds = dataset.data_source_ids || (dataset.data_source_id ? [dataset.data_source_id] : [])

  console.log('数据集:', dataset.name, '数据源 IDs:', dataSourceIds)

  if (dataSourceIds.length > 0) {
    try {
      // 获取所有数据源的表结构
      const allSchemas: SchemaColumn[] = []

      for (const dsId of dataSourceIds) {
        try {
          console.log('获取数据源', dsId, '的 schema')
          const schemaRes = await dataSourceApi.getSchema(dsId)
          console.log('schema 数据:', schemaRes.data)
          allSchemas.push(...schemaRes.data)
        } catch (e) {
          console.warn(`获取数据源 ${dsId} 的 schema 失败:`, e)
        }
      }

      // 按表名分组
      const tableMap = new Map<string, SchemaColumn[]>()
      allSchemas.forEach(col => {
        if (!tableMap.has(col.table_name)) {
          tableMap.set(col.table_name, [])
        }
        tableMap.get(col.table_name)!.push(col)
      })

      console.log('表列表:', Array.from(tableMap.keys()))

      // 转换为表格列表
      allTables.value = Array.from(tableMap.entries()).map(([tableName], index) => ({
        id: index + 1,
        name: tableName,
        label: tableName,
        checked: false,
      }))
    } catch (e) {
      ElMessage.error('获取数据表失败')
      console.error(e)
    }
  } else {
    console.log('该数据集没有关联的数据源')
  }
}

// 切换数据表选择
const handleTableCheckChange = (table: any) => {
  const idx = selectedTables.value.findIndex(t => t.id === table.id)
  if (table.checked) {
    if (idx === -1) {
      selectedTables.value.push(table)
    }
  } else {
    if (idx !== -1) {
      selectedTables.value.splice(idx, 1)
    }
  }
}

// 清空已选数据表
const clearSelectedTables = () => {
  allTables.value.forEach(t => t.checked = false)
  selectedTables.value = []
}

// 执行查询
const handleQuery = async () => {
  console.log('handleQuery called, question:', question.value)
  console.log('selectedDataset:', selectedDataset.value)
  console.log('dataset_id:', selectedDataset.value?.id)

  if (!question.value.trim()) {
    ElMessage.warning('请输入问题')
    return
  }

  if (!userStore.currentWorkspace) {
    ElMessage.warning('请先选择工作空间')
    return
  }

  const userQuestion = question.value
  question.value = ''
  loading.value = true

  // 添加用户消息
  messages.value.push({
    role: 'user',
    content: userQuestion,
  })

  // 添加一个空的助手消息，实时更新 - 使用 reactive 确保响应式
  const assistantMsg = reactive({
    role: 'assistant',
    content: '',
    data: null as any,
    thinkingLines: [] as string[],  // 实时思考过程
  })
  messages.value.push(assistantMsg)

  // 用于累积思考过程
  let thinkingContent = ''
  let lastThinkingLine = ''

  const updateThinking = (text: string) => {
    const cleanedText = text.replace(/\n+$/, '').trimEnd()
    if (!cleanedText || cleanedText === lastThinkingLine) return

    lastThinkingLine = cleanedText
    thinkingContent += cleanedText + '\n'
    // 实时更新最后一条消息
    const lastMsg = messages.value[messages.value.length - 1] as any
    if (lastMsg) {
      lastMsg.content = thinkingContent
      // 同时添加到 thinkingLines 用于实时显示
      if (!lastMsg.thinkingLines) {
        lastMsg.thinkingLines = []
      }
      lastMsg.thinkingLines.push(cleanedText)
    }
  }

  try {
    // 显示开始思考
    updateThinking('收到你的问题，正在处理中')

    // 获取用户选择的表名列表
    const selectedTableNames = selectedTables.value.map(t => t.name)
    console.log('[DEBUG] 发送查询请求, table_names:', selectedTableNames)

    const response = await queryApi.streamExecute({
      question: userQuestion,
      workspace_id: userStore.currentWorkspace.id,
      dataset_id: selectedDataset.value?.id,
      table_names: selectedTableNames.length > 0 ? selectedTableNames : undefined,
    })

    if (!response.ok) {
      throw new Error(response.statusText)
    }

    const reader = response.body?.getReader()
    const decoder = new TextDecoder()

    if (!reader) {
      throw new Error('无法读取响应')
    }

    let buffer = ''
    let finalData: any = null

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            console.log('[Query] 事件:', data.type, data.step || '', data.summary?.substring(0, 30))

            if (data.type === 'run_start') {
              updateThinking('正在理解你的需求')
            } else if (data.type === 'node_start') {
              const startText = getFriendlyStepStartText(data.step)
              if (startText) {
                updateThinking(startText)
              }
            } else if (data.type === 'node_end') {
              if (!data.outputs) {
                continue
              }

              if (data.step === 'format_node' && data.outputs?.final_answer) {
                // 格式化节点完成，提取最终结果
                finalData = data.outputs.final_answer
                console.log('[Query] 收到 format_node 结果:', JSON.stringify(finalData, null, 2))
                if (finalData?.answer_text) {
                  updateThinking('结果说明：' + finalData.answer_text)
                }
              } else {
                const endLines = getFriendlyStepEndLines(data.step, data.outputs)
                endLines.forEach(lineText => updateThinking(lineText))
              }
            } else if (data.type === 'node_error') {
              updateThinking('处理过程中出现问题：' + (data.error?.message || '执行失败'))
            } else if (data.type === 'final') {
              finalData = data.result || data.outputs
              console.log('[Query] 收到 final, outputs:', finalData)
              if (finalData?.answer_text || finalData?.message) {
                updateThinking('结果说明：' + (finalData?.answer_text || finalData?.message))
              }
              // 收到 final 后也更新消息数据
              if (finalData) {
                const lastMsg = messages.value[messages.value.length - 1]
                if (lastMsg) {
                  // 处理不同格式的数据
                  let columns: any[] = []
                  let rows: any[] = []

                  if (finalData?.value && Array.isArray(finalData.value)) {
                    // search/list 类型的数据结构
                    rows = finalData.value
                    if (rows.length > 0) {
                      columns = Object.keys(rows[0])
                    }
                  } else {
                    columns = finalData?.result?.columns || finalData?.columns || []
                    rows = finalData?.result?.rows || finalData?.rows || []
                  }

                  const rowCount = finalData?.row_count || rows.length
                  let resultSchema = columns.map((c: string) => ({ name: c, type: 'string' }))
                  if (resultSchema.length === 0 && rows.length > 0) {
                    resultSchema = Object.keys(rows[0]).map(key => ({ name: key, type: 'string' }))
                  }
                  const statusValue = finalData?.status || 'success'

                  // 处理不同意图类型
                  let intentValue = finalData?.intent || 'list'
                  if (finalData?.type === 'chat') intentValue = 'chat'
                  if (finalData?.type === 'search') intentValue = 'search'
                  if (finalData?.type === 'count') intentValue = 'count'

                  lastMsg.data = reactive<QueryResponse>({
                    question: userQuestion,
                    intent: intentValue,
                    intent_text: finalData?.intent_text || getIntentLabel(intentValue),
                    sql: finalData?.sql || '',
                    result_rows: rows,
                    result_schema: resultSchema,
                    chart_suggestion: finalData?.chart_suggestion || (intentValue === 'count' ? 'bar' : 'table'),
                    row_count: rowCount,
                    status: statusValue,
                    error: finalData?.error?.message || finalData?.message,
                    answer: finalData?.message || finalData?.answer_text || '',
                    trace_id: '',
                    audit_id: '',
                  })
                }
              }
            } else if (data.type === 'done') {
              updateThinking('处理完成，结果已返回')
              loading.value = false
            }
          } catch (e) {
            console.error('Parse error:', e)
          }
        }
      }
    }

    // 更新消息结果
    console.log('[DEBUG] finalData:', finalData)
    if (finalData) {
      const lastMsg = messages.value[messages.value.length - 1]
      if (lastMsg) {
        // 获取列名 - 从 result.columns 或 result_rows 的第一条记录
        const columns = finalData?.result?.columns || finalData?.columns || []
        const rows = finalData?.result?.rows || finalData?.rows || []
        const rowCount = finalData?.result?.row_count || finalData?.row_count || rows.length

        console.log('[DEBUG] columns:', columns)
        console.log('[DEBUG] rows:', rows)
        console.log('[DEBUG] rowCount:', rowCount)

        // 如果没有 columns 但有 rows，从 rows 获取列名
        let resultSchema = columns.map((c: string) => ({ name: c, type: 'string' }))
        if (resultSchema.length === 0 && rows.length > 0) {
          resultSchema = Object.keys(rows[0]).map(key => ({ name: key, type: 'string' }))
        }

        console.log('[DEBUG] resultSchema:', resultSchema)

        // 使用 reactive 确保响应式更新，确保 status 字段被正确设置
        const statusValue = finalData?.status || 'success'
        console.log('[DEBUG] finalData.status =', finalData?.status, '-> 使用:', statusValue)
        const responseData = reactive<QueryResponse>({
          question: userQuestion,
          intent: finalData?.intent || 'list',
          intent_text: finalData?.intent_text || getIntentLabel(finalData?.intent || 'list'),
          sql: finalData?.sql || '',
          result_rows: rows,
          result_schema: resultSchema,
          chart_suggestion: finalData?.chart_suggestion || 'table',
          row_count: rowCount,
          status: statusValue,
          error: finalData?.error?.message,
          answer: finalData?.answer_text || '',
          trace_id: '',
          audit_id: '',
        })

        // 直接更新最后一条消息的 data 属性，确保响应式
        console.log('[DEBUG] 设置 result_rows:', rows.length, 'status:', statusValue)
        lastMsg.data = responseData
        // 强制触发更新
        messages.value = [...messages.value]
        console.log('[DEBUG] messages 更新完成, data.status =', lastMsg.data?.status)
        console.log('[DEBUG] lastMsg.data:', lastMsg.data)
      }

    } else {
      console.log('[DEBUG] finalData 为空，没有结果数据')
    }
  } catch (e: any) {
    console.error('Query error:', e)
    updateThinking('[错误] ' + (e.message || '查询失败') + '\n')
    ElMessage.error(e.message || '查询失败')
    const lastMsg = messages.value[messages.value.length - 1]
    if (lastMsg) {
      lastMsg.content = '查询失败'
      lastMsg.data = { status: 'error', row_count: 0 } as QueryResponse
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="query-page">
    <!-- 中间数据集面板 -->
    <div class="dataset-panel">
      <div class="dataset-header">
        <span class="panel-label">数据集</span>
        <el-select
          v-model="selectedDataset"
          placeholder="请选择数据集"
          @change="handleDatasetChange"
          class="dataset-select"
        >
          <el-option
            v-for="ds in datasets"
            :key="ds.id"
            :label="ds.name"
            :value="ds"
          />
        </el-select>
      </div>

      <div class="table-search" v-if="selectedDataset">
        <el-input
          v-model="tableSearch"
          placeholder="搜索数据集/表"
          prefix-icon="Search"
          clearable
        />
      </div>

      <div class="table-list" v-if="selectedDataset">
        <div
          v-for="table in filteredTables"
          :key="table.id"
          class="table-item"
          @click="table.checked = !table.checked; handleTableCheckChange(table)"
        >
          <el-checkbox :model-value="table.checked" @change="table.checked = !table.checked; handleTableCheckChange(table)" />
          <span class="table-name">{{ table.label }}</span>
        </div>
      </div>
      <div class="table-empty" v-else>
        <el-icon :size="32"><InfoFilled /></el-icon>
        <span>请先选择数据集</span>
      </div>
    </div>

    <!-- 右侧主区域 -->
    <div class="main-area">
      <!-- 已选数据表标签 -->
      <div class="selected-tags" v-if="selectedTableLabels.length > 0">
        <span class="tags-label">已选:</span>
        <div class="tags-list">
          <el-tag
            v-for="(label, idx) in selectedTableLabels"
            :key="idx"
            closable
            @close="clearSelectedTables"
            class="selected-tag"
          >
            {{ label }}
          </el-tag>
        </div>
        <el-button text type="primary" size="small" @click="clearSelectedTables">
          清空
        </el-button>
      </div>

      <!-- 聊天区域 -->
      <div class="chat-container">
        <div class="chat-messages">
          <!-- 欢迎页 -->
          <div v-if="messages.length === 0" class="welcome-page">
            <h1 class="welcome-title">
              您好！欢迎使用 <span class="highlight">智能取数</span>
            </h1>
            <p class="welcome-desc">
              用自然语言描述需求，系统自动生成 SQL 并返回结果。您可以直接选择数据表后提问，也可以从下方示例开始。
            </p>

            <div class="support-section">
              <h3 class="support-title">支持数据领域</h3>
              <div class="support-list">
                <div class="support-item">
                  <span class="support-icon">✅</span>
                  <span class="support-text">用户分析：用户增长、活跃度、留存率等</span>
                </div>
                <div class="support-item">
                  <span class="support-icon">📱</span>
                  <span class="support-text">业务运营：套餐使用、流量消耗、通话时长等</span>
                </div>
                <div class="support-item">
                  <span class="support-icon">🏠</span>
                  <span class="support-text">渠道分析：各渠道获客、转化、ROI等</span>
                </div>
                <div class="support-item">
                  <span class="support-icon">🧾</span>
                  <span class="support-text">财务报表：收入、成本、利润等财务指标</span>
                </div>
                <div class="support-item">
                  <span class="support-icon">💰</span>
                  <span class="support-text">用户价值：ARPU、LTV、付费转化等</span>
                </div>
              </div>
            </div>

            <el-divider />

            <div class="quick-examples">
              <h3 class="examples-title">快捷示例</h3>
              <div class="examples-list">
                <el-button
                  v-for="(example, idx) in quickExamples"
                  :key="idx"
                  class="example-btn"
                  @click="question = example"
                >
                  {{ example }}
                </el-button>
              </div>
            </div>
          </div>

          <!-- 消息列表 -->
          <div
            v-for="(msg, idx) in messages"
            :key="idx"
            class="message"
            :class="`message-${msg.role}`"
          >
            <div class="message-content">
              <!-- 用户消息 - 无背景色，不换行 -->
              <div v-if="msg.role === 'user'" style="color: #333; padding: 8px 0; max-width: 100%; margin-left: auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                {{ msg.content }}
              </div>

              <!-- 助手消息 - 多个独立卡片 -->
              <div v-else class="assistant-cards">
                <!-- 思考过程卡片 -->
                <div v-if="msg.thinkingLines && msg.thinkingLines.length > 0" class="thinking-card">
                  <div class="card-header">
                    <div class="header-left">
                      <span class="icon-emoji">🤖</span>
                      <span class="header-title">处理进度</span>
                    </div>
                    <el-icon class="is-loading loading-icon"><Loading /></el-icon>
                  </div>
                  <div class="thinking-content">
                    <div v-for="(line, lidx) in msg.thinkingLines" :key="lidx" class="thinking-line" :style="{ color: getLogColor(line) }">
                      {{ line }}
                    </div>
                  </div>
                </div>

                <!-- 闲聊回复卡片 -->
                <div v-if="msg.data && msg.data.intent === 'chat'" class="chat-reply-card">
                  <div class="chat-reply-content">
                    <span class="chat-avatar">🤖</span>
                    <div class="chat-text">{{ msg.data.answer || msg.content }}</div>
                  </div>
                </div>

                <!-- 意图识别卡片（非闲聊） -->
                <div v-else-if="msg.data && msg.data.intent" class="intent-card">
                  <div class="intent-content">
                    <span class="icon-emoji">{{ msg.data.intent === 'chat' ? '💬' : msg.data.intent === 'search' ? '🔍' : msg.data.intent === 'count' ? '📊' : '💡' }}</span>
                    <span class="intent-text">已识别为意图: <strong>{{ msg.data.intent_text || (msg.data.intent === 'chat' ? '闲聊' : msg.data.intent === 'search' ? '向量检索' : msg.data.intent === 'count' ? '统计查询' : msg.data.intent === 'list' ? '列表查询' : msg.data.intent) }}</strong></span>
                  </div>
                </div>

                <!-- 成功状态卡片 -->
                <div v-if="msg.data && msg.data.status === 'success'" class="success-card">
                  <div class="success-content">
                    <span class="icon-emoji success-icon">✅</span>
                    <span class="success-title">查询成功</span>
                    <span class="result-count">{{ getSuccessSummaryText(msg.data) }}</span>
                  </div>
                </div>

                <!-- SQL卡片 - 可编辑 -->
                <div v-if="msg.data && msg.data.sql" class="sql-card">
                  <div class="card-header">
                    <div class="header-left">
                      <span class="icon-emoji">📝</span>
                      <span class="header-title">生成的 SQL</span>
                    </div>
                  </div>
                  <div class="sql-editor">
                    <el-input type="textarea" v-model="msg.data.sql" :rows="4" class="sql-textarea" />
                  </div>
                </div>

                <!-- 结果表格卡片 -->
                <div v-if="msg.data && msg.data.row_count > 0" class="result-card">
                  <div class="card-header">
                    <div class="header-left">
                      <span class="icon-emoji">{{ msg.data.intent === 'search' ? '🔍' : msg.data.intent === 'count' ? '📊' : '📋' }}</span>
                      <span class="header-title">{{ msg.data.intent === 'search' ? '检索结果' : msg.data.intent === 'count' ? '统计结果' : '查询结果' }}</span>
                    </div>
                    <div class="header-right">
                      <el-radio-group
                        v-if="canRenderChart(msg.data)"
                        :model-value="getResultViewMode(idx)"
                        size="small"
                        class="result-view-toggle"
                        @change="handleResultViewModeChange(idx, $event, msg.data)"
                      >
                        <el-radio-button label="detail">明细</el-radio-button>
                        <el-radio-button label="chart">图表</el-radio-button>
                      </el-radio-group>
                      <el-tag size="small" class="count-tag">{{ msg.data.row_count }} 条</el-tag>
                    </div>
                  </div>
                  <div class="table-wrapper">
                    <!-- 向量检索结果展示（包含相似度分数） -->
                    <template v-if="msg.data.intent === 'search'">
                      <el-table :data="(msg.data.result_rows || []).slice(0, 10)" border stripe size="small" max-height="350" class="result-table">
                        <el-table-column v-for="col in msg.data.result_schema" :key="col.name" :prop="col.name" :label="col.name" min-width="120" show-overflow-tooltip />
                        <el-table-column v-if="msg.data.result_rows && msg.data.result_rows[0] && msg.data.result_rows[0]._distance" label="相似度" width="100" align="center">
                          <template #default="{ row }">
                            <el-tag size="small" type="success">{{ (1 / (1 + (row._distance || 0))).toFixed(3) }}</el-tag>
                          </template>
                        </el-table-column>
                        <el-table-column v-if="msg.data.result_rows && msg.data.result_rows[0] && msg.data.result_rows[0].hybrid_score" label="综合得分" width="100" align="center">
                          <template #default="{ row }">
                            <el-tag size="small" type="success">{{ (row.hybrid_score || 0).toFixed(3) }}</el-tag>
                          </template>
                        </el-table-column>
                      </el-table>
                    </template>
                    <template v-else-if="getResultViewMode(idx) === 'chart'">
                      <div v-if="isLikelyRankingView(msg.data)" class="ranking-board">
                        <div class="ranking-header-row">
                          <span class="ranking-col-rank">排序</span>
                          <span class="ranking-col-name">{{ getRankingMeta(msg.data)?.dimensionLabel || '名称' }}</span>
                          <span class="ranking-col-value">{{ getRankingMeta(msg.data)?.metricLabel || '数值' }}</span>
                        </div>
                        <div class="ranking-list">
                          <div
                            v-for="item in getRankingRows(msg.data)"
                            :key="`${item.name}-${item.rank}`"
                            class="ranking-item"
                          >
                            <div class="ranking-rank">
                              <span v-if="item.rank <= 3" :class="['rank-badge', `rank-badge-${item.rank}`]">{{ item.rank }}</span>
                              <span v-else class="rank-num">{{ item.rank }}</span>
                            </div>
                            <div class="ranking-name" :title="item.name">{{ item.name }}</div>
                            <div class="ranking-bar-wrap">
                              <div class="ranking-bar-track">
                                <div class="ranking-bar-fill" :style="{ width: `${(item.ratio * 100).toFixed(1)}%` }" />
                              </div>
                            </div>
                            <div class="ranking-value">{{ formatMetricValue(item.value) }}</div>
                          </div>
                        </div>
                      </div>
                      <div
                        v-else
                        :id="getChartDomId(idx)"
                        class="result-chart"
                      />
                    </template>
                    <!-- 普通查询明细 -->
                    <template v-else>
                      <el-table :data="(msg.data.result_rows || []).slice(0, 10)" border stripe size="small" max-height="350" class="result-table">
                        <el-table-column v-for="col in msg.data.result_schema" :key="col.name" :prop="col.name" :label="col.name" min-width="120" show-overflow-tooltip />
                      </el-table>
                    </template>
                    <div v-if="msg.data.row_count > 10 && getResultViewMode(idx) !== 'chart'" class="more-hint">
                      仅显示前 10 行，滑动查看更多（共 {{ msg.data.row_count }} 行）
                    </div>
                  </div>
                </div>

                <!-- 失败状态 -->
                <div v-if="msg.data && msg.data.status === 'error'" class="error-card">
                  <div class="error-content">
                    <span class="icon-emoji error-icon">❌</span>
                    <span class="error-title">查询失败</span>
                  </div>
                  <div v-if="msg.data.error" class="error-detail">{{ msg.data.error }}</div>
                </div>

                <!-- 加载中提示 -->
                <div v-if="(!msg.data || !msg.data.status) && msg.thinkingLines && msg.thinkingLines.length > 0" class="loading-card">
                  <el-icon class="is-loading loading-icon"><Loading /></el-icon>
                  <span class="loading-text">正在处理中...</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 输入区域 -->
        <div class="chat-input">
          <div class="input-container">
            <div class="input-wrapper">
              <el-input
                v-model="question"
                type="textarea"
                placeholder="只需自然语言描述需求..."
                :rows="3"
                :autosize="{ minRows: 2, maxRows: 6 }"
                resize="none"
                :loading="loading"
                @keydown.enter.exact.prevent="handleQuery"
              />
              <el-button
                type="primary"
                class="send-btn"
                :loading="loading"
                :disabled="!question.trim()"
                @click="handleQuery"
              >
                <el-icon><Promotion /></el-icon>
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.query-page {
  height: 100%;
  display: flex;
  background-color: #f5f5f5;
  position: relative;
}

// 左侧边栏
.sidebar {
  width: 220px;
  background-color: #fff;
  border-right: 1px solid #e8eaed;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: relative;
  z-index: 10;
}

.sidebar-header {
  padding: 16px;
  border-bottom: 1px solid #e8eaed;
}

.logo-section {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
}

.logo-icon {
  color: #409eff;
}

.logo-text {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.new-session-btn {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

// 导航菜单
.nav-section {
  padding: 12px 8px;
  border-bottom: 1px solid #e8eaed;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  color: #606266;
  transition: all 0.2s ease;
  font-size: 14px;

  &:hover {
    background-color: #f5f7fa;
    color: #303133;
  }

  &.active {
    background-color: #ecf5ff;
    color: #409eff;
  }
}

.history-section {
  flex: 1;
  padding: 12px 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  color: #909399;
  font-size: 12px;
  font-weight: 500;
}

.history-list {
  flex: 1;
  overflow-y: auto;
  margin-top: 8px;
}

.history-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background-color: #f5f7fa;
  }
}

.history-title {
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.history-time {
  font-size: 11px;
  color: #909399;
}

// 历史会话空状态
.history-empty {
  padding: 20px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}

// 中间数据集面板
.dataset-panel {
  width: 260px;
  background-color: #fff;
  border-right: 1px solid #e8eaed;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: relative;
  z-index: 10;
}

.dataset-header {
  padding: 16px;
  border-bottom: 1px solid #e8eaed;
}

.panel-label {
  display: block;
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
  font-weight: 500;
}

.dataset-select {
  width: 100%;
}

.table-search {
  padding: 12px 16px;
  border-bottom: 1px solid #e8eaed;
}

.table-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

.table-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #909399;
  font-size: 14px;
  padding: 40px 20px;
}

.table-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background-color: #f5f7fa;
  }

  .table-name {
    font-size: 13px;
    color: #303133;
  }
}

// 右侧主区域
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

.mobile-buttons {
  display: none;
  position: absolute;
  top: 16px;
  left: 16px;
  z-index: 5;
  gap: 8px;
}

.selected-tags {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  background-color: #fff;
  border-bottom: 1px solid #e8eaed;
  flex-shrink: 0;
}

.tags-label {
  font-size: 13px;
  color: #606266;
  font-weight: 500;
}

.tags-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  flex: 1;
}

.selected-tag {
  background-color: #ecf5ff;
  border-color: #b3d8ff;
  color: #409eff;
}

// 聊天容器
.chat-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  background-color: #f5f5f5;
}

// 欢迎页
.welcome-page {
  max-width: 720px;
  margin: 0 auto;
  padding: 20px 0;
}

.welcome-title {
  font-size: 32px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 16px;
  line-height: 1.4;

  .highlight {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #409eff 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }
}

.welcome-desc {
  font-size: 15px;
  color: #606266;
  line-height: 1.8;
  margin-bottom: 32px;
}

.support-section {
  margin-bottom: 32px;
}

.support-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 16px;
}

.support-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.support-item {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 14px;
  color: #606266;
}

.support-icon {
  font-size: 16px;
  width: 24px;
  text-align: center;
}

.quick-examples {
  margin-top: 24px;
}

.examples-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 16px;
}

.examples-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.example-btn {
  text-align: left;
  justify-content: flex-start;
  padding: 12px 16px;
  height: auto;
  white-space: normal;
  line-height: 1.5;
  color: #606266;
  background-color: #f5f7fa;
  border-color: #e8eaed;

  &:hover {
    background-color: #ecf5ff;
    border-color: #b3d8ff;
    color: #409eff;
  }
}

// 消息样式
.message {
  margin-bottom: 20px;
  display: flex;
  animation: fadeIn 0.3s ease;
  width: 100%;

  &.message-user {
    justify-content: flex-end;

    .message-content {
      background-color: #409eff;
      color: #fff;
      border-radius: 16px 16px 4px 16px;
      box-shadow: 0 2px 8px rgba(64, 158, 255, 0.3);
      padding: 12px 16px;
    }
  }

  &.message-assistant {
    justify-content: flex-end;

    .message-content {
      background-color: #fff;
      border: 1px solid #e4e7ed;
      border-radius: 12px;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
      padding: 16px;
      max-width: 100%;
      width: 100%;
    }
  }
}

// 助手消息卡片容器
.assistant-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
  max-width: 100%;
}

// 思考过程卡片
.thinking-card {
  background: white;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  border: none;

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;

    .header-left {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .icon-emoji {
      font-size: 20px;
    }

    .header-title {
      color: #333;
      font-weight: 600;
      font-size: 14px;
    }

    .loading-icon {
      color: #409eff;
      font-size: 16px;
    }
  }

  .thinking-content {
    background: #f8f9fa;
    border-radius: 8px;
    padding: 12px;
    max-height: 400px;
    overflow-y: auto;
    font-family: 'Monaco', 'Menlo', monospace;
    font-size: 12px;
    line-height: 1.8;

    .thinking-line {
      color: #666;
    }
  }
}

// 意图识别卡片
.intent-card {
  background: #e6f7ff;
  border-radius: 12px;
  padding: 12px 16px;
  border-left: 4px solid #1890ff;

  .intent-content {
    display: flex;
    align-items: center;
    gap: 8px;

    .icon-emoji {
      font-size: 16px;
    }

    .intent-text {
      color: #1890ff;
      font-weight: 500;
      font-size: 14px;

      strong {
        font-weight: 600;
      }
    }
  }
}

// SQL卡片
.sql-card {
  background: #fff7e6;
  border-radius: 12px;
  padding: 16px;
  border: 1px solid #ffd591;

  .card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;

    .header-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .icon-emoji {
      font-size: 16px;
    }

    .header-title {
      color: #fa8c16;
      font-weight: 600;
      font-size: 14px;
    }
  }

  .sql-editor {
    .sql-textarea {
      :deep(.el-textarea__inner) {
        font-family: 'Monaco', 'Menlo', monospace;
        font-size: 13px;
        background: white;
        border: 1px solid #d9d9d9;
        border-radius: 6px;
      }
    }
  }
}

// 成功状态卡片
.success-card {
  background: #f6ffed;
  border-radius: 12px;
  padding: 12px 16px;
  border: 1px solid #b7eb8f;

  .success-content {
    display: flex;
    align-items: center;
    gap: 8px;

    .success-icon {
      font-size: 18px;
    }

    .success-title {
      color: #52c41a;
      font-weight: 600;
      font-size: 16px;
    }

    .result-count {
      color: #666;
      margin-left: 8px;
      font-size: 14px;
    }
  }
}

// 结果表格卡片
.result-card {
  background: white;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  border: 1px solid #e8e8e8;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;

    .header-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .icon-emoji {
      font-size: 16px;
    }

    .header-title {
      color: #333;
      font-weight: 600;
      font-size: 14px;
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .result-view-toggle {
      :deep(.el-radio-button__inner) {
        padding: 5px 12px;
        border-radius: 16px;
      }
    }

    .count-tag {
      background: #f0f0f0;
      border-color: #d9d9d9;
      color: #666;
    }
  }

  .table-wrapper {
    .ranking-board {
      border: 1px solid #e8edf5;
      border-radius: 12px;
      overflow: hidden;
      background: #ffffff;
    }

    .ranking-header-row {
      display: grid;
      grid-template-columns: 90px minmax(180px, 1fr) 120px;
      align-items: center;
      gap: 16px;
      padding: 12px 16px;
      background: linear-gradient(90deg, #f6f9ff 0%, #eef4ff 100%);
      color: #44536a;
      font-weight: 600;
      font-size: 13px;
      border-bottom: 1px solid #edf1f8;
    }

    .ranking-list {
      max-height: 360px;
      overflow-y: auto;
    }

    .ranking-item {
      display: grid;
      grid-template-columns: 90px minmax(180px, 1fr) 1fr 120px;
      align-items: center;
      gap: 16px;
      padding: 14px 16px;
      border-bottom: 1px solid #f0f3f8;

      &:nth-child(odd) {
        background: #fbfdff;
      }

      &:last-child {
        border-bottom: none;
      }
    }

    .ranking-rank {
      display: flex;
      align-items: center;
      justify-content: center;

      .rank-badge {
        width: 28px;
        height: 28px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 13px;
        font-weight: 700;
        color: #fff;
      }

      .rank-badge-1 {
        background: linear-gradient(135deg, #f5a623 0%, #f8c255 100%);
        box-shadow: 0 4px 8px rgba(245, 166, 35, 0.35);
      }

      .rank-badge-2 {
        background: linear-gradient(135deg, #4c8cff 0%, #74a6ff 100%);
        box-shadow: 0 4px 8px rgba(76, 140, 255, 0.35);
      }

      .rank-badge-3 {
        background: linear-gradient(135deg, #f28f45 0%, #f6b17e 100%);
        box-shadow: 0 4px 8px rgba(242, 143, 69, 0.3);
      }

      .rank-num {
        font-size: 18px;
        font-weight: 600;
        color: #4e5b70;
      }
    }

    .ranking-name {
      color: #1f2d3d;
      font-size: 18px;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .ranking-bar-wrap {
      display: flex;
      align-items: center;
      width: 100%;
      min-width: 120px;
    }

    .ranking-bar-track {
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: #e7edf8;
      overflow: hidden;
      position: relative;
    }

    .ranking-bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2f66f6 0%, #3a7bff 100%);
      box-shadow: inset 0 0 6px rgba(21, 76, 214, 0.25);
      transition: width 0.45s ease;
    }

    .ranking-value {
      justify-self: end;
      color: #1f2d3d;
      font-size: 32px;
      font-weight: 700;
      letter-spacing: 0.5px;
      line-height: 1;
    }

    .result-table {
      width: 100%;
      border-radius: 8px;
      overflow: hidden;
    }

    .result-chart {
      width: 100%;
      height: 340px;
      border: 1px solid #e8edf5;
      border-radius: 12px;
      background: #fff;
    }

    .more-hint {
      text-align: center;
      padding: 8px;
      color: #909399;
      font-size: 12px;
      background: #f5f7fa;
      border-radius: 0 0 8px 8px;
    }
  }
}

// 失败状态卡片
.error-card {
  background: #fff2f0;
  border-radius: 12px;
  padding: 16px;
  border: 1px solid #ffccc7;

  .error-content {
    display: flex;
    align-items: center;
    gap: 8px;

    .error-icon {
      font-size: 18px;
    }

    .error-title {
      color: #ff4d4f;
      font-weight: 600;
      font-size: 16px;
    }
  }

  .error-detail {
    margin-top: 8px;
    color: #666;
    font-size: 14px;
  }
}

// 加载中卡片
.loading-card {
  text-align: center;
  color: #999;
  padding: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;

  .loading-icon {
    font-size: 16px;
  }

  .loading-text {
    font-size: 14px;
  }
}

// @keyframes 定义
@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.message-content {
  max-width: 100%;
  padding: 14px 18px;
  word-wrap: break-word;
  line-height: 1.6;
}

.result-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  padding: 12px 16px;
  background: linear-gradient(135deg, #f0f9eb 0%, #e1f3d8 100%);
  border-radius: 8px;
  border: 1px solid #67c23a;
}

.result-info {
  font-size: 14px;
  color: #67c23a;
  font-weight: 500;
}

/* 思考过程区域 - 黑色背景类似 aaa.jpg */
.agent-thinking-section {
  margin: 16px 0;
  border-radius: 8px;
  overflow: hidden;
  background: #1e1e1e;
  border: 1px solid #333;
}

.agent-thinking-section .section-header {
  padding: 12px 16px;
  background: #2d2d2d;
  border-bottom: 1px solid #333;
}

.agent-thinking-section .section-title {
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}

.agent-thinking-section .thinking-content {
  padding: 16px;
  max-height: 300px;
  overflow-y: auto;
}

.agent-thinking-section .thinking-content pre {
  margin: 0;
  color: #9cdcfe;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

/* SQL 详情区域 */
.sql-section {
  margin: 16px 0;
  border-radius: 8px;
  overflow: hidden;
  background: #f5f7fa;
  border: 1px solid #dcdfe6;
}

.sql-section .section-header {
  padding: 12px 16px;
  background: #ebeef5;
  border-bottom: 1px solid #dcdfe6;
  display: flex;
  align-items: center;
}

.sql-section .section-title {
  color: #303133;
  font-size: 14px;
  font-weight: 600;
}

.sql-section .sql-content {
  padding: 16px;
}

.sql-section .sql-content pre {
  margin: 0;
  color: #e6a23c;
  font-size: 13px;
  font-family: 'Monaco', 'Menlo', monospace;
  line-height: 1.5;
}

/* 结果表格区域 */
.result-table-section {
  margin: 16px 0;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #dcdfe6;
}

.result-table-section .section-header {
  padding: 12px 16px;
  background: #f5f7fa;
  border-bottom: 1px solid #dcdfe6;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.result-table-section .section-title {
  color: #303133;
  font-size: 14px;
  font-weight: 600;
}

.sql-section {
  margin: 12px 0;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  overflow: hidden;
}

.sql-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background-color: #f5f7fa;
  cursor: pointer;
  user-select: none;

  &:hover {
    background-color: #ebeef5;
  }

  .arrow {
    margin-left: auto;
  }
}

.sql-content {
  padding: 12px;
  background-color: #fff;
}

.sql-item {
  margin-bottom: 12px;

  &:last-child {
    margin-bottom: 0;
  }
}

.sql-label {
  font-weight: 600;
  color: #606266;
  margin-bottom: 6px;
  font-size: 12px;
}

.sql-display {
  background-color: #f5f7fa;
  padding: 10px;
  border-radius: 4px;
  font-family: 'Monaco', 'Menlo', monospace;
  font-size: 12px;
  margin: 0;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.reasoning-section,
.agent-steps {
  margin: 12px 0;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  overflow: hidden;
}

.reasoning-header,
.steps-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background-color: #f0f9ff;
  cursor: pointer;
  user-select: none;

  &:hover {
    background-color: #e0f2fe;
  }

  .arrow {
    margin-left: auto;
  }
}

.reasoning-content {
  padding: 12px;
  background-color: #fff;
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
}

.steps-content {
  background-color: #fafafa;
  max-height: 400px;
  overflow-y: auto;
}

.step-item {
  padding: 12px;
  border-bottom: 1px solid #ebeef5;

  &:last-child {
    border-bottom: none;
  }
}

.step-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 8px;

  &.thought {
    background-color: #e6f7ff;
    color: #1890ff;
    border: 1px solid #91d5ff;
  }

  &.action {
    background-color: #fff7e6;
    color: #fa8c16;
    border: 1px solid #ffd591;
  }

  &.observation {
    background-color: #f6ffed;
    color: #52c41a;
    border: 1px solid #b7eb8f;
  }
}

.step-text {
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
  padding-left: 4px;
}

.step-input {
  margin-top: 8px;
  padding: 8px;
  background-color: #f5f5f5;
  border-radius: 4px;

  pre {
    margin: 0;
    font-size: 12px;
    font-family: 'Monaco', 'Menlo', monospace;
    white-space: pre-wrap;
    word-break: break-all;
    color: #e6a23c;
  }
}

.observation-text {
  color: #67c23a;
}

.result-table {
  margin-top: 16px;
}

.chart-type-selector {
  margin-bottom: 12px;
  display: flex;
  gap: 10px;
}

.chart-container {
  width: 100%;
  height: 400px;
}

.error-message {
  margin-top: 12px;
}

.warnings {
  margin-top: 12px;
}

.chat-input {
  padding: 16px 24px 20px;
  background-color: #fff;
  border-top: 1px solid #e8eaed;
  flex-shrink: 0;
}

.input-container {
  max-width: 100%;
  margin: 0 auto;
}

.input-wrapper {
  display: flex;
  gap: 12px;
  align-items: flex-end;
  background: #f5f7fa;
  border: 2px solid #e4e7ed;
  border-radius: 12px;
  padding: 12px 16px;
  transition: all 0.3s ease;

  &:hover {
    border-color: #c0c4cc;
  }

  &:focus-within {
    border-color: #409eff;
    box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.1);
  }

  :deep(.el-textarea__inner) {
    background: transparent;
    border: none;
    font-size: 15px;
    line-height: 1.6;
    padding: 4px 0;

    &:focus {
      box-shadow: none;
    }
  }
}

.send-btn {
  height: auto;
  padding: 10px 14px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.3s ease;

  &:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(64, 158, 255, 0.3);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

// 移动端响应式
.mobile-overlay {
  display: none;
}

@media (max-width: 1024px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 60px;
    bottom: 0;
    transform: translateX(-100%);
    transition: transform 0.3s ease;
    z-index: 100;

    &.mobile-show {
      transform: translateX(0);
    }
  }

  .dataset-panel {
    position: fixed;
    left: 0;
    top: 60px;
    bottom: 0;
    transform: translateX(-100%);
    transition: transform 0.3s ease;
    z-index: 100;

    &.mobile-show {
      transform: translateX(0);
    }
  }

  .mobile-overlay {
    display: block;
    position: fixed;
    top: 60px;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 99;
  }

  .mobile-buttons {
    display: flex;
  }

  .main-area {
    margin-left: 0;
  }
}

@media (max-width: 768px) {
  .welcome-title {
    font-size: 24px;
  }

  .welcome-desc {
    font-size: 14px;
  }

  .message-content {
    max-width: 100%;
    width: 100%;
  }

  .result-card .table-wrapper {
    .result-chart {
      height: 260px;
    }

    .ranking-header-row {
      grid-template-columns: 64px minmax(120px, 1fr) 90px;
      gap: 10px;
      font-size: 12px;
      padding: 10px 12px;
    }

    .ranking-item {
      grid-template-columns: 64px minmax(120px, 1fr) 1fr 90px;
      gap: 10px;
      padding: 10px 12px;
    }

    .ranking-rank .rank-badge {
      width: 22px;
      height: 22px;
      font-size: 12px;
    }

    .ranking-rank .rank-num {
      font-size: 15px;
    }

    .ranking-name {
      font-size: 14px;
    }

    .ranking-bar-track {
      height: 9px;
    }

    .ranking-value {
      font-size: 16px;
    }
  }
}
</style>

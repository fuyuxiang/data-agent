<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { annotationApi } from '@/api'
import type {
  AnnotationBox,
  AnnotationClassInfo,
  AnnotationItem,
  AnnotationMeta,
  AnnotationSession,
} from '@/api/types'
import { ElMessage } from 'element-plus'

const userStore = useUserStore()

const loadingMeta = ref(false)
const scanning = ref(false)
const sessionLoading = ref(false)
const actionLoading = ref(false)
const session = ref<AnnotationSession | null>(null)
const meta = ref<AnnotationMeta | null>(null)

const sourceForm = reactive({
  media_type: 'image' as 'image' | 'video',
  source_dir: '',
  output_dir: '',
  use_tracking: true,
  frame_interval: 1,
  detect_size: 640,
  force_reprocess: false,
})

const selectedClass = ref('car')
const showAuto = ref(true)
const showManual = ref(true)
const showConf = ref(false)
const classFilter = ref('全部')
const confThreshold = ref(0.1)
const drawStart = ref<{ x: number; y: number } | null>(null)
const manualEditMode = ref(false)
const currentMediaUrl = ref('')
const previewMediaUrl = ref('')
const previewMissing = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

const classList = computed<AnnotationClassInfo[]>(() => meta.value?.classes || [])
const currentIndex = computed(() => session.value?.current_index || 0)
const currentItem = computed<AnnotationItem | null>(() => {
  if (!session.value?.items?.length) return null
  return session.value.items[currentIndex.value] || null
})

const currentAnnotations = computed<AnnotationBox[]>(() => currentItem.value?.annotations || [])
const showImagePreview = computed(() => {
  return (
    session.value?.media_type === 'image'
    && currentItem.value?.status === 'ready'
    && !!previewMediaUrl.value
    && !manualEditMode.value
    && !drawStart.value
  )
})
const displayedImageUrl = computed(() => {
  return showImagePreview.value ? previewMediaUrl.value : currentMediaUrl.value
})

const visibleAnnotations = computed<AnnotationBox[]>(() => {
  return currentAnnotations.value.filter((annotation) => {
    if (annotation.manual && !showManual.value) return false
    if (!annotation.manual && !showAuto.value) return false
    if (!annotation.manual && annotation.confidence < confThreshold.value) return false
    if (classFilter.value !== '全部' && annotation.class !== classFilter.value) return false
    return true
  })
})

const imageStats = computed(() => {
  const total = currentAnnotations.value.length
  const auto = currentAnnotations.value.filter(item => !item.manual).length
  const manual = total - auto
  return { total, auto, manual }
})

const summarizeAnnotations = (annotations: AnnotationBox[]) => {
  const total = annotations.length
  const auto = annotations.filter(item => !item.manual).length
  const manual = total - auto
  const byClass: Record<string, number> = {}
  annotations.forEach((annotation) => {
    byClass[annotation.class] = (byClass[annotation.class] || 0) + 1
  })
  return {
    total,
    auto,
    manual,
    by_class: byClass,
  }
}

const videoStats = computed(() => {
  const stats = currentItem.value?.stats || {}
  return {
    trackedFrames: Number(stats.tracked_frames || 0),
    peakTracks: Number(stats.peak_tracks || 0),
    totalFrames: Number(stats.total_frames || 0),
    fps: Number(stats.fps || 0),
  }
})

const classSummaryText = computed(() => {
  const byClass = currentItem.value?.stats?.by_class || {}
  const entries = Object.entries(byClass as Record<string, number>)
  if (!entries.length) return ''
  return entries
    .sort((left, right) => right[1] - left[1])
    .map(([key, count]) => `${getClassLabel(key)} ${count}`)
    .join(' / ')
})

const pageStatusType = computed(() => {
  if (!session.value) return 'info'
  if (session.value.status === 'failed') return 'error'
  if (session.value.status === 'processing' || session.value.status === 'pending') return 'warning'
  return 'success'
})

const pageStatusText = computed(() => {
  if (!session.value) return '未开始'
  if (session.value.status === 'failed') return '处理失败'
  if (session.value.status === 'processing' || session.value.status === 'pending') return session.value.progress.message
  return '标注完成'
})

const getClassLabel = (className: string) => {
  return classList.value.find(item => item.class_name === className)?.label || className
}

const getClassId = (className: string) => {
  return classList.value.find(item => item.class_name === className)?.class_id || 0
}

const revokeBlobUrl = (target: 'current' | 'preview') => {
  const value = target === 'current' ? currentMediaUrl.value : previewMediaUrl.value
  if (value) URL.revokeObjectURL(value)
  if (target === 'current') currentMediaUrl.value = ''
  else previewMediaUrl.value = ''
}

const clearSessionMedia = () => {
  revokeBlobUrl('current')
  revokeBlobUrl('preview')
  previewMissing.value = false
}

const saveLocalPreference = () => {
  localStorage.setItem(`annotation_source_dir_${sourceForm.media_type}`, sourceForm.source_dir)
  localStorage.setItem('annotation_output_dir', sourceForm.output_dir)
}

const applyDefaults = () => {
  if (!meta.value) return
  const mediaKey = sourceForm.media_type === 'video' ? 'video_dir' : 'image_dir'
  sourceForm.source_dir = localStorage.getItem(`annotation_source_dir_${sourceForm.media_type}`) || meta.value.defaults[mediaKey] || ''
  sourceForm.output_dir = localStorage.getItem('annotation_output_dir') || meta.value.defaults.output_dir || ''
  selectedClass.value = classList.value[0]?.class_name || 'car'
}

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const refreshSession = async (silent = false) => {
  if (!session.value || !userStore.currentWorkspace) return
  try {
    const response = await annotationApi.getSession(session.value.id, userStore.currentWorkspace.id)
    session.value = response.data
  } catch (error: any) {
    if (!silent) ElMessage.error(error.response?.data?.detail || '刷新标注状态失败')
    stopPolling()
  }
}

const syncPolling = () => {
  if (!session.value) {
    stopPolling()
    return
  }
  if (session.value.status === 'pending' || session.value.status === 'processing') {
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        void refreshSession(true)
      }, 2000)
    }
    return
  }
  stopPolling()
}

const loadItemMedia = async () => {
  clearSessionMedia()
  drawStart.value = null
  if (!session.value || !currentItem.value || !userStore.currentWorkspace) return

  try {
    if (session.value.media_type === 'image') {
      if (currentItem.value.status === 'ready') {
        try {
          const previewResponse = await annotationApi.getItemFile(session.value.id, currentItem.value.id, userStore.currentWorkspace.id, 'preview')
          previewMediaUrl.value = URL.createObjectURL(previewResponse.data)
        } catch {
          previewMediaUrl.value = ''
        }
      }
      const sourceResponse = await annotationApi.getItemFile(session.value.id, currentItem.value.id, userStore.currentWorkspace.id, 'source')
      currentMediaUrl.value = URL.createObjectURL(sourceResponse.data)
      return
    }

    try {
      const previewResponse = await annotationApi.getItemFile(session.value.id, currentItem.value.id, userStore.currentWorkspace.id, 'preview')
      previewMediaUrl.value = URL.createObjectURL(previewResponse.data)
    } catch {
      previewMissing.value = true
    }

    const sourceResponse = await annotationApi.getItemFile(session.value.id, currentItem.value.id, userStore.currentWorkspace.id, 'source')
    currentMediaUrl.value = URL.createObjectURL(sourceResponse.data)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '加载媒体文件失败')
  }
}

const loadMeta = async () => {
  loadingMeta.value = true
  try {
    const response = await annotationApi.getMeta()
    meta.value = response.data
    applyDefaults()
    await tryRestoreSession()
  } catch (error) {
    ElMessage.error('加载智能标注配置失败')
  } finally {
    loadingMeta.value = false
  }
}

const tryRestoreSession = async () => {
  if (!userStore.currentWorkspace || !sourceForm.source_dir) {
    session.value = null
    return
  }

  try {
    const response = await annotationApi.restore(userStore.currentWorkspace.id, sourceForm.media_type, sourceForm.source_dir)
    session.value = response.data
  } catch {
    session.value = null
  }
}

const handleScan = async () => {
  if (!userStore.currentWorkspace) {
    ElMessage.warning('请先选择工作空间')
    return
  }
  if (!sourceForm.source_dir.trim()) {
    ElMessage.warning('请输入目录路径')
    return
  }
  scanning.value = true
  saveLocalPreference()
  try {
    const response = await annotationApi.scan({
      workspace_id: userStore.currentWorkspace.id,
      media_type: sourceForm.media_type,
      source_dir: sourceForm.source_dir.trim(),
      output_dir: sourceForm.output_dir.trim(),
      use_tracking: sourceForm.use_tracking,
      frame_interval: sourceForm.frame_interval,
      detect_size: sourceForm.detect_size,
      force_reprocess: sourceForm.force_reprocess,
    })
    session.value = response.data
    if (response.data.restored) {
      ElMessage.success('已恢复上次标注状态')
    } else {
      ElMessage.success(sourceForm.media_type === 'image' ? '图片扫描已开始' : '视频标注已开始')
    }
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '启动智能标注失败')
  } finally {
    scanning.value = false
  }
}

const updateCursor = async (index: number) => {
  if (!session.value || !userStore.currentWorkspace) return
  sessionLoading.value = true
  try {
    const response = await annotationApi.updateCursor(session.value.id, userStore.currentWorkspace.id, index)
    session.value = response.data
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '切换文件失败')
  } finally {
    sessionLoading.value = false
  }
}

const goPrev = () => {
  if (!session.value?.items?.length) return
  const nextIndex = (currentIndex.value - 1 + session.value.items.length) % session.value.items.length
  void updateCursor(nextIndex)
}

const goNext = () => {
  if (!session.value?.items?.length) return
  const nextIndex = (currentIndex.value + 1) % session.value.items.length
  void updateCursor(nextIndex)
}

const replaceCurrentItem = (updatedItem: AnnotationItem) => {
  if (!session.value) return
  const nextItems = session.value.items.map(item => item.id === updatedItem.id ? updatedItem : item)
  session.value = {
    ...session.value,
    items: nextItems,
  }
}

const persistAnnotations = async (nextAnnotations: AnnotationBox[]) => {
  if (!session.value || !currentItem.value || !userStore.currentWorkspace) return
  const previousItem = JSON.parse(JSON.stringify(currentItem.value)) as AnnotationItem
  replaceCurrentItem({
    ...previousItem,
    annotations: nextAnnotations,
    status: 'ready',
    stats: summarizeAnnotations(nextAnnotations),
  })
  actionLoading.value = true
  try {
    const response = await annotationApi.updateItemAnnotations(
      session.value.id,
      currentItem.value.id,
      userStore.currentWorkspace.id,
      nextAnnotations,
    )
    replaceCurrentItem(response.data)
    await loadItemMedia()
  } catch (error: any) {
    replaceCurrentItem(previousItem)
    ElMessage.error(error.response?.data?.detail || '保存标注失败')
  } finally {
    actionLoading.value = false
  }
}

const handleImageClick = async (event: MouseEvent) => {
  if (!session.value || session.value.media_type !== 'image' || !currentItem.value || !currentMediaUrl.value) return
  manualEditMode.value = true
  const target = event.currentTarget as HTMLElement | null
  if (!target) return
  const rect = target.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const x = (event.clientX - rect.left) / rect.width
  const y = (event.clientY - rect.top) / rect.height
  if (x < 0 || x > 1 || y < 0 || y > 1) return

  if (!drawStart.value) {
    drawStart.value = { x, y }
    return
  }

  const start = drawStart.value
  drawStart.value = null
  const x1 = Math.min(start.x, x)
  const y1 = Math.min(start.y, y)
  const x2 = Math.max(start.x, x)
  const y2 = Math.max(start.y, y)
  const width = currentItem.value.width || 0
  const height = currentItem.value.height || 0
  if ((x2 - x1) * width < 5 || (y2 - y1) * height < 5) {
    ElMessage.warning('框太小，请重新绘制')
    return
  }

  const nextAnnotations = [
    ...currentAnnotations.value,
    {
      class: selectedClass.value,
      class_id: getClassId(selectedClass.value),
      confidence: 1,
      bbox: [x1, y1, x2, y2],
      manual: true,
    },
  ]
  await persistAnnotations(nextAnnotations)
}

const cancelDrawing = () => {
  drawStart.value = null
}

const updateAnnotationClass = async (index: number, className: string) => {
  manualEditMode.value = true
  const nextAnnotations = currentAnnotations.value.map((annotation, annotationIndex) => {
    if (annotationIndex !== index) return annotation
    return {
      ...annotation,
      class: className,
      class_id: getClassId(className),
    }
  })
  await persistAnnotations(nextAnnotations)
}

const deleteAnnotation = async (index: number) => {
  manualEditMode.value = true
  const nextAnnotations = currentAnnotations.value.filter((_annotation, annotationIndex) => annotationIndex !== index)
  await persistAnnotations(nextAnnotations)
}

const clearAnnotations = async () => {
  manualEditMode.value = true
  await persistAnnotations([])
}

const saveCurrentItem = async () => {
  if (!session.value || !currentItem.value || !userStore.currentWorkspace) return
  actionLoading.value = true
  try {
    const response = await annotationApi.saveItem(session.value.id, currentItem.value.id, userStore.currentWorkspace.id)
    ElMessage.success(response.data.message)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '保存当前标签失败')
  } finally {
    actionLoading.value = false
  }
}

const saveAllItems = async () => {
  if (!session.value || !userStore.currentWorkspace) return
  actionLoading.value = true
  try {
    const response = await annotationApi.exportSession(session.value.id, userStore.currentWorkspace.id)
    ElMessage.success(response.data.message)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '批量保存失败')
  } finally {
    actionLoading.value = false
  }
}

const downloadPreviewVideo = async () => {
  if (!session.value || !currentItem.value || !userStore.currentWorkspace) return
  try {
    const response = await annotationApi.getItemFile(session.value.id, currentItem.value.id, userStore.currentWorkspace.id, 'preview')
    const url = URL.createObjectURL(response.data)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${currentItem.value.file_name.replace(/\.[^.]+$/, '')}_tracked.mp4`
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '下载预览视频失败')
  }
}

const annotationRowLabel = (annotation: AnnotationBox) => {
  return `${annotation.manual ? '手动' : '自动'} · ${getClassLabel(annotation.class)}`
}

const annotationCoordText = (annotation: AnnotationBox) => {
  const width = currentItem.value?.width || 0
  const height = currentItem.value?.height || 0
  const [x1, y1, x2, y2] = annotation.bbox
  return [
    Math.round(x1 * width),
    Math.round(y1 * height),
    Math.round(x2 * width),
    Math.round(y2 * height),
  ].join(', ')
}

const getBoxStyle = (annotation: AnnotationBox) => {
  const [x1, y1, x2, y2] = annotation.bbox
  return {
    left: `${x1 * 100}%`,
    top: `${y1 * 100}%`,
    width: `${(x2 - x1) * 100}%`,
    height: `${(y2 - y1) * 100}%`,
    borderColor: getBoxColor(annotation.class),
  }
}

const getBoxColor = (className: string) => {
  const palette: Record<string, string> = {
    person: '#31c48d',
    car: '#3b82f6',
    truck: '#f97316',
    bus: '#8b5cf6',
    van: '#38bdf8',
    motorcycle: '#ec4899',
    bicycle: '#06b6d4',
    excavator: '#f59e0b',
    bulldozer: '#14b8a6',
    'dump truck': '#e11d48',
    tractor: '#8b5cf6',
    trailer: '#facc15',
  }
  return palette[className] || '#ef4444'
}

watch(() => session.value?.status, () => {
  syncPolling()
})

watch(() => currentItem.value?.id, () => {
  manualEditMode.value = false
  void loadItemMedia()
})

watch(() => currentItem.value?.status, () => {
  void loadItemMedia()
})

watch(() => sourceForm.media_type, async () => {
  sourceForm.source_dir = localStorage.getItem(`annotation_source_dir_${sourceForm.media_type}`) || (sourceForm.media_type === 'video' ? meta.value?.defaults.video_dir : meta.value?.defaults.image_dir) || ''
  drawStart.value = null
  manualEditMode.value = false
  classFilter.value = '全部'
  await tryRestoreSession()
})

watch(() => userStore.currentWorkspace?.id, async () => {
  session.value = null
  manualEditMode.value = false
  clearSessionMedia()
  if (userStore.currentWorkspace) {
    await loadMeta()
  }
})

onMounted(() => {
  if (userStore.currentWorkspace) {
    void loadMeta()
  }
})

onBeforeUnmount(() => {
  stopPolling()
  clearSessionMedia()
})
</script>

<template>
  <div class="annotation-page" v-loading="loadingMeta || sessionLoading">
    <aside class="control-panel">
      <div class="panel-card">
        <div class="panel-title">数据源</div>

        <el-radio-group v-model="sourceForm.media_type" class="full-width">
          <el-radio-button label="image">图片</el-radio-button>
          <el-radio-button label="video">视频</el-radio-button>
        </el-radio-group>

        <div class="form-block">
          <div class="field-label">{{ sourceForm.media_type === 'image' ? '图片目录' : '视频目录' }}</div>
          <el-input
            v-model="sourceForm.source_dir"
            placeholder="输入服务端可访问目录"
            @blur="tryRestoreSession"
          />
        </div>

        <div class="form-block">
          <div class="field-label">输出目录</div>
          <el-input v-model="sourceForm.output_dir" placeholder="YOLO 标签输出目录" />
        </div>

        <div v-if="sourceForm.media_type === 'video'" class="video-options">
          <div class="field-row">
            <span>启用跟踪</span>
            <el-switch v-model="sourceForm.use_tracking" />
          </div>
          <div class="field-label">检测间隔</div>
          <el-input-number v-model="sourceForm.frame_interval" :min="1" :max="30" style="width: 100%" />
          <div class="field-label">检测分辨率</div>
          <el-input-number v-model="sourceForm.detect_size" :min="160" :max="1280" :step="160" style="width: 100%" />
        </div>

        <div class="field-row force-row">
          <span>强制重跑</span>
          <el-checkbox v-model="sourceForm.force_reprocess" />
        </div>

        <el-button type="primary" class="full-width" :loading="scanning" @click="handleScan">
          扫描并开始标注
        </el-button>

        <div v-if="session" class="status-box">
          <div class="status-header">
            <span>任务状态</span>
            <el-tag size="small" :type="pageStatusType as any">{{ pageStatusText }}</el-tag>
          </div>
          <el-progress
            :percentage="session.progress.percent"
            :status="session.status === 'failed' ? 'exception' : undefined"
            :stroke-width="10"
          />
          <div class="status-subtext">
            {{ session.progress.processed }}/{{ session.progress.total }} 已完成
            <span v-if="session.progress.failed">，失败 {{ session.progress.failed }}</span>
          </div>
        </div>

        <el-button
          v-if="session"
          class="full-width secondary-btn"
          :disabled="session.media_type === 'video'"
          :loading="actionLoading"
          @click="saveAllItems"
        >
          保存全部
        </el-button>
        <div v-if="session?.media_type === 'video'" class="tip-text">
          视频保持与参考实现一致，暂不导出 YOLO 标签。
        </div>
      </div>

      <div class="panel-card">
        <div class="panel-title">引擎状态</div>
        <div class="engine-tags">
          <el-tag :type="meta?.engine.yolo_available ? 'success' : 'danger'">
            YOLO {{ meta?.engine.yolo_available ? '可用' : '不可用' }}
          </el-tag>
          <el-tag :type="meta?.engine.vision_available ? 'success' : 'warning'">
            视觉模型 {{ meta?.engine.vision_available ? '可用' : '回退模式' }}
          </el-tag>
        </div>
        <div class="tip-text" v-if="meta?.engine.vision_model">
          当前模型：{{ meta.engine.vision_model }}
        </div>
      </div>

      <div class="panel-card">
        <div class="panel-title">类别</div>
        <div class="class-list">
          <div v-for="item in classList" :key="item.class_name" class="class-item">
            <span>{{ item.label }}</span>
            <span class="class-key">{{ item.class_name }}</span>
          </div>
        </div>
      </div>
    </aside>

    <section class="workspace-panel">
      <div v-if="!session" class="empty-wrap">
        <el-empty description="在左侧填写目录并启动智能标注">
          <template #description>
            <div class="empty-desc">支持图片批量自动标注、视频逐帧跟踪标注、手工修正与 YOLO 导出。</div>
          </template>
        </el-empty>
      </div>

      <template v-else>
        <div class="workspace-card nav-card">
          <el-button @click="goPrev" :disabled="!session.items.length">上一张</el-button>
          <div class="nav-center">
            <div class="nav-title">{{ currentItem?.file_name || '未选择文件' }}</div>
            <div class="nav-subtitle">{{ currentIndex + 1 }}/{{ session.items.length }}</div>
          </div>
          <el-button @click="goNext" :disabled="!session.items.length">下一张</el-button>
        </div>

        <div v-if="currentItem?.status === 'failed'" class="workspace-card">
          <el-alert
            title="当前文件处理失败"
            :description="currentItem.error_message || '请检查文件格式、模型配置或后端日志'"
            type="error"
            :closable="false"
            show-icon
          />
        </div>

        <template v-if="session.media_type === 'image' && currentItem">
          <div class="metric-row">
            <div class="metric-card">
              <div class="metric-label">标注总数</div>
              <div class="metric-value">{{ imageStats.total }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">自动标注</div>
              <div class="metric-value">{{ imageStats.auto }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">手动标注</div>
              <div class="metric-value">{{ imageStats.manual }}</div>
            </div>
          </div>

          <div class="workspace-card">
            <div class="toolbar-title">结果预览</div>
            <div class="filter-toolbar">
              <div class="toolbar-item">
                <span>显示自动</span>
                <el-switch v-model="showAuto" />
              </div>
              <div class="toolbar-item">
                <span>显示手动</span>
                <el-switch v-model="showManual" />
              </div>
              <div class="toolbar-item">
                <span>显示置信度</span>
                <el-switch v-model="showConf" />
              </div>
              <div class="toolbar-item class-filter">
                <span>类别筛选</span>
                <el-select v-model="classFilter">
                  <el-option label="全部" value="全部" />
                  <el-option
                    v-for="item in classList"
                    :key="item.class_name"
                    :label="item.label"
                    :value="item.class_name"
                  />
                </el-select>
              </div>
              <div class="toolbar-item slider-item">
                <span>自动框阈值 {{ confThreshold.toFixed(2) }}</span>
                <el-slider v-model="confThreshold" :min="0" :max="1" :step="0.05" />
              </div>
            </div>
          </div>

          <div class="image-layout">
            <div class="workspace-card viewer-card">
              <div v-if="currentItem.description" class="desc-card">
                <div class="desc-title">场景描述</div>
                <div class="desc-text">{{ currentItem.description }}</div>
              </div>

              <div class="draw-tip">
                <template v-if="drawStart">
                  起点：({{ Math.round(drawStart.x * (currentItem.width || 0)) }}, {{ Math.round(drawStart.y * (currentItem.height || 0)) }})，请点击右下角完成画框
                </template>
                <template v-else>
                  点击图片左上角和右下角绘制标注框
                </template>
              </div>

              <div class="image-stage" @click="handleImageClick">
                <img v-if="displayedImageUrl" :src="displayedImageUrl" class="image-main" alt="annotation-source" />
                <div
                  v-if="!showImagePreview"
                  v-for="(annotation, index) in visibleAnnotations"
                  :key="`${annotation.class}-${index}`"
                  class="ann-box"
                  :style="getBoxStyle(annotation)"
                >
                  <span class="ann-label">
                    [{{ annotation.manual ? '手' : '自' }}] {{ getClassLabel(annotation.class) }}
                    <template v-if="showConf"> {{ annotation.confidence.toFixed(2) }}</template>
                  </span>
                </div>
                <div v-if="drawStart" class="draw-point" :style="{ left: `${drawStart.x * 100}%`, top: `${drawStart.y * 100}%` }"></div>
              </div>

              <div class="image-foot">
                <div class="manual-tools">
                  <div class="field-label">手动标注类别</div>
                  <el-select v-model="selectedClass" style="width: 220px">
                    <el-option
                      v-for="item in classList"
                      :key="item.class_name"
                      :label="item.label"
                      :value="item.class_name"
                    />
                  </el-select>
                  <el-button v-if="drawStart" @click="cancelDrawing">取消画框</el-button>
                  <el-button
                    v-if="currentItem.status === 'ready' && previewMediaUrl"
                    @click="manualEditMode = !manualEditMode"
                  >
                    {{ manualEditMode ? '查看标注预览' : '进入编辑模式' }}
                  </el-button>
                </div>
                <div class="tip-text">
                  图片尺寸：{{ currentItem.width }} × {{ currentItem.height }}，显示 {{ visibleAnnotations.length }}/{{ currentAnnotations.length }} 个标注
                </div>
              </div>
            </div>

            <div class="workspace-card list-card">
              <div class="toolbar-title">标注列表</div>
              <div v-if="classSummaryText" class="tip-text">类别统计：{{ classSummaryText }}</div>
              <el-empty v-if="!currentAnnotations.length" description="暂无标注" />
              <div v-else class="annotation-list">
                <div v-for="(annotation, index) in currentAnnotations" :key="`${annotation.class}-${index}`" class="annotation-row">
                  <div class="annotation-row-head">
                    <span class="annotation-index">{{ index + 1 }}</span>
                    <span class="annotation-label">{{ annotationRowLabel(annotation) }}</span>
                  </div>
                  <div class="annotation-row-body">
                    <el-select
                      :model-value="annotation.class"
                      style="width: 160px"
                      @change="(value: string) => updateAnnotationClass(index, value)"
                    >
                      <el-option
                        v-for="item in classList"
                        :key="item.class_name"
                        :label="item.label"
                        :value="item.class_name"
                      />
                    </el-select>
                    <span class="coord-text">{{ annotationCoordText(annotation) }}</span>
                    <el-button text type="danger" @click="deleteAnnotation(index)">删除</el-button>
                  </div>
                </div>
              </div>
              <div class="list-actions">
                <el-button type="primary" :loading="actionLoading" @click="saveCurrentItem">保存</el-button>
                <el-button :disabled="!currentAnnotations.length" @click="clearAnnotations">清空</el-button>
              </div>
            </div>
          </div>
        </template>

        <template v-if="session.media_type === 'video' && currentItem">
          <div class="image-layout">
            <div class="workspace-card viewer-card">
              <div class="toolbar-title">视频预览</div>
              <video v-if="previewMediaUrl" class="video-main" :src="previewMediaUrl" controls />
              <el-alert
                v-else-if="previewMissing"
                title="预览视频未生成"
                description="请重新运行带跟踪的标注任务"
                type="warning"
                :closable="false"
                show-icon
              />
              <video v-else-if="currentMediaUrl" class="video-main" :src="currentMediaUrl" controls />

              <div class="list-actions">
                <el-button v-if="previewMediaUrl" type="primary" @click="downloadPreviewVideo">下载预览视频</el-button>
              </div>
            </div>

            <div class="workspace-card list-card">
              <div v-if="currentItem.description" class="desc-card video-desc">
                <div class="desc-title">视频场景描述</div>
                <div class="desc-text">{{ currentItem.description }}</div>
              </div>

              <div class="toolbar-title">视频信息</div>
              <div class="metric-column">
                <div class="metric-card">
                  <div class="metric-label">总跟踪帧数</div>
                  <div class="metric-value">{{ videoStats.trackedFrames }}</div>
                </div>
                <div class="metric-card">
                  <div class="metric-label">同时跟踪目标峰值</div>
                  <div class="metric-value">{{ videoStats.peakTracks }}</div>
                </div>
                <div class="metric-card">
                  <div class="metric-label">视频总帧数</div>
                  <div class="metric-value">{{ videoStats.totalFrames }}</div>
                </div>
                <div class="metric-card">
                  <div class="metric-label">FPS</div>
                  <div class="metric-value">{{ videoStats.fps.toFixed(1) }}</div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </template>
    </section>
  </div>
</template>

<style scoped lang="scss">
.annotation-page {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  min-height: 100%;
}

.control-panel,
.workspace-panel {
  min-width: 0;
}

.panel-card,
.workspace-card,
.metric-card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  box-shadow: 0 12px 28px rgba(15, 23, 42, 0.05);
}

.panel-card {
  padding: 18px;
  margin-bottom: 16px;
}

.panel-title,
.toolbar-title {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 14px;
}

.full-width {
  width: 100%;
}

.secondary-btn {
  margin-top: 12px;
}

.form-block,
.video-options {
  margin-top: 14px;
}

.field-label {
  font-size: 12px;
  color: #64748b;
  margin-bottom: 6px;
}

.field-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.force-row {
  margin: 14px 0;
}

.status-box {
  margin-top: 16px;
  padding: 12px;
  border-radius: 12px;
  background: #f8fafc;
}

.status-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.status-subtext,
.tip-text,
.empty-desc,
.nav-subtitle,
.coord-text,
.class-key {
  color: #64748b;
  font-size: 12px;
}

.engine-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.class-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.class-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border-radius: 10px;
  background: #f8fafc;
  font-size: 13px;
}

.empty-wrap {
  min-height: 480px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%);
  border-radius: 20px;
  border: 1px dashed #cbd5e1;
}

.nav-card {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 16px;
  padding: 16px 20px;
  margin-bottom: 16px;
}

.nav-center {
  text-align: center;
}

.nav-title {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.metric-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}

.metric-column {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.metric-card {
  padding: 18px;
}

.metric-label {
  font-size: 12px;
  color: #64748b;
  margin-bottom: 6px;
}

.metric-value {
  font-size: 28px;
  font-weight: 700;
  color: #0f172a;
}

.filter-toolbar {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.toolbar-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12px;
  color: #475569;
}

.class-filter {
  min-width: 140px;
}

.slider-item {
  grid-column: span 2;
}

.workspace-card {
  padding: 18px;
  margin-bottom: 16px;
}

.image-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(320px, 0.9fr);
  gap: 16px;
}

.viewer-card,
.list-card {
  min-width: 0;
}

.desc-card {
  padding: 14px 16px;
  border-radius: 12px;
  background: #eff6ff;
  margin-bottom: 14px;
}

.video-desc {
  margin-bottom: 18px;
}

.desc-title {
  font-size: 13px;
  font-weight: 700;
  color: #1d4ed8;
  margin-bottom: 6px;
}

.desc-text,
.draw-tip {
  font-size: 13px;
  color: #1e293b;
}

.draw-tip {
  margin-bottom: 12px;
}

.image-stage {
  position: relative;
  display: inline-block;
  width: 100%;
  border-radius: 16px;
  overflow: hidden;
  background: repeating-linear-gradient(
    45deg,
    #f8fafc,
    #f8fafc 12px,
    #f1f5f9 12px,
    #f1f5f9 24px
  );
  cursor: crosshair;
}

.image-main,
.video-main {
  display: block;
  width: 100%;
  max-height: 720px;
  object-fit: contain;
  background: #0f172a;
  border-radius: 16px;
}

.ann-box {
  position: absolute;
  border: 2px solid;
  box-sizing: border-box;
  pointer-events: none;
}

.ann-label {
  position: absolute;
  left: 0;
  top: -26px;
  display: inline-flex;
  align-items: center;
  padding: 4px 8px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.84);
  color: #fff;
  font-size: 12px;
  white-space: nowrap;
}

.draw-point {
  position: absolute;
  width: 10px;
  height: 10px;
  margin-left: -5px;
  margin-top: -5px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.18);
}

.image-foot,
.manual-tools,
.annotation-row,
.annotation-row-body,
.annotation-row-head,
.list-actions {
  display: flex;
}

.image-foot {
  margin-top: 14px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.manual-tools {
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.annotation-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 14px;
}

.annotation-row {
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border-radius: 12px;
  background: #f8fafc;
}

.annotation-row-head,
.annotation-row-body {
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.annotation-row-body {
  flex-wrap: wrap;
}

.annotation-index {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: #0f172a;
  color: #fff;
  font-size: 12px;
}

.annotation-label {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
}

.list-actions {
  gap: 12px;
  margin-top: 18px;
}

@media (max-width: 1200px) {
  .annotation-page {
    grid-template-columns: 1fr;
  }

  .filter-toolbar,
  .image-layout,
  .metric-row,
  .metric-column {
    grid-template-columns: 1fr;
  }

  .slider-item {
    grid-column: span 1;
  }
}
</style>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { dataSourceApi, datasetApi } from '@/api'
import type { DataSource, Dataset } from '@/api/types'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Edit, Delete, FolderOpened, Document, Connection, PictureFilled, VideoCameraFilled, Plus, WarningFilled } from '@element-plus/icons-vue'

const userStore = useUserStore()
let pollTimer: ReturnType<typeof setInterval> | null = null

// 加载状态
const loading = ref(false)
const datasets = ref<Dataset[]>([])
const dataSources = ref<DataSource[]>([])
const selectedDataset = ref<Dataset | null>(null)
const isEditing = ref(false)  // 是否在编辑状态
const savingDataset = ref(false)

// 数据集表单
const datasetForm = ref({
  name: '',
  description: '',
  data_source_ids: [] as number[],
  file_paths: [] as string[],
  status: 'draft',
})

// 数据源表单（用于新建内联数据源）
const dataSourceForm = ref({
  name: '',
  type: 'mysql',
  host: '',
  port: 3306,
  database: '',
  username: '',
  password: '',
  connection_string: '',
})

// CSV 文件上传
const csvFiles = ref<File[]>([])
const imageFiles = ref<File[]>([])
const videoFiles = ref<File[]>([])
const mediaPathInput = ref('')
const uploading = ref(false)
const uploadProgress = ref(0)

// 创建数据源中
const creatingDs = ref(false)

const typeOptions = [
  { value: 'mysql', label: 'MySQL' },
  { value: 'postgresql', label: 'PostgreSQL' },
  { value: 'sqlserver', label: 'SQL Server' },
  { value: 'csv', label: 'CSV 文件' },
  { value: 'duckdb', label: 'DuckDB' },
]

// 加载数据
const loadData = async (silent = false) => {
  if (!userStore.currentWorkspace) {
    if (!silent) ElMessage.warning('请先选择工作空间')
    return
  }

  loading.value = true
  try {
    const [dsRes, ddRes] = await Promise.all([
      dataSourceApi.list(userStore.currentWorkspace.id),
      datasetApi.list(userStore.currentWorkspace.id),
    ])
    dataSources.value = dsRes.data
    datasets.value = ddRes.data
    if (selectedDataset.value) {
      const latest = datasets.value.find((item) => item.id === selectedDataset.value?.id)
      if (latest) selectedDataset.value = latest
    }
    syncPolling()
  } catch (e) {
    if (!silent) ElMessage.error('加载失败')
    console.error('加载失败:', e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadData(true)
  syncPolling()
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

watch(() => userStore.currentWorkspace, () => loadData(true))

const syncPolling = () => {
  const hasProcessing = datasets.value.some((item) => ['pending', 'processing'].includes(item.processing_status))
  if (hasProcessing && !pollTimer) {
    pollTimer = setInterval(() => loadData(true), 5000)
    return
  }
  if (!hasProcessing && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// 选择数据集
const handleDatasetSelect = (ds: Dataset) => {
  selectedDataset.value = ds
  isEditing.value = true
  datasetForm.value = {
    name: ds.name,
    description: ds.description || '',
    data_source_ids: ds.data_source_ids || (ds.data_source_id ? [ds.data_source_id] : []),
    file_paths: [],
    status: ds.status || 'draft',
  }
  imageFiles.value = []
  videoFiles.value = []
  csvFiles.value = []
  mediaPathInput.value = ''
}

// 新增数据集
const handleAddDataset = () => {
  selectedDataset.value = null
  isEditing.value = true
  datasetForm.value = {
    name: '',
    description: '',
    data_source_ids: [],
    file_paths: [],
    status: 'draft',
  }
  csvFiles.value = []
  imageFiles.value = []
  videoFiles.value = []
  mediaPathInput.value = ''
}

// 编辑数据集
const handleEditDataset = (ds: Dataset) => {
  handleDatasetSelect(ds)
}

// 保存数据集
const handleSaveDataset = async () => {
  if (!datasetForm.value.name) {
    ElMessage.warning('请输入数据集名称')
    return
  }

  if (selectedDataset.value && (imageFiles.value.length > 0 || videoFiles.value.length > 0 || datasetForm.value.file_paths.length > 0)) {
    ElMessage.warning('当前版本仅支持在新建数据集时接入图片/视频资源')
    return
  }

  savingDataset.value = true

  try {
    const payload: any = {
      name: datasetForm.value.name,
      description: datasetForm.value.description,
      data_source_ids: datasetForm.value.data_source_ids,
      data_source_id: datasetForm.value.data_source_ids[0] || null,
      status: datasetForm.value.status,
      workspace_id: userStore.currentWorkspace!.id,
      file_paths: datasetForm.value.file_paths,
    }

    if (selectedDataset.value) {
      if (csvFiles.value.length > 0) {
        await handleUploadCsvFiles()
      }
      await datasetApi.update(selectedDataset.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      const hasFileInputs = csvFiles.value.length > 0 || imageFiles.value.length > 0 || videoFiles.value.length > 0 || datasetForm.value.file_paths.length > 0
      const requestBody = hasFileInputs ? buildDatasetFormData(payload) : payload
      const res = await datasetApi.create(requestBody)
      const created = res.data
      const isProcessing = ['pending', 'processing'].includes(created.processing_status)
      ElMessage.success(isProcessing ? '数据集创建成功，图片/视频正在处理中' : '创建成功')
    }
    isEditing.value = false
    selectedDataset.value = null
    csvFiles.value = []
    imageFiles.value = []
    videoFiles.value = []
    mediaPathInput.value = ''
    await loadData(true)
  } catch (e: any) {
    console.error('保存失败:', e)
    ElMessage.error(e.response?.data?.detail || '操作失败')
  } finally {
    savingDataset.value = false
  }
}

// 删除数据集
const handleDeleteDataset = async (id: number, name: string) => {
  try {
    await ElMessageBox.confirm(`确定要删除数据集 "${name}" 吗？`, '提示', { type: 'warning' })
    await datasetApi.delete(id)
    ElMessage.success('删除成功')
    if (selectedDataset.value?.id === id) {
      selectedDataset.value = null
      isEditing.value = false
    }
    loadData()
  } catch (e: any) {
    if (e !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

// CSV 文件选择
const handleCsvFileChange = (uploadFile: any) => {
  const file = uploadFile.raw
  if (!file) return

  if (!file.name.endsWith('.csv')) {
    ElMessage.error('只支持 CSV 文件')
    return
  }

  csvFiles.value.push(file)
}

const validateMediaFile = (file: File, type: 'image' | 'video') => {
  const maxMb = type === 'image' ? 50 : 1024
  const validType = type === 'image'
    ? file.type.startsWith('image/') || /\.(png|jpe?g|bmp|webp)$/i.test(file.name)
    : file.type.startsWith('video/') || /\.(mp4|mov|avi|mkv|m4v|webm)$/i.test(file.name)
  if (!validType) {
    ElMessage.error(type === 'image' ? '只支持图片文件' : '只支持视频文件')
    return false
  }
  if (file.size / 1024 / 1024 > maxMb) {
    ElMessage.error(`${type === 'image' ? '图片' : '视频'}大小不能超过 ${maxMb}MB`)
    return false
  }
  return true
}

const handleImageFileChange = (uploadFile: any) => {
  const file = uploadFile.raw as File | undefined
  if (!file || !validateMediaFile(file, 'image')) return
  imageFiles.value.push(file)
}

const handleVideoFileChange = (uploadFile: any) => {
  const file = uploadFile.raw as File | undefined
  if (!file || !validateMediaFile(file, 'video')) return
  videoFiles.value.push(file)
}

// 删除 CSV 文件
const removeCsvFile = (index: number) => {
  csvFiles.value.splice(index, 1)
}

const removeImageFile = (index: number) => {
  imageFiles.value.splice(index, 1)
}

const removeVideoFile = (index: number) => {
  videoFiles.value.splice(index, 1)
}

const handleAddMediaPath = () => {
  const value = mediaPathInput.value.trim()
  if (!value) {
    ElMessage.warning('请输入文件路径')
    return
  }
  if (!datasetForm.value.file_paths.includes(value)) {
    datasetForm.value.file_paths.push(value)
  }
  mediaPathInput.value = ''
}

const removeMediaPath = (index: number) => {
  datasetForm.value.file_paths.splice(index, 1)
}

// 创建数据源并添加到数据集
const handleCreateDataSourceAndAdd = async () => {
  if (!dataSourceForm.value.name) {
    ElMessage.warning('请输入数据源名称')
    return
  }

  if (!dataSourceForm.value.type) {
    ElMessage.warning('请选择数据库类型')
    return
  }

  creatingDs.value = true

  try {
    const res = await dataSourceApi.create({
      ...dataSourceForm.value,
      workspace_id: userStore.currentWorkspace!.id,
    } as any)
    ElMessage.success('数据源创建成功')

    // 刷新数据源列表
    await loadData()

    // 添加到数据集
    if (!datasetForm.value.data_source_ids.includes(res.data.id)) {
      datasetForm.value.data_source_ids.push(res.data.id)
    }

    // 清空表单
    dataSourceForm.value = {
      name: '',
      type: 'mysql',
      host: '',
      port: 3306,
      database: '',
      username: '',
      password: '',
      connection_string: '',
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '创建失败')
  } finally {
    creatingDs.value = false
  }
}

// 数据库类型切换时自动设置默认端口
const handleTypeChange = () => {
  if (dataSourceForm.value.type === 'mysql') {
    dataSourceForm.value.port = 3306
  } else if (dataSourceForm.value.type === 'postgresql') {
    dataSourceForm.value.port = 5432
  } else if (dataSourceForm.value.type === 'sqlserver') {
    dataSourceForm.value.port = 1433
  }
}

// 从数据集中移除数据源
const removeDataSourceFromDataset = (index: number) => {
  datasetForm.value.data_source_ids.splice(index, 1)
}

// 上传 CSV 文件并创建数据源
const handleUploadCsvFiles = async () => {
  if (csvFiles.value.length === 0) {
    ElMessage.warning('请选择至少一个 CSV 文件')
    return
  }

  uploading.value = true

  try {
    const workspaceId = userStore.currentWorkspace?.id || 1

    // 为每个 CSV 文件创建数据源并上传
    for (let i = 0; i < csvFiles.value.length; i++) {
      const file = csvFiles.value[i]

      // 先创建数据源
      const dsRes = await dataSourceApi.create({
        name: file.name.replace('.csv', ''),
        type: 'csv',
        workspace_id: workspaceId,
      } as any)

      // 上传 CSV 文件
      await dataSourceApi.uploadCSV(file, workspaceId, dsRes.data.id)

      // 添加到数据集
      if (!datasetForm.value.data_source_ids.includes(dsRes.data.id)) {
        datasetForm.value.data_source_ids.push(dsRes.data.id)
      }

      uploadProgress.value = Math.round(((i + 1) / csvFiles.value.length) * 100)
    }

    ElMessage.success(`成功上传 ${csvFiles.value.length} 个文件`)
    csvFiles.value = []
    uploadProgress.value = 0
    loadData(true)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '上传失败')
  } finally {
    uploading.value = false
  }
}

// 获取数据源类型标签
const getDataSourceTypeLabel = (type: string) => {
  const option = typeOptions.find(t => t.value === type)
  return option?.label || type
}

const buildDatasetFormData = (payload: Record<string, any>) => {
  const formData = new FormData()
  formData.append('payload', JSON.stringify(payload))
  csvFiles.value.forEach((file) => formData.append('csv_files', file))
  imageFiles.value.forEach((file) => formData.append('image_files', file))
  videoFiles.value.forEach((file) => formData.append('video_files', file))
  return formData
}

const formatFileSize = (size: number) => {
  if (!size) return '0 B'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`
}

const getProcessingTagType = (processingStatus: Dataset['processing_status']) => {
  if (processingStatus === 'ready') return 'success'
  if (processingStatus === 'failed') return 'danger'
  if (processingStatus === 'processing') return 'warning'
  return 'info'
}

const getProcessingStatusLabel = (dataset: Dataset) => {
  if (dataset.processing_status === 'ready') return dataset.media_count > 0 ? '可检索' : '已就绪'
  if (dataset.processing_status === 'failed') return '处理失败'
  if (dataset.processing_status === 'processing') return '图片/视频处理中'
  return '等待处理'
}

const selectedDatasetAlert = computed(() => {
  if (!selectedDataset.value) return null
  if (selectedDataset.value.processing_status === 'processing' || selectedDataset.value.processing_status === 'pending') {
    return {
      type: 'warning',
      title: '当前数据集中的图片/视频正在处理中',
      description: '离线处理完成前，多模态检索结果可能不完整。',
    }
  }
  if (selectedDataset.value.processing_status === 'failed') {
    return {
      type: 'error',
      title: '当前数据集中的部分图片/视频处理失败',
      description: selectedDataset.value.error_message || '请检查路径、文件格式或后端日志后重试。',
    }
  }
  return null
})

</script>

<template>
  <div class="data-config-page">
    <!-- 左侧：数据集列表 -->
    <div class="left-panel">
      <div class="panel-header">
        <span class="panel-title">数据集</span>
      </div>
      <div class="source-list">
        <div
          v-for="ds in datasets"
          :key="ds.id"
          class="source-item"
          :class="{ active: selectedDataset?.id === ds.id }"
          @click="handleDatasetSelect(ds)"
        >
          <div class="source-info">
            <el-icon><FolderOpened /></el-icon>
            <div class="source-meta">
              <span class="source-name">{{ ds.name }}</span>
              <el-tag size="small" :type="getProcessingTagType(ds.processing_status)">
                {{ getProcessingStatusLabel(ds) }}
              </el-tag>
            </div>
          </div>
          <div class="source-actions">
            <el-button size="small" text @click.stop="handleEditDataset(ds)">
              <el-icon><Edit /></el-icon>
            </el-button>
            <el-button size="small" text type="danger" @click.stop="handleDeleteDataset(ds.id, ds.name)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
        <div v-if="datasets.length === 0 && !loading" class="empty-tip">
          暂无数据集，请添加
        </div>
      </div>
    </div>

    <!-- 右侧：数据集配置 -->
    <div class="right-panel">
      <div v-if="!isEditing" class="empty-config">
        <el-empty description="请选择或创建数据集">
          <el-button type="primary" @click="handleAddDataset">新建数据集</el-button>
        </el-empty>
      </div>

      <div v-else class="config-content">
        <el-alert
          v-if="selectedDatasetAlert"
          :title="selectedDatasetAlert.title"
          :description="selectedDatasetAlert.description"
          :type="selectedDatasetAlert.type as any"
          :closable="false"
          show-icon
          class="dataset-alert"
        >
          <template #icon><el-icon><WarningFilled /></el-icon></template>
        </el-alert>

        <div class="edit-form">
          <h4>{{ selectedDataset ? '编辑数据集' : '新建数据集' }}</h4>

          <el-form label-width="100px">
            <el-form-item label="数据集名称">
              <el-input v-model="datasetForm.name" placeholder="请输入数据集名称" />
            </el-form-item>

            <el-form-item label="描述">
              <el-input v-model="datasetForm.description" type="textarea" :rows="2" placeholder="数据集描述" />
            </el-form-item>

            <el-form-item label="状态">
              <el-radio-group v-model="datasetForm.status">
                <el-radio label="draft">草稿</el-radio>
                <el-radio label="active">启用</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-form>
        </div>

        <!-- 数据来源配置 -->
        <div class="edit-form data-sources-section">
          <h4>数据来源</h4>

          <!-- 已添加的数据源列表 -->
          <div v-if="datasetForm.data_source_ids.length > 0" class="added-sources">
            <div class="section-title">已添加的数据源：</div>
            <div
              v-for="(dsId, index) in datasetForm.data_source_ids"
              :key="dsId"
              class="source-item"
            >
              <div class="source-info">
                <el-icon><Connection /></el-icon>
                <span>{{ dataSources.find(s => s.id === dsId)?.name || '未知' }}</span>
                <el-tag size="small">{{ getDataSourceTypeLabel(dataSources.find(s => s.id === dsId)?.type || '') }}</el-tag>
              </div>
              <el-button size="small" text type="danger" @click="removeDataSourceFromDataset(index)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>

          <!-- 添加数据源区域 -->
          <div class="add-source-section">
            <!-- 左边：数据库连接配置 -->
            <div class="add-source-item">
              <div class="add-source-header">
                <el-icon><Connection /></el-icon>
                <span>数据库连接</span>
              </div>

              <el-form label-width="80px" size="small">
                <el-form-item label="数据源名称">
                  <el-input v-model="dataSourceForm.name" placeholder="用于标识此连接" />
                </el-form-item>

                <el-form-item label="数据库类型">
                  <el-select v-model="dataSourceForm.type" placeholder="选择类型" @change="handleTypeChange">
                    <el-option
                      v-for="t in typeOptions.filter(t => t.value !== 'csv')"
                      :key="t.value"
                      :label="t.label"
                      :value="t.value"
                    />
                  </el-select>
                </el-form-item>

                <el-form-item label="主机">
                  <el-input v-model="dataSourceForm.host" placeholder="localhost" />
                </el-form-item>

                <el-form-item label="端口">
                  <el-input-number v-model="dataSourceForm.port" :min="1" :max="65535" style="width: 100%" />
                </el-form-item>

                <el-form-item label="数据库">
                  <el-input v-model="dataSourceForm.database" placeholder="数据库名" />
                </el-form-item>

                <el-form-item label="用户名">
                  <el-input v-model="dataSourceForm.username" placeholder="用户名" />
                </el-form-item>

                <el-form-item label="密码">
                  <el-input v-model="dataSourceForm.password" type="password" placeholder="密码" show-password />
                </el-form-item>

                <el-form-item>
                  <el-button type="primary" size="small" @click="handleCreateDataSourceAndAdd" :loading="creatingDs">
                    添加到数据集
                  </el-button>
                </el-form-item>
              </el-form>
            </div>

            <!-- 右边：CSV 文件上传 -->
            <div class="add-source-item">
              <div class="add-source-header">
                <el-icon><Document /></el-icon>
                <span>CSV 文件</span>
              </div>
              <el-upload
                :auto-upload="false"
                :file-list="[]"
                :on-change="handleCsvFileChange"
                accept=".csv"
                multiple
                drag
              >
                <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
                <div class="el-upload__text">拖拽 CSV 文件到此处</div>
                <template #tip>
                  <div class="el-upload__tip">支持多个 CSV 文件</div>
                </template>
              </el-upload>

              <div v-if="csvFiles.length > 0" class="file-list">
                <el-tag
                  v-for="(file, index) in csvFiles"
                  :key="index"
                  closable
                  @close="removeCsvFile(index)"
                  class="file-tag"
                >
                  {{ file.name }}
                </el-tag>
              </div>

              <el-button
                v-if="selectedDataset && csvFiles.length > 0"
                type="primary"
                size="small"
                :loading="uploading"
                @click="handleUploadCsvFiles"
                style="margin-top: 10px"
              >
                {{ uploading ? `上传中 ${uploadProgress}%` : '上传 CSV 文件到当前数据集' }}
              </el-button>
              <div v-else class="inline-tip">新建数据集时，CSV 会随“创建数据集”一起提交。</div>
            </div>
          </div>

          <div class="media-input-section">
            <div class="section-title">图/视频数据</div>
            <div class="media-grid">
              <div class="add-source-item">
                <div class="add-source-header">
                  <el-icon><PictureFilled /></el-icon>
                  <span>图片上传</span>
                </div>
                <el-upload
                  :auto-upload="false"
                  :file-list="[]"
                  :on-change="handleImageFileChange"
                  accept=".jpg,.jpeg,.png,.bmp,.webp,image/*"
                  multiple
                  drag
                >
                  <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
                  <div class="el-upload__text">拖拽图片到此处，或点击选择</div>
                </el-upload>
              </div>

              <div class="add-source-item">
                <div class="add-source-header">
                  <el-icon><VideoCameraFilled /></el-icon>
                  <span>视频上传</span>
                </div>
                <el-upload
                  :auto-upload="false"
                  :file-list="[]"
                  :on-change="handleVideoFileChange"
                  accept=".mp4,.mov,.avi,.mkv,.m4v,.webm,video/*"
                  multiple
                  drag
                >
                  <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
                  <div class="el-upload__text">拖拽视频到此处，或点击选择</div>
                </el-upload>
              </div>
            </div>

            <div class="path-box">
              <div class="add-source-header">
                <el-icon><Document /></el-icon>
                <span>服务端可访问路径</span>
              </div>
              <div class="path-input-row">
                <el-input
                  v-model="mediaPathInput"
                  placeholder="输入本地路径、挂载路径或目录路径"
                  @keydown.enter.prevent="handleAddMediaPath"
                />
                <el-button type="primary" plain @click="handleAddMediaPath">
                  <el-icon><Plus /></el-icon>
                  添加路径
                </el-button>
              </div>
              <div v-if="datasetForm.file_paths.length > 0" class="path-list">
                <el-tag
                  v-for="(filePath, index) in datasetForm.file_paths"
                  :key="filePath"
                  closable
                  @close="removeMediaPath(index)"
                  class="file-tag"
                >
                  {{ filePath }}
                </el-tag>
              </div>
            </div>

            <div v-if="imageFiles.length > 0 || videoFiles.length > 0" class="selected-media-files">
              <div class="section-title">已选媒体文件</div>
              <div
                v-for="(file, index) in imageFiles"
                :key="`image-${index}-${file.name}`"
                class="selected-file-row"
              >
                <span class="selected-file-name">图片 · {{ file.name }}</span>
                <span class="selected-file-size">{{ formatFileSize(file.size) }}</span>
                <el-button text type="danger" @click="removeImageFile(index)">移除</el-button>
              </div>
              <div
                v-for="(file, index) in videoFiles"
                :key="`video-${index}-${file.name}`"
                class="selected-file-row"
              >
                <span class="selected-file-name">视频 · {{ file.name }}</span>
                <span class="selected-file-size">{{ formatFileSize(file.size) }}</span>
                <el-button text type="danger" @click="removeVideoFile(index)">移除</el-button>
              </div>
            </div>

            <div class="inline-tip">
              支持上传图片/视频，或填写服务端可访问路径。视频会按逻辑切片离线处理，不会默认导出大量物理 clip。
            </div>
          </div>
        </div>

        <!-- 保存按钮 -->
        <div class="action-bar">
          <el-button type="primary" :loading="savingDataset" @click="handleSaveDataset">
            {{ selectedDataset ? '保存修改' : '创建数据集' }}
          </el-button>
        </div>
      </div>
    </div>

  </div>
</template>

<style scoped lang="scss">
.data-config-page {
  display: flex;
  height: 100%;
  background: #f7f8fa;
}

.left-panel {
  width: 280px;
  background: #fff;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
  border-bottom: 1px solid #e4e7ed;
}

.panel-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.source-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.source-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: all 0.2s;

  &:hover {
    background: #f5f7fa;
  }

  &.active {
    background: #ecf5ff;
    color: #409eff;
  }
}

.source-info {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow: hidden;
}

.source-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.source-name {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.source-actions {
  display: flex;
  gap: 4px;
}

.right-panel {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.empty-config {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.config-content {
  max-width: 900px;
}

.dataset-alert {
  margin-bottom: 16px;
}

.edit-form {
  background: #fff;
  padding: 20px;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
  margin-bottom: 20px;

  h4 {
    margin: 0 0 16px;
    font-size: 16px;
    font-weight: 600;
    color: #303133;
  }
}

.data-sources-section {
  .section-title {
    font-size: 14px;
    font-weight: 500;
    color: #606266;
    margin-bottom: 12px;
  }
}

.added-sources {
  margin-bottom: 20px;
  padding-bottom: 20px;
  border-bottom: 1px dashed #e4e7ed;

  .source-item {
    background: #f5f7fa;
    margin-bottom: 8px;
  }
}

.add-source-section {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}

.media-input-section {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px dashed #e4e7ed;
}

.media-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 20px;
}

.add-source-item {
  padding: 16px;
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
}

.path-box {
  padding: 16px;
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
  margin-bottom: 16px;
}

.path-input-row {
  display: flex;
  gap: 12px;
}

.path-list {
  margin-top: 12px;
}

.add-source-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 14px;
  font-weight: 500;
  color: #303133;
}

.source-select-list {
  max-height: 200px;
  overflow-y: auto;
}

.source-select-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 4px;
  cursor: pointer;
  margin-bottom: 4px;

  &:hover {
    background: #f5f7fa;
  }

  &.added {
    background: #ecf5ff;
    color: #409eff;
  }
}

.empty-tip {
  text-align: center;
  padding: 20px;
  color: #909399;
  font-size: 14px;
}

.file-list {
  margin-top: 12px;
}

.file-tag {
  margin-right: 8px;
  margin-bottom: 8px;
}

.selected-media-files {
  padding: 16px;
  border-radius: 8px;
  background: #f5f7fa;
  margin-bottom: 12px;
}

.selected-file-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 12px;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid #ebeef5;

  &:last-child {
    border-bottom: none;
  }
}

.selected-file-name {
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.selected-file-size {
  color: #909399;
  font-size: 12px;
}

.el-icon--upload {
  font-size: 40px;
  color: #409eff;
  margin-bottom: 8px;
}

.el-upload__text {
  color: #606266;
  font-size: 14px;

  em {
    color: #409eff;
  }
}

.el-upload__tip {
  color: #909399;
  font-size: 12px;
  margin-top: 7px;
}

.inline-tip {
  margin-top: 10px;
  color: #909399;
  font-size: 12px;
}

.action-bar {
  text-align: center;
}

@media (max-width: 960px) {
  .add-source-section,
  .media-grid {
    grid-template-columns: 1fr;
  }

  .path-input-row {
    flex-direction: column;
  }
}
</style>

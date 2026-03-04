<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { dataSourceApi, datasetApi } from '@/api'
import type { DataSource, Dataset } from '@/api/types'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Edit, Delete, FolderOpened, Document, Connection } from '@element-plus/icons-vue'

const userStore = useUserStore()

// 加载状态
const loading = ref(false)
const datasets = ref<Dataset[]>([])
const dataSources = ref<DataSource[]>([])
const selectedDataset = ref<Dataset | null>(null)
const isEditing = ref(false)  // 是否在编辑状态

// 数据集表单
const datasetForm = ref({
  name: '',
  description: '',
  data_source_ids: [] as number[],
  csv_files: [] as File[],
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
const loadData = async () => {
  if (!userStore.currentWorkspace) {
    ElMessage.warning('请先选择工作空间')
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
    ElMessage.success(`加载了 ${ddRes.data.length} 个数据集`)
  } catch (e) {
    ElMessage.error('加载失败')
    console.error('加载失败:', e)
  } finally {
    loading.value = false
  }
}

onMounted(loadData)

watch(() => userStore.currentWorkspace, loadData)

// 选择数据集
const handleDatasetSelect = (ds: Dataset) => {
  selectedDataset.value = ds
  isEditing.value = true
  datasetForm.value = {
    name: ds.name,
    description: ds.description || '',
    data_source_ids: ds.data_source_ids || (ds.data_source_id ? [ds.data_source_id] : []),
    csv_files: [],
    status: ds.status || 'draft',
  }
}

// 新增数据集
const handleAddDataset = () => {
  selectedDataset.value = null
  isEditing.value = true
  datasetForm.value = {
    name: '',
    description: '',
    data_source_ids: [],
    csv_files: [],
    status: 'draft',
  }
  csvFiles.value = []
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

  // 如果有 CSV 文件需要先上传
  if (csvFiles.value.length > 0) {
    await handleUploadCsvFiles()
    if (uploading.value) return // 上传失败
  }

  ElMessage.info(`正在保存，data_source_ids: ${JSON.stringify(datasetForm.value.data_source_ids)}`)

  try {
    const payload: any = {
      name: datasetForm.value.name,
      description: datasetForm.value.description,
      data_source_ids: datasetForm.value.data_source_ids,
      data_source_id: datasetForm.value.data_source_ids[0] || null,
      status: datasetForm.value.status,
    }

    if (selectedDataset.value) {
      await datasetApi.update(selectedDataset.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await datasetApi.create({
        ...payload,
        workspace_id: userStore.currentWorkspace!.id,
      })
      ElMessage.success('创建成功')
    }
    isEditing.value = false
    loadData()
  } catch (e: any) {
    console.error('保存失败:', e)
    ElMessage.error(e.response?.data?.detail || '操作失败')
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

// 删除 CSV 文件
const removeCsvFile = (index: number) => {
  csvFiles.value.splice(index, 1)
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
    loadData()
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
            <span class="source-name">{{ ds.name }}</span>
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
                v-if="csvFiles.length > 0"
                type="primary"
                size="small"
                :loading="uploading"
                @click="handleUploadCsvFiles"
                style="margin-top: 10px"
              >
                {{ uploading ? `上传中 ${uploadProgress}%` : '上传 CSV 文件' }}
              </el-button>
            </div>
          </div>
        </div>

        <!-- 保存按钮 -->
        <div class="action-bar">
          <el-button type="primary" @click="handleSaveDataset">
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

.add-source-item {
  padding: 16px;
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
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

.action-bar {
  text-align: center;
}
</style>

<template>
  <section class="trial-panel" :data-module="meta.key">
    <header class="trial-head">
      <div>
        <h3>探针试跑 · {{ meta.name }}</h3>
        <p class="trial-desc">
          从模板到探针定位本轮试跑定义；没有有效选择器时先产出空数据集，再回模板补字段。
        </p>
      </div>
      <button class="btn" type="button" data-testid="trial-refresh" @click="reload">刷新</button>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="trial-error">{{ errorMessage }}</p>

    <template v-if="meta.key === 'template_rule'">
      <form class="trial-form" @submit.prevent="loadTemplate">
        <label>
          <span>模板 ID</span>
          <input v-model="templateId" data-testid="trial-template-id" placeholder="例如 102" />
        </label>
        <button class="btn primary" type="submit" data-testid="trial-load-config">读取试跑配置</button>
      </form>

      <div v-if="config" class="trial-grid">
        <article class="trial-card">
          <h4>试跑配置来源</h4>
          <dl>
            <dt>模板版本</dt><dd>{{ config.template_version }}（{{ config.template_status }}）</dd>
            <dt>采样页面</dt><dd>{{ config.page }}</dd>
            <dt>试跑定义哈希</dt><dd><code>{{ config.definition_hash }}</code></dd>
            <dt>探针来源</dt>
            <dd data-testid="trial-probe-source">
              {{ config.probe_source.name }} · {{ probeSourceLabel(config.probe_source) }}
            </dd>
            <dt>建议探针</dt>
            <dd data-testid="trial-suggested-count">
              {{ config.suggested_probe_count }} 条
              <span v-if="!config.suggested_probe_count" class="muted">（空数组：无有效选择器）</span>
            </dd>
            <dt>正式快照能力</dt>
            <dd>{{ config.can_produce_official ? '本轮可产出正式字段快照' : '本轮不能产出正式快照' }}</dd>
          </dl>
          <ul v-if="config.issues.length" class="issue-list" data-testid="trial-issues">
            <li v-for="issue in config.issues" :key="issue.code + issue.message" :class="`sev-${issue.severity}`">
              [{{ severityLabel(issue.severity) }}] {{ issue.message }}
            </li>
          </ul>
        </article>

        <article class="trial-card">
          <h4>同页面可回查版本</h4>
          <table class="data-table">
            <thead><tr><th>ID</th><th>版本</th><th>状态</th><th>有选择器</th></tr></thead>
            <tbody>
              <tr v-for="v in config.versions" :key="v.id" :data-testid="`trial-version-${v.id}`">
                <td>{{ v.id }}</td><td>{{ v.version }}</td><td>{{ v.status }}</td>
                <td>{{ v.has_selectors ? '是' : '否' }}</td>
              </tr>
            </tbody>
          </table>
          <p class="muted">
            兼容历史探针只提供采样出处，旧字段不参与新版试跑；需要补字段时以本轮诊断为准。
          </p>
        </article>
      </div>

      <div v-if="config" class="trial-actions">
        <button class="btn primary" data-testid="trial-start" type="button" @click="runTemplateTrial">
          {{ config.can_produce_official ? '运行试跑' : '运行试跑（预期空字段数据集）' }}
        </button>
      </div>

      <div v-if="lastRun" class="trial-grid" data-testid="trial-result">
        <article class="trial-card">
          <h4>本轮数据集 · {{ lastRun.dataset.dataset_type }}</h4>
          <p data-testid="trial-explanation">{{ lastRun.dataset.explanation }}</p>
          <table class="data-table">
            <thead><tr><th>字段</th><th>选择器</th><th>结果</th><th>采样值</th><th>出处</th></tr></thead>
            <tbody>
              <tr v-for="row in lastRun.dataset.rows" :key="row.字段">
                <td>{{ row.字段 }}</td>
                <td><code>{{ row.选择器 || '—' }}</code></td>
                <td>{{ row.结果 }}</td>
                <td>{{ row.采样值 || '—' }}</td>
                <td><code>{{ row.采样出处 }}</code></td>
              </tr>
            </tbody>
          </table>
          <p class="muted">本轮可验证：{{ lastRun.dataset.verifiable.join('；') }}</p>
          <p class="muted">本轮不可验证：{{ lastRun.dataset.not_verifiable.join('；') || '无' }}</p>
        </article>

        <article class="trial-card">
          <h4>待补字段清单（已回写模板）</h4>
          <table class="data-table" data-testid="trial-pending">
            <thead><tr><th>字段</th><th>建议选择器</th><th>采样出处</th><th>验收</th></tr></thead>
            <tbody>
              <tr v-for="item in pendingFields" :key="item.字段">
                <td>{{ item.字段 }}</td>
                <td><code>{{ item.建议选择器 }}</code></td>
                <td><code>{{ item.采样出处 }}</code></td>
                <td>{{ item.验收状态 }} · 仅用于定位 DOM</td>
              </tr>
              <tr v-if="!pendingFields.length">
                <td colspan="4" class="empty-state">没有待补字段</td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>

      <form v-if="config && pendingFields.length" class="trial-form" @submit.prevent="fillField">
        <label>
          <span>补字段</span>
          <select v-model="fill.field" data-testid="trial-fill-field">
            <option v-for="item in pendingFields" :key="item.字段" :value="item.字段">{{ item.字段 }}</option>
          </select>
        </label>
        <label>
          <span>选择器</span>
          <input v-model="fill.selector" data-testid="trial-fill-selector" placeholder="例如 h1.title" />
        </label>
        <button class="btn" type="submit" data-testid="trial-fill-submit">补回选择器</button>
      </form>

      <div v-if="comparison && comparison.fields" class="trial-card" data-testid="trial-comparison">
        <h4>轮次对比 · 第 {{ comparison.previous_round }} 轮 → 第 {{ comparison.current_round }} 轮</h4>
        <p>
          覆盖 {{ comparison.previous_covered }} → {{ comparison.current_covered }}
          （{{ comparison.covered_delta >= 0 ? '+' : '' }}{{ comparison.covered_delta }}），
          定义{{ comparison.definition_changed ? '已变更' : '未变更' }}。
        </p>
        <table class="data-table">
          <thead><tr><th>字段</th><th>上轮</th><th>本轮</th><th>变化</th></tr></thead>
          <tbody>
            <tr v-for="item in comparison.fields" :key="item.字段">
              <td>{{ item.字段 }}</td>
              <td>{{ item.上轮结果 }} {{ item.上轮采样值 || '' }}</td>
              <td>{{ item.本轮结果 }} {{ item.本轮采样值 || '' }}</td>
              <td>{{ item.变化 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <template v-else-if="meta.key === 'probe_selector'">
      <form class="trial-form" @submit.prevent="loadProbe">
        <label>
          <span>探针 ID</span>
          <input v-model="probeId" data-testid="trial-probe-id" placeholder="例如 101" />
        </label>
        <button class="btn primary" type="submit" data-testid="trial-probe-load">读取试跑入口</button>
      </form>

      <div v-if="probeConfig" class="trial-grid">
        <article class="trial-card">
          <h4>本轮落到的模板</h4>
          <dl>
            <dt>模板</dt><dd>{{ probeConfig.template_name }} · {{ probeConfig.template_version }}（ID {{ probeConfig.template_id }}）</dd>
            <dt>探针来源</dt><dd data-testid="trial-probe-kind">{{ probeSourceLabel(probeConfig.probe_source) }}</dd>
            <dt>说明</dt><dd>{{ probeConfig.probe_source.message }}</dd>
            <dt>试跑定义哈希</dt><dd><code>{{ probeConfig.definition_hash }}</code></dd>
          </dl>
          <p v-if="!probeConfig.can_produce_official" class="warn-text" data-testid="trial-probe-warning">
            当前落到的模板没有有效选择器，本轮会产出空数据集；旧探针的历史字段不参与。
          </p>
          <button class="btn primary" type="button" data-testid="trial-probe-run" @click="runProbeTrial">
            从探针入口试跑
          </button>
        </article>

        <article class="trial-card">
          <h4>页面级诊断</h4>
          <p class="muted">仅列出有实际采样出处的 DOM 定位证据，全部为未验收。</p>
          <button class="btn" type="button" data-testid="trial-diagnostics" @click="loadDiagnostics">
            读取页面级诊断
          </button>
          <table v-if="diagnostics" class="data-table" data-testid="trial-diagnostic-table">
            <thead><tr><th>字段</th><th>候选选择器</th><th>采样出处</th><th>验收</th></tr></thead>
            <tbody>
              <tr v-for="item in diagnostics.diagnostics" :key="item.字段">
                <td>{{ item.字段 }}</td>
                <td><code>{{ item.DOM定位 }}</code></td>
                <td><code>{{ item.采样出处 }}</code></td>
                <td>{{ item.验收状态 }}</td>
              </tr>
              <tr v-if="!diagnostics.diagnostics.length">
                <td colspan="4" class="empty-state">本轮没有可用的 DOM 定位证据</td>
              </tr>
            </tbody>
          </table>
          <p v-if="diagnostics" class="muted">丢弃无出处条目：{{ diagnostics.discarded }} 条</p>
        </article>
      </div>

      <div v-if="probeRun" class="trial-card" data-testid="trial-probe-result">
        <h4>探针试跑结果 · {{ probeRun.trial.试跑编号 }} · {{ probeRun.dataset.dataset_type }}</h4>
        <p>{{ probeRun.dataset.explanation }}</p>
        <ul>
          <li v-for="item in probeRun.dataset.verifiable" :key="item">可验证：{{ item }}</li>
          <li v-for="item in probeRun.dataset.not_verifiable" :key="item">不可验证：{{ item }}</li>
        </ul>
        <button
          v-if="probeRun.trial.试跑状态 === '采集中断'"
          class="btn"
          type="button"
          data-testid="trial-retry"
          @click="retryCurrent"
        >
          按相同试跑定义重试
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import {
  getComparison,
  getPendingFields,
  getProbeDiagnostics,
  getProbeTrialReadiness,
  getTemplateTrialConfig,
  retryTrial,
  startProbeTrial,
  startTemplateTrial,
  updateTemplateSelectors,
} from '@/api/backend-service'
import type { ModuleMeta } from '@/data/types'

const props = defineProps<{ meta: ModuleMeta }>()
const errorMessage = ref('')

const templateId = ref('102')
const probeId = ref('101')
const config = ref<any>(null)
const probeConfig = ref<any>(null)
const lastRun = ref<any>(null)
const probeRun = ref<any>(null)
const diagnostics = ref<any>(null)
const comparison = ref<any>(null)
const pendingFields = ref<any[]>([])
const fill = ref<{ field: string; selector: string }>({ field: '', selector: '' })

function probeSourceLabel(source: any): string {
  if (!source) return '未知'
  if (source.source_type === 'legacy_probe') return `兼容历史探针（来自 ${source.probe_template_version || '未知版本'}）`
  if (source.source_type === 'template_only') return '无探针，仅按模板采样'
  return '当前模板版本探针'
}

function severityLabel(severity: string): string {
  return severity === 'error' ? '阻断' : severity === 'warning' ? '警告' : '提示'
}

function resetOutput() {
  errorMessage.value = ''
  config.value = null
  probeConfig.value = null
  lastRun.value = null
  probeRun.value = null
  diagnostics.value = null
  comparison.value = null
  pendingFields.value = []
}

function syncFill() {
  const first = pendingFields.value[0]
  fill.value = first
    ? { field: first.字段, selector: first.建议选择器 || '' }
    : { field: '', selector: '' }
}

async function reloadPending(id: number) {
  if (!id) return
  const payload = await getPendingFields(id)
  if (payload.ok) {
    pendingFields.value = payload.pending_fields || []
    syncFill()
  }
}

async function loadTemplate() {
  errorMessage.value = ''
  const id = Number(templateId.value)
  if (!id) {
    errorMessage.value = '请先填写模板 ID'
    return
  }
  try {
    config.value = await getTemplateTrialConfig(id)
    if (!config.value.ok) throw new Error(config.value.reason || '试跑配置不可用')
    await reloadPending(id)
    lastRun.value = null
    comparison.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试跑配置读取失败'
  }
}

async function runTemplateTrial() {
  errorMessage.value = ''
  try {
    const id = Number(templateId.value)
    const result = await startTemplateTrial(id, { entry: '模板规则' })
    if (!result.ok) throw new Error(result.reason || '试跑发起失败')
    lastRun.value = result
    await reloadPending(id)
    config.value = await getTemplateTrialConfig(id)
    comparison.value = await getComparison(Number(result.trial.id))
    if (!comparison.value.ok) comparison.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试跑失败'
  }
}

async function fillField() {
  errorMessage.value = ''
  try {
    const id = Number(templateId.value)
    const result = await updateTemplateSelectors(id, {
      field: fill.value.field,
      selector: fill.value.selector,
    })
    if (!result.ok) throw new Error(result.reason || '选择器保存失败')
    await reloadPending(id)
    config.value = await getTemplateTrialConfig(id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补字段失败'
  }
}

async function loadProbe() {
  errorMessage.value = ''
  const id = Number(probeId.value)
  if (!id) {
    errorMessage.value = '请先填写探针 ID'
    return
  }
  try {
    probeConfig.value = await getProbeTrialReadiness(id)
    if (!probeConfig.value.ok) throw new Error(probeConfig.value.reason || '探针试跑配置不可用')
    probeRun.value = null
    diagnostics.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '探针试跑配置读取失败'
  }
}

async function runProbeTrial() {
  errorMessage.value = ''
  try {
    const result = await startProbeTrial(Number(probeId.value), { entry: '探针选择' })
    if (!result.ok) throw new Error(result.reason || '探针试跑发起失败')
    probeRun.value = result
    probeConfig.value = await getProbeTrialReadiness(Number(probeId.value))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '探针试跑失败'
  }
}

async function retryCurrent() {
  if (!probeRun.value) return
  errorMessage.value = ''
  try {
    const result = await retryTrial(Number(probeRun.value.trial.id), probeRun.value.trial.试跑定义哈希)
    if (!result.ok) throw new Error(result.reason || '重试失败')
    probeRun.value = result
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试失败'
  }
}

async function loadDiagnostics() {
  errorMessage.value = ''
  try {
    const result = await getProbeDiagnostics(Number(probeId.value))
    if (!result.ok) throw new Error(result.reason || '诊断读取失败')
    diagnostics.value = result
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '诊断读取失败'
  }
}

async function reload() {
  if (props.meta.key === 'probe_selector') await loadProbe()
  else await loadTemplate()
}

watch(
  () => props.meta.key,
  () => {
    resetOutput()
    void reload()
  },
  { immediate: true },
)
</script>

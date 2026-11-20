import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ----------------------------------------------------------------------
// 模板 ↔ 探针试跑
// ----------------------------------------------------------------------
async function getJson<T>(path: string, fallback: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(fallback)
  return (await response.json()) as T
}

async function postJson<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  if (!response.ok) throw new Error(fallback)
  return (await response.json()) as T
}

export const getTemplateTrialConfig = (id: number, probeId?: number) =>
  getJson<any>(
    `/api/template_rule/${id}/trial-config${probeId ? `?probe_id=${probeId}` : ''}`,
    '试跑配置读取失败',
  )

export const getProbeTrialReadiness = (probeId: number) =>
  getJson<any>(`/api/probe_selector/${probeId}/trial-readiness`, '探针试跑配置读取失败')

export const startTemplateTrial = (id: number, payload: Record<string, unknown> = {}) =>
  postJson<any>(`/api/template_rule/${id}/trial-runs`, payload, '试跑发起失败')

export const startProbeTrial = (probeId: number, payload: Record<string, unknown> = {}) =>
  postJson<any>(`/api/probe_selector/${probeId}/trial-runs`, payload, '探针试跑发起失败')

export const retryTrial = (trialId: number, definitionHash?: string) =>
  postJson<any>(`/api/probe_trial/${trialId}/retry`, { definition_hash: definitionHash }, '试跑重试失败')

export const getComparison = (trialId: number) =>
  getJson<any>(`/api/probe_trial/${trialId}/comparison`, '轮次对比读取失败')

export const getPendingFields = (templateId: number) =>
  getJson<any>(`/api/template_rule/${templateId}/pending-fields`, '待补字段读取失败')

export const updateTemplateSelectors = (
  templateId: number,
  payload: Record<string, unknown>,
) => postJson<any>(`/api/template_rule/${templateId}/selectors`, payload, '选择器保存失败')

export const getProbeDiagnostics = (probeId: number) =>
  getJson<any>(`/api/probe_selector/${probeId}/page-diagnostics`, '页面级诊断读取失败')

export const getTemplateTrials = (templateId: number) =>
  getJson<any>(`/api/template_rule/${templateId}/trials`, '试跑轮次读取失败')

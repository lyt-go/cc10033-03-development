/** 放行回执域接口封装：批次版本、放行、领取/签收、签批、核销与投递启动。 */
import { request } from './client'

export type PermitIdentity = {
  id?: number
  permit_id: string
  receipt_no?: string
  scope?: string
  batch_key?: string
  batch_no?: string
  export_id?: number | string | null
  revision?: number
  revision_label?: string
  version?: string
  receiver?: string
  status?: string
  effective?: boolean
  valid?: boolean
  redeemable?: boolean
  can_send?: boolean
  sendable?: boolean
  needs_reconfirm?: boolean
  needs_review?: boolean
  needs_owner_confirmation?: boolean
  reason_code?: string
  current_revision?: number
  current_revision_label?: string
  current_permit_id?: string | null
  verifiable?: boolean
  frozen_batch?: Record<string, unknown>
  approval?: { decision?: string; approved?: boolean; approver?: string; comment?: string; iso?: string } | null
  [key: string]: unknown
}

export type Verdict = {
  ok: boolean
  allowed?: boolean
  can_send?: boolean
  sendable?: boolean
  needs_reconfirm?: boolean
  needs_owner_confirmation?: boolean
  code?: string
  reason_code?: string
  message?: string
  permit?: PermitIdentity | null
  receipt?: PermitIdentity | null
  identity?: Record<string, unknown> | null
  current_permit?: PermitIdentity | null
  claim?: Record<string, unknown> | null
  approval?: Record<string, unknown> | null
  todo?: DeliveryTodo | null
  delivery?: Record<string, unknown> | null
  [key: string]: unknown
}

export type BatchVersion = {
  id: number
  batch_key: string
  batch_no: string
  revision: number
  revision_label: string
  version?: string
  status: string
  status_zh?: string
  current: boolean
  export_id?: number | null
  task_no?: string
  file_path?: string
  digest?: string
  correction_reason?: string
  iso?: string
  snapshot?: Record<string, unknown>
}

export type DeliveryTodo = {
  id: number
  pipe_id?: number | null
  title?: string
  batch_key?: string
  batch_no?: string
  revision?: number
  revision_label?: string
  receiver?: string
  permit_id?: string | null
  status: string
  approval_status?: string
  签批结果?: string
  claim_status?: string
  can_send?: boolean
  sendable?: boolean
  needs_reconfirm?: boolean
  marker?: string
  verdict?: Partial<Verdict>
  receipt?: PermitIdentity | null
  [key: string]: unknown
}

export type ReleaseOverview = {
  batches: BatchVersion[]
  receipts: PermitIdentity[]
  claims: Record<string, unknown>[]
  approvals: Record<string, unknown>[]
  todos: DeliveryTodo[]
  deliveries: Record<string, unknown>[]
}

async function postJson<T = Verdict>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const releaseApi = {
  overview: () => getJson<ReleaseOverview>('/api/release/overview'),
  listBatches: () => getJson<{ items: BatchVersion[] }>('/api/release/batches'),
  listReceipts: () => getJson<{ items: PermitIdentity[] }>('/api/release/receipts'),
  listTodos: (pipeId?: number | string) =>
    getJson<{ items: DeliveryTodo[] }>(`/api/delivery/todos${qs({ pipe_id: pipeId })}`),
  getReceipt: (ref: string) => getJson<PermitIdentity & { ok?: boolean }>(`/api/release/receipts/${encodeURIComponent(ref)}`),

  createBatch: (body: Record<string, unknown>) => postJson<BatchVersion & Verdict>('/api/release/batches', body),
  release: (body: Record<string, unknown>) => postJson<Verdict>('/api/release/receipts', body),
  correct: (body: Record<string, unknown>) => postJson<Verdict>('/api/release/correct', body),
  claim: (body: Record<string, unknown>) => postJson<Verdict>('/api/release/claim', body),
  approve: (body: Record<string, unknown>) => postJson<Verdict>('/api/release/approve', body),
  redeem: (body: Record<string, unknown>) => postJson<Verdict>('/api/release/redeem', body),
  start: (body: Record<string, unknown>) => postJson<Verdict>('/api/delivery/start', body),
  evaluate: (params: Record<string, string | number | undefined | null>) =>
    getJson<Verdict>(`/api/release/evaluate${qs(params)}`),
  precheck: (body: Record<string, unknown>) => postJson<Verdict>('/api/release/precheck', body),
  createTodo: (body: Record<string, unknown>) => postJson<{ ok: boolean; todo: DeliveryTodo; message?: string }>('/api/delivery/todos', body),
  todoAction: (todoId: number, action: 'redeem' | 'start', body: Record<string, unknown> = {}) =>
    postJson<Verdict>(`/api/delivery/todos/${todoId}/${action}`, body),
}

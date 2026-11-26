<template>
  <section
    class="gate-panel release-panel"
    data-testid="release-panel"
    aria-label="交接放行与放行回执"
  >
    <header class="gate-head">
      <div>
        <h3>交接放行 · 放行回执</h3>
        <p class="gate-tip">
          选择确定的导出批次与接收方发起放行；更正批次会生成 R2 新版本，旧签收/回执原样保留可查证。
        </p>
      </div>
      <button class="btn ghost" type="button" data-testid="release-refresh" @click="reload">刷新</button>
    </header>

    <!-- 发起放行 -->
    <form class="gate-form" data-testid="release-form" @submit.prevent="submitRelease">
      <label class="gate-field">
        <span>选择批次（导出任务）</span>
        <select
          v-model="selectedExportId"
          data-testid="batch-select"
          id="batch-select"
          aria-label="选择批次"
          @focus="loadExports"
          @pointerdown="loadExports"
        >
          <option value="" disabled>请选择已生成的导出批次</option>
          <option v-for="row in exportRows" :key="row.id" :value="String(row.id)">
            #{{ row.id }} {{ row['任务编号'] }}（{{ row.status }}）
          </option>
        </select>
      </label>
      <label class="gate-field">
        <span>接收方</span>
        <input
          v-model="receiver"
          list="receiver-options"
          data-testid="receiver-input"
          id="receiver-input"
          name="receiver"
          autocomplete="off"
          placeholder="填写确定的接收方，如 Kafka-数仓 / 接收方A"
        />
        <datalist id="receiver-options">
          <option v-for="name in receiverSuggestions" :key="name" :value="name" />
        </datalist>
      </label>
      <label class="gate-field narrow">
        <span>交接负责人</span>
        <input v-model="owner" data-testid="owner-input" name="owner" placeholder="交接负责人" />
      </label>
      <button class="btn primary" type="submit" data-testid="initiate-release" id="initiate-release">
        发起放行
      </button>
      <button class="btn" type="button" data-testid="correct-batch" id="correct-batch" @click="submitCorrect">
        更正批次
      </button>
      <label class="gate-field grow">
        <span>更正原因（可选）</span>
        <input v-model="reason" data-testid="correct-reason" placeholder="例如：修正漏导行" />
      </label>
    </form>

    <!-- 全局判定条 -->
    <div
      v-if="lastVerdict"
      :class="['banner', lastVerdict.ok === false || lastVerdict.can_send === false ? 'banner-bad' : 'banner-ok']"
      data-testid="verdict-banner"
      role="status"
      :data-ok="String(lastVerdict.ok)"
      :data-can-send="String(Boolean(lastVerdict.can_send))"
    >
      <span>{{ lastVerdict.message }}</span>
      <span v-if="lastVerdict.permit_id" class="mono">许可身份：{{ lastVerdict.permit_id }}</span>
    </div>

    <!-- 版本变更/需要复核提示 -->
    <div
      v-if="reviewHint"
      class="banner banner-warn"
      data-testid="review-required"
      role="alert"
    >
      <strong>有版本需要复核：</strong>
      <span>
        原回执指向 {{ reviewHint.oldLabel }}，批次当前为
        <strong data-testid="review-version">{{ reviewHint.newLabel }}</strong>；
        请负责人对 {{ reviewHint.newLabel }} 重新发起放行。原批次回执仍可打开查证。
      </span>
      <div class="banner-actions">
        <button class="btn primary" type="button" data-testid="review-current-btn" @click="reviewCurrent">
          定位并复核 {{ reviewHint.newLabel }}
        </button>
        <button class="btn" type="button" data-testid="open-old-receipt" @click="openReceipt(reviewHint.oldPermitId)">
          打开原批次回执（{{ reviewHint.oldPermitId }}）
        </button>
      </div>
    </div>

    <div class="gate-cols">
      <!-- 批次版本 -->
      <div class="gate-block">
        <h4>批次版本（R1 放行 → 更正产生 R2）</h4>
        <table class="data-table" data-testid="batch-versions-table">
          <thead>
            <tr><th>批次</th><th>版本</th><th>文件/摘要</th><th>状态</th><th>可发送标记</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="b in overview.batches"
              :key="b.id"
              :id="`batch-row-${b.id}`"
              :class="{ 'row-flash': flashBatchId === b.id }"
              :data-testid="`batch-version-row-${b.id}`"
              :data-current="String(b.current)"
            >
              <td>{{ b.batch_no }}</td>
              <td data-testid="revision-label"><strong>{{ b.revision_label }}</strong></td>
              <td class="ellipsis" :title="`${b.file_path ?? ''} ${b.digest ?? ''}`">
                {{ b.file_path || '—' }}
                <div class="muted small">{{ b.digest }}</div>
              </td>
              <td>{{ b.status_zh || b.status }}</td>
              <td>
                <span v-if="effectivePermitFor(b)" class="badge badge-ok" data-testid="sendable-version">
                  回执 {{ effectivePermitFor(b)?.permit_id }} · 实际可发送
                </span>
                <span v-else-if="b.current" class="badge badge-warn">当前版本 · 待放行/复核</span>
                <span v-else class="badge">已更正 · 留档</span>
              </td>
              <td>
                <button
                  v-if="b.current"
                  class="link"
                  type="button"
                  :data-testid="`correct-${b.id}`"
                  @click="correctSpecific(b)"
                >
                  更正为 R{{ b.revision + 1 }}
                </button>
                <span v-else class="muted">—</span>
              </td>
            </tr>
            <tr v-if="!overview.batches.length">
              <td colspan="6" class="empty-state">尚未生成放行批次；先在上方选择导出任务并发起放行会自动建立 R1。</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 放行回执 -->
      <div class="gate-block">
        <h4>放行回执 / 许可身份</h4>
        <table class="data-table" data-testid="receipts-table">
          <thead>
            <tr><th>许可身份</th><th>范围</th><th>事实状态</th><th>当前效力</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="r in overview.receipts"
              :key="r.permit_id"
              :data-testid="`receipt-row-${r.id}`"
              :data-permit="r.permit_id"
              :data-effective="String(Boolean(r.effective))"
            >
              <td class="mono" data-testid="permit-id-cell">{{ r.permit_id }}</td>
              <td>{{ r.revision_label }} → {{ r.receiver }}</td>
              <td>{{ r.status }}</td>
              <td>
                <span :class="['badge', r.effective ? 'badge-ok' : 'badge-stale']" data-testid="receipt-state">
                  {{ r.effective ? (r.reason_code === 'ALREADY_REDEEMED' ? '已核销' : '有效') : '版本已变/失效' }}
                </span>
              </td>
              <td class="row-actions wrap">
                <button class="link" type="button" :data-testid="`claim-${r.id}`" @click="claim(r)">领取</button>
                <button class="link" type="button" :data-testid="`sign-${r.id}`" @click="claim(r)">签收</button>
                <button class="link" type="button" :data-testid="`approve-${r.id}`" @click="approve(r, '通过')">签批通过</button>
                <button class="link danger" type="button" :data-testid="`reject-${r.id}`" @click="approve(r, '驳回')">驳回</button>
                <button class="link" type="button" :data-testid="`view-receipt-${r.id}`" @click="openReceipt(r.permit_id)">查看回执</button>
              </td>
            </tr>
            <tr v-if="!overview.receipts.length">
              <td colspan="5" class="empty-state">暂无放行回执。下载预检或导出完成不能代替放行。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 旧签收 -->
    <div class="gate-block" data-testid="old-signoffs">
      <h4>旧签收记录（保留原批次定位，可原样查证）</h4>
      <ul v-if="overview.claims.length" class="signoff-list">
        <li
          v-for="c in overview.claims"
          :key="String(c.id)"
          :data-testid="`old-signoff-row-${c.id}`"
          class="signoff-item"
        >
          <button class="link" type="button" :data-testid="`open-signoff-${c.id}`" @click="openClaim(c)">
            {{ c.claim_no }} · {{ c.batch_no }} · {{ c.revision_label }} · {{ c.receiver }}
            <span class="muted">（{{ c.iso }}）</span>
          </button>
        </li>
      </ul>
      <p v-else class="muted small" data-testid="no-signoffs">暂无签收记录。</p>
    </div>

    <ReceiptDialog
      :open="dialogOpen"
      :permit="dialogPermit"
      :loading="dialogLoading"
      @close="closeDialog"
      @open-current="(pid) => openReceipt(pid)"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listEntries } from '@/api/backend-service'
import {
  releaseApi,
  type BatchVersion,
  type PermitIdentity,
  type ReleaseOverview,
  type Verdict,
} from '@/api/release'
import type { EntryRow } from '@/data/types'
import ReceiptDialog from './ReceiptDialog.vue'

const props = defineProps<{ tick?: number; presetExportId?: number | null }>()

const route = useRoute()
const router = useRouter()

const exportRows = ref<EntryRow[]>([])
const overview = ref<ReleaseOverview>({
  batches: [], receipts: [], claims: [], approvals: [], todos: [], deliveries: [],
})
const selectedExportId = ref('')
const receiver = ref('')
const owner = ref('交接负责人')
const reason = ref('')
const lastVerdict = ref<Verdict | null>(null)
const flashBatchId = ref<number | null>(null)

const dialogOpen = ref(false)
const dialogLoading = ref(false)
const dialogPermit = ref<PermitIdentity | null>(null)

let timer: ReturnType<typeof setInterval> | null = null

const receiverSuggestions = computed(() => {
  const fromReceipts = overview.value.receipts.map((r) => String(r.receiver ?? ''))
  const targets = exportRows.value.map((row) => String(row['目标系统'] ?? '')).filter(Boolean)
  return Array.from(new Set([...fromReceipts, ...targets, '接收方A', '接收方B'])).filter(Boolean)
})

const reviewHint = computed(() => {
  const stale = overview.value.receipts.find((r) => r.needs_reconfirm)
  const verdictStale =
    lastVerdict.value && lastVerdict.value.code === 'VERSION_SUPERSEDED'
      ? lastVerdict.value
      : null
  if (verdictStale && verdictStale.permit) {
    return {
      oldLabel: verdictStale.permit.revision_label,
      newLabel: verdictStale.permit.current_revision_label ?? '当前版本',
      oldPermitId: verdictStale.permit.permit_id,
    }
  }
  if (stale) {
    return {
      oldLabel: stale.revision_label,
      newLabel: stale.current_revision_label ?? '当前版本',
      oldPermitId: stale.permit_id,
    }
  }
  return null
})

function effectivePermitFor(batch: BatchVersion): PermitIdentity | undefined {
  return overview.value.receipts.find(
    (r) =>
      r.batch_key === batch.batch_key &&
      r.revision === batch.revision &&
      r.effective &&
      r.reason_code !== 'ALREADY_REDEEMED',
  )
}

async function loadExports() {
  try {
    const pageData = await listEntries('export_dataset', { size: '200' })
    exportRows.value = pageData.items
    if (!selectedExportId.value && exportRows.value.length) {
      const completed = exportRows.value.find((r) => r.status === '已完成')
      selectedExportId.value = String((completed ?? exportRows.value[0]).id)
    }
  } catch {
    /* 列表读取失败不阻断面板，总览仍可工作 */
  }
}

async function reload() {
  try {
    overview.value = await releaseApi.overview()
  } catch (error) {
    lastVerdict.value = { ok: false, message: error instanceof Error ? error.message : '放行数据读取失败' }
  }
}

function broadcastChanged() {
  window.dispatchEvent(new CustomEvent('gate-changed'))
}

async function submitRelease() {
  if (!receiver.value.trim()) {
    lastVerdict.value = { ok: false, can_send: false, message: '请先填写接收方；放行范围必须指向确定的批次与接收方' }
    return
  }
  if (!selectedExportId.value) {
    lastVerdict.value = { ok: false, can_send: false, message: '请先选择导出批次' }
    return
  }
  const verdict = await releaseApi.release({
    export_id: Number(selectedExportId.value),
    receiver: receiver.value.trim(),
    owner: owner.value.trim() || '交接负责人',
  })
  lastVerdict.value = verdict
  await reload()
  broadcastChanged()
}

async function submitCorrect() {
  if (!selectedExportId.value) {
    lastVerdict.value = { ok: false, message: '请先选择要更正的导出批次' }
    return
  }
  const verdict = await releaseApi.correct({
    export_id: Number(selectedExportId.value),
    reason: reason.value.trim() || '交接人更正批次',
  })
  lastVerdict.value = verdict
  reason.value = ''
  await reload()
  broadcastChanged()
}

async function correctSpecific(batch: BatchVersion) {
  const verdict = await releaseApi.correct({
    batch_key: batch.batch_key,
    reason: reason.value.trim() || '交接人更正批次',
  })
  lastVerdict.value = verdict
  await reload()
  broadcastChanged()
}

async function claim(r: PermitIdentity) {
  const verdict = await releaseApi.claim({ permit_id: r.permit_id, receiver: r.receiver })
  lastVerdict.value = verdict
  await reload()
  broadcastChanged()
}

async function approve(r: PermitIdentity, decision: string) {
  const verdict = await releaseApi.approve({ permit_id: r.permit_id, decision })
  lastVerdict.value = verdict
  await reload()
  broadcastChanged()
}

function reviewCurrent() {
  if (!reviewHint.value) return
  const current = overview.value.batches.find(
    (b) => b.current && b.revision_label === reviewHint.value?.newLabel,
  ) ?? overview.value.batches.find((b) => b.current)
  if (current) {
    selectedExportId.value = current.export_id ? String(current.export_id) : selectedExportId.value
    flashBatch(current.id)
  }
  document.querySelector<HTMLInputElement>('[data-testid="receiver-input"]')?.focus()
}

function flashBatch(id: number) {
  flashBatchId.value = id
  document.getElementById(`batch-row-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  window.setTimeout(() => { flashBatchId.value = null }, 2400)
}

async function openReceipt(permitId: string | undefined) {
  if (!permitId) return
  dialogOpen.value = true
  dialogLoading.value = true
  dialogPermit.value = overview.value.receipts.find((r) => r.permit_id === permitId) ?? null
  try {
    const full = await releaseApi.getReceipt(permitId)
    if (full && !('ok' in full && full.ok === false)) dialogPermit.value = full as PermitIdentity
  } catch {
    /* 保留列表中的简版 */
  } finally {
    dialogLoading.value = false
  }
  if (route.query.receipt !== permitId) {
    void router.replace({ query: { ...route.query, receipt: permitId } })
  }
}

async function openClaim(c: Record<string, unknown>) {
  const permitId = String(c.permit_id ?? '')
  const batch = overview.value.batches.find(
    (b) => b.batch_key === c.batch_key && b.revision === c.revision,
  )
  if (batch) flashBatch(batch.id)
  await openReceipt(permitId)
}

function closeDialog() {
  dialogOpen.value = false
  dialogPermit.value = null
  if (route.query.receipt) {
    const query = { ...route.query }
    delete query.receipt
    void router.replace({ query })
  }
}

watch(() => props.tick, () => {
  void loadExports()
  void reload()
})
watch(() => props.presetExportId, (value) => {
  if (value) selectedExportId.value = String(value)
})
watch(() => route.query.receipt, (value) => {
  if (typeof value === 'string' && value && (!dialogOpen.value || dialogPermit.value?.permit_id !== value)) {
    void openReceipt(value)
  }
})

function onGateChanged() { void reload() }

onMounted(async () => {
  await Promise.all([loadExports(), reload()])
  if (props.presetExportId) selectedExportId.value = String(props.presetExportId)
  const q = route.query.receipt
  if (typeof q === 'string' && q) await openReceipt(q)
  window.addEventListener('gate-changed', onGateChanged)
  timer = setInterval(() => {
    void reload()
    void loadExports()
  }, 4000)
})

onBeforeUnmount(() => {
  window.removeEventListener('gate-changed', onGateChanged)
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.gate-panel { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 14px 0; }
.gate-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.gate-head h3 { margin: 0; font-size: 16px; }
.gate-tip { color: var(--muted); font-size: 12.5px; margin: 4px 0 0; max-width: 760px; }
.gate-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; }
.gate-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 200px; }
.gate-field.narrow { min-width: 140px; }
.gate-field.grow { flex: 1; min-width: 180px; }
.gate-field input, .gate-field select { border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; font-size: 13px; color: #0f172a; background: #fff; }
.gate-cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
@media (max-width: 1100px) { .gate-cols { grid-template-columns: 1fr; } }
.gate-block { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-top: 10px; }
.gate-block h4 { margin: 0 0 8px; font-size: 14px; }
.banner { border-radius: 8px; padding: 9px 12px; font-size: 13px; margin: 8px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.banner-ok { background: #dcfce7; border: 1px solid #86efac; color: #166534; }
.banner-bad { background: #fee2e2; border: 1px solid #fca5a5; color: #991b1b; }
.banner-warn { background: #fff7ed; border: 1px solid #fdba74; color: #9a3412; flex-direction: column; align-items: flex-start; gap: 6px; }
.banner-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2ff; color: #3730a3; font-size: 12px; white-space: nowrap; }
.badge-ok { background: #dcfce7; color: #166534; }
.badge-warn { background: #fef9c3; color: #854d0e; }
.badge-stale { background: #fee2e2; color: #991b1b; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.ellipsis { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-actions.wrap { flex-wrap: wrap; gap: 8px; }
.link.danger { color: #b42318; }
.signoff-list { margin: 0; padding-left: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.signoff-item { font-size: 13px; }
.row-flash { outline: 2px solid var(--brand); background: #eff6ff; transition: background 0.3s; }
</style>

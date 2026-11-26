<template>
  <section
    class="gate-panel delivery-panel"
    data-testid="delivery-release-panel"
    aria-label="投递待发送行与回执核销"
  >
    <header class="gate-head">
      <div>
        <h3>放行核销 · 投递管道待发送行</h3>
        <p class="gate-tip">
          只有指向当前批次版本与接收方的有效放行回执才能核销并启动投递；旧版本/新接收方会被拒绝并提示请负责人复核。
        </p>
      </div>
      <button class="btn ghost" type="button" data-testid="delivery-refresh" @click="reload">刷新</button>
    </header>

    <!-- 操作员按版本提交投递 -->
    <form class="gate-form" data-testid="submit-version-form" @submit.prevent="submitScoped">
      <label class="gate-field">
        <span>选择批次版本</span>
        <select v-model="scopedBatchId" data-testid="version-select" aria-label="选择批次版本">
          <option value="" disabled>请选择批次版本（R1/R2）</option>
          <option v-for="b in overview.batches" :key="b.id" :value="String(b.id)">
            {{ b.batch_no }} · {{ b.revision_label }}{{ b.current ? '（当前）' : '（已更正）' }}
          </option>
        </select>
      </label>
      <label class="gate-field">
        <span>接收方</span>
        <input v-model="scopedReceiver" data-testid="submit-receiver-input" name="receiver" placeholder="该提交的接收方" />
      </label>
      <label class="gate-field">
        <span>许可身份（可留空自动匹配）</span>
        <input v-model="scopedPermit" data-testid="submit-permit-input" placeholder="HX-000000" />
      </label>
      <button class="btn primary" type="submit" data-testid="submit-version" id="submit-version">
        提交投递
      </button>
    </form>

    <!-- 每个版本一个显式「提交 Rn」入口，便于两版分别验收 -->
    <div class="version-submit-row" data-testid="version-submit-list">
      <span class="muted small">两版分别提交：</span>
      <span v-for="b in overview.batches" :key="`vs-${b.id}`" class="version-submit-item">
        <button
          class="btn"
          type="button"
          :data-testid="`submit-${b.revision_label}`"
          @click="submitRevision(b)"
        >
          提交 {{ b.revision_label }}
        </button>
      </span>
    </div>

    <div
      v-if="lastVerdict"
      :class="['banner', lastVerdict.can_send === false || lastVerdict.ok === false ? 'banner-bad' : 'banner-ok']"
      data-testid="delivery-verdict-banner"
      role="status"
      :data-ok="String(lastVerdict.ok)"
      :data-can-send="String(Boolean(lastVerdict.can_send))"
      :data-code="lastVerdict.code ?? ''"
    >
      <span data-testid="verdict-message">{{ lastVerdict.message }}</span>
      <span v-if="lastVerdict.permit_id" class="mono">
        返回许可身份：{{ lastVerdict.permit_id }}（can_send={{ String(Boolean(lastVerdict.can_send)) }}，
        needs_reconfirm={{ String(Boolean(lastVerdict.needs_reconfirm)) }}）
      </span>
    </div>

    <!-- 登记待发送行 -->
    <form class="gate-form compact" data-testid="create-todo-form" @submit.prevent="submitCreateTodo">
      <label class="gate-field">
        <span>管道ID（可选）</span>
        <input v-model="newTodo.pipe_id" data-testid="todo-pipe-id" placeholder="如 3" />
      </label>
      <label class="gate-field">
        <span>批次号</span>
        <input v-model="newTodo.batch_no" data-testid="todo-batch-no" placeholder="批次号/任务编号" />
      </label>
      <label class="gate-field">
        <span>接收方</span>
        <input v-model="newTodo.receiver" data-testid="todo-receiver" placeholder="接收方" />
      </label>
      <button class="btn" type="submit" data-testid="create-todo">登记待发送行</button>
    </form>

    <!-- 待发送行 -->
    <div class="gate-block">
      <h4>待发送行（用回执标出实际可发送的版本）</h4>
      <table class="data-table" data-testid="delivery-todos-table">
        <thead>
          <tr>
            <th>ID</th><th>批次/版本</th><th>接收方</th><th>回执许可身份</th>
            <th>领取</th><th>签批结果</th><th>状态</th><th>可发送标记</th><th>核销/投递</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="todo in overview.todos"
            :key="todo.id"
            :data-testid="`todo-row-${todo.id}`"
            :data-status="String(todo.status)"
            :data-can-send="String(Boolean(todo.can_send))"
            :class="{ 'row-sendable': todo.can_send }"
          >
            <td>{{ todo.id }}</td>
            <td>
              {{ todo.batch_no || '—' }}
              <strong data-testid="todo-revision">{{ todo.revision_label || '' }}</strong>
            </td>
            <td data-testid="todo-receiver-cell">{{ todo.receiver }}</td>
            <td class="mono">
              <input
                class="permit-input"
                :data-testid="`permit-input-${todo.id}`"
                :value="permitDraft(todo)"
                placeholder="HX-000000"
                @input="setPermitDraft(todo.id, ($event.target as HTMLInputElement).value)"
              />
              <RouterLink
                v-if="todo.permit_id"
                :to="`/export_dataset?receipt=${encodeURIComponent(todo.permit_id)}`"
                class="link"
                data-testid="open-original-receipt-link"
              >
                打开原批次回执
              </RouterLink>
            </td>
            <td>{{ todo.claim_status || '—' }}</td>
            <td data-testid="todo-approval">{{ todo.approval_status || todo['签批结果'] || '待签批' }}</td>
            <td data-testid="todo-status">{{ todo.status }}</td>
            <td>
              <span :class="['badge', todo.can_send ? 'badge-ok' : 'badge-stale']" data-testid="todo-marker">
                {{ todo.marker }}
              </span>
            </td>
            <td class="row-actions wrap">
              <button class="link" type="button" :data-testid="`redeem-${todo.id}`" @click="onRedeem(todo)">核销</button>
              <button class="link" type="button" :data-testid="`start-${todo.id}`" @click="onStart(todo)">启动投递</button>
              <span
                v-if="rowMessages[todo.id]"
                :class="['row-msg', rowMessageOk[todo.id] ? 'row-msg-ok' : '']"
                :data-testid="`todo-verdict-${todo.id}`"
              >
                {{ rowMessages[todo.id] }}
              </span>
            </td>
          </tr>
          <tr v-if="!overview.todos.length">
            <td colspan="9" class="empty-state" data-testid="empty-todos">
              暂无待发送行；负责人发起放行后会自动生成，也可在上方手动登记。
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import {
  releaseApi,
  type BatchVersion,
  type DeliveryTodo,
  type ReleaseOverview,
  type Verdict,
} from '@/api/release'

const props = defineProps<{ tick?: number }>()

const overview = ref<ReleaseOverview>({
  batches: [], receipts: [], claims: [], approvals: [], todos: [], deliveries: [],
})
const lastVerdict = ref<Verdict | null>(null)
const permitDrafts = reactive<Record<number, string>>({})
const rowMessages = reactive<Record<number, string>>({})
const rowMessageOk = reactive<Record<number, boolean>>({})

const scopedBatchId = ref('')
const scopedReceiver = ref('')
const scopedPermit = ref('')
const newTodo = reactive({ pipe_id: '', batch_no: '', receiver: '' })

let timer: ReturnType<typeof setInterval> | null = null

function permitDraft(todo: DeliveryTodo): string {
  if (permitDrafts[todo.id] !== undefined) return permitDrafts[todo.id]
  return todo.permit_id ?? ''
}

function setPermitDraft(id: number, value: string) {
  permitDrafts[id] = value
}

async function reload() {
  try {
    overview.value = await releaseApi.overview()
    if (!scopedBatchId.value && overview.value.batches.length) {
      const current = overview.value.batches.find((b) => b.current) ?? overview.value.batches[0]
      scopedBatchId.value = String(current.id)
    }
    if (!scopedReceiver.value) {
      const first = overview.value.todos[0]?.receiver
      if (first) scopedReceiver.value = first
    }
  } catch (error) {
    lastVerdict.value = { ok: false, message: error instanceof Error ? error.message : '投递待办读取失败' }
  }
}

function broadcastChanged() {
  window.dispatchEvent(new CustomEvent('gate-changed'))
}

function verdictOf(result: Verdict, todoId?: number) {
  lastVerdict.value = result
  if (todoId !== undefined) {
    rowMessages[todoId] = result.message ?? (result.ok ? '已处理' : '被拒绝')
    rowMessageOk[todoId] = Boolean(result.ok && result.can_send !== false)
  }
}

async function onRedeem(todo: DeliveryTodo) {
  const permitId = permitDraft(todo).trim()
  const result = await releaseApi.redeem({
    todo_id: todo.id,
    permit_id: permitId || undefined,
    receiver: todo.receiver,
  })
  verdictOf(result, todo.id)
  await reload()
  broadcastChanged()
}

async function onStart(todo: DeliveryTodo) {
  const permitId = permitDraft(todo).trim()
  const result = await releaseApi.start({
    todo_id: todo.id,
    permit_id: permitId || undefined,
    receiver: todo.receiver,
  })
  verdictOf(result, todo.id)
  await reload()
  broadcastChanged()
}

function scopedBatch(): BatchVersion | undefined {
  return overview.value.batches.find((b) => String(b.id) === scopedBatchId.value)
}

async function submitScoped() {
  const batch = scopedBatch()
  const result = await releaseApi.start({
    batch_key: batch?.batch_key,
    revision: batch?.revision_label,
    receiver: scopedReceiver.value.trim() || undefined,
    permit_id: scopedPermit.value.trim() || undefined,
  })
  lastVerdict.value = result
  await reload()
  broadcastChanged()
}

async function submitRevision(batch: BatchVersion) {
  const result = await releaseApi.start({
    batch_key: batch.batch_key,
    revision: batch.revision_label,
    receiver: scopedReceiver.value.trim() || undefined,
  })
  lastVerdict.value = result
  await reload()
  broadcastChanged()
}

async function submitCreateTodo() {
  if (!newTodo.receiver.trim()) {
    lastVerdict.value = { ok: false, message: '登记待发送行至少需要填写接收方' }
    return
  }
  const created = await releaseApi.createTodo({
    pipe_id: newTodo.pipe_id ? Number(newTodo.pipe_id) : undefined,
    batch_no: newTodo.batch_no || undefined,
    receiver: newTodo.receiver.trim(),
  })
  lastVerdict.value = { ok: created.ok, message: created.message ?? '已登记待发送行' }
  newTodo.pipe_id = ''
  newTodo.batch_no = ''
  newTodo.receiver = ''
  await reload()
  broadcastChanged()
}

function onGateChanged() { void reload() }

watch(() => props.tick, () => void reload())

onMounted(() => {
  void reload()
  window.addEventListener('gate-changed', onGateChanged)
  timer = setInterval(() => void reload(), 4000)
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
.gate-tip { color: var(--muted); font-size: 12.5px; margin: 4px 0 0; max-width: 820px; }
.gate-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; }
.gate-form.compact { background: #fff; border: 1px dashed var(--border); border-radius: 8px; padding: 10px; }
.gate-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 180px; }
.gate-field input, .gate-field select { border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; font-size: 13px; color: #0f172a; background: #fff; }
.version-submit-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.version-submit-item { display: inline-flex; }
.gate-block { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-top: 10px; overflow-x: auto; }
.gate-block h4 { margin: 0 0 8px; font-size: 14px; }
.banner { border-radius: 8px; padding: 9px 12px; font-size: 13px; margin: 8px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.banner-ok { background: #dcfce7; border: 1px solid #86efac; color: #166534; }
.banner-bad { background: #fee2e2; border: 1px solid #fca5a5; color: #991b1b; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; white-space: nowrap; }
.badge-ok { background: #dcfce7; color: #166534; }
.badge-stale { background: #fee2e2; color: #991b1b; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.row-actions.wrap { flex-wrap: wrap; gap: 8px; align-items: center; }
.row-msg { display: block; flex-basis: 100%; color: #991b1b; font-size: 12px; }
.row-msg-ok { color: #166534; }
.permit-input { width: 130px; border: 1px solid var(--border); border-radius: 6px; padding: 4px 6px; font-size: 12px; margin-bottom: 4px; }
.row-sendable { background: #f0fdf4; }
</style>

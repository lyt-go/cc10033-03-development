<template>
  <div v-if="open" class="modal-backdrop" data-testid="receipt-dialog" @click.self="$emit('close')">
    <div class="modal receipt-dialog" role="dialog" aria-modal="true" aria-label="放行回执">
      <h3>
        放行回执
        <span v-if="permit" class="permit-id" data-testid="dialog-permit-id">{{ permit.permit_id }}</span>
      </h3>

      <div v-if="loading" class="receipt-loading">回执读取中…</div>
      <div v-else-if="!permit" class="receipt-missing">找不到该回执，它可能尚未生成。</div>

      <template v-else>
        <div
          v-if="permit.needs_reconfirm"
          class="banner banner-warn"
          data-testid="dialog-review-required"
        >
          <strong>批次版本已变更：</strong>
          该回执签发给 {{ permit.revision_label }}，批次当前版本为
          <strong data-testid="dialog-current-version">{{ permit.current_revision_label ?? '更新版本' }}</strong>，
          旧回执保留可原样查证，但不能据此启动投递，需要负责人对新版本复核放行。
          <button
            v-if="permit.current_permit_id"
            class="btn primary"
            type="button"
            data-testid="dialog-goto-current"
            @click="$emit('open-current', permit.current_permit_id)"
          >
            打开当前版本回执（{{ permit.current_permit_id }}）
          </button>
        </div>

        <dl class="receipt-grid" data-testid="receipt-identity">
          <div><dt>许可身份</dt><dd data-testid="receipt-permit-id">{{ permit.permit_id }}</dd></div>
          <div><dt>回执编号</dt><dd>{{ permit.receipt_no }}</dd></div>
          <div><dt>放行范围</dt><dd data-testid="receipt-scope">{{ permit.scope }}</dd></div>
          <div><dt>接收方</dt><dd data-testid="receipt-receiver">{{ permit.receiver }}</dd></div>
          <div><dt>批次定位</dt><dd data-testid="receipt-batch-position">
            {{ permit.batch_no }} · {{ permit.revision_label }}
          </dd></div>
          <div><dt>事实状态</dt><dd><span class="badge">{{ permit.status }}</span></dd></div>
          <div>
            <dt>当前效力</dt>
            <dd>
              <span :class="['badge', permit.effective ? 'badge-ok' : 'badge-stale']" data-testid="receipt-effective">
                {{ permit.effective ? (permit.reason_code === 'ALREADY_REDEEMED' ? '已核销' : '当前有效/可核销') : '已被新版本取代' }}
              </span>
            </dd>
          </div>
          <div><dt>能否发送</dt><dd data-testid="receipt-can-send">{{ permit.can_send ? '可以' : '不可以' }}</dd></div>
        </dl>

        <div class="frozen-batch" data-testid="original-batch">
          <h4>原批次留档（更正/签收时冻结，可原样查证）</h4>
          <pre>{{ formatFrozen(permit) }}</pre>
        </div>

        <div v-if="permit.approval" class="approval-box" data-testid="receipt-approval">
          签批结果：<strong>{{ permit.approval.decision }}</strong>
          <span v-if="permit.approval.approver">（{{ permit.approval.approver }}）</span>
        </div>
      </template>

      <div class="page-actions">
        <button class="btn" type="button" data-testid="receipt-close" @click="$emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { PermitIdentity } from '@/api/release'

const props = defineProps<{
  open: boolean
  permit: PermitIdentity | null
  loading?: boolean
}>()

defineEmits<{
  (e: 'close'): void
  (e: 'open-current', permitId: string): void
}>()

const frozenText = computed(() => {
  const frozen = props.permit?.frozen_batch
  return frozen && Object.keys(frozen).length ? JSON.stringify(frozen, null, 2) : '无额外冻结信息'
})

function formatFrozen(permit: PermitIdentity): string {
  const frozen = permit.frozen_batch
  return frozen && Object.keys(frozen).length ? JSON.stringify(frozen, null, 2) : frozenText.value
}
</script>

<style scoped>
.receipt-dialog { width: min(640px, 100%); }
.permit-id { color: var(--brand); font-family: ui-monospace, monospace; margin-left: 8px; }
.receipt-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; margin: 12px 0; }
.receipt-grid dt { font-size: 12px; color: var(--muted); }
.receipt-grid dd { margin: 2px 0 0; font-size: 13px; word-break: break-all; }
.frozen-batch h4 { margin: 8px 0; font-size: 13px; }
.frozen-batch pre { background: #0f172a; color: #e2e8f0; padding: 10px; border-radius: 6px; font-size: 12px; max-height: 180px; overflow: auto; }
.banner { border-radius: 8px; padding: 10px 12px; font-size: 13px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.banner-warn { background: #fff7ed; border: 1px solid #fdba74; color: #9a3412; }
.approval-box { font-size: 13px; margin: 8px 0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2ff; color: #3730a3; font-size: 12px; }
.badge-ok { background: #dcfce7; color: #166534; }
.badge-stale { background: #fee2e2; color: #991b1b; }
.receipt-loading, .receipt-missing { font-size: 13px; color: var(--muted); padding: 12px 0; }
</style>

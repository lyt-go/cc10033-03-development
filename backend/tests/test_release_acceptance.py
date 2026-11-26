"""放行回执验收：R1 已放行 -> 更正 R2 -> 领取/提交并发 -> 两版能否启动投递 + 原领取请求重放。

对应需求：
- 交接负责人选择确定批次与接收方发起放行，生成可供投递服务核销的放行回执；
- 下载预检/导出完成不能代替放行；
- 更正批次后旧签收/回执原样可查，但旧许可不再能核销启动；
- 新接收方不能借用旧批次许可；
- 领取按 (回执, 接收方)/幂等键去重，重复提交原领取请求返回同一许可身份；
- 并发核销只有当前有效放行成功；
- 所有判定返回许可身份 + can_send + needs_reconfirm。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import SERVICES
from app.services.release_gate import release_gate


@pytest.fixture(autouse=True)
def _reset_gate():
    release_gate.reset()
    yield
    release_gate.reset()


def _completed_export(number: str = "ACCEPT-EXP"):
    ed = SERVICES["export_dataset"]
    entry, _ = ed.create_entry({"任务编号": number, "数据来源": "验收数据", "导出格式": "CSV"})
    ed.execute_export(entry["id"], data=[1, 2, 3])
    return entry


class TestReleaseReceiptAcceptance:
    def test_completed_export_and_precheck_cannot_replace_release(self):
        entry = _completed_export("PRECHECK-1")
        # 导出已完成
        assert entry["status"] == "已完成"
        # 但没有回执时不能启动投递
        denied = release_gate.start_delivery(export_id=entry["id"], receiver="接收方A")
        assert denied["ok"] is False
        assert denied["can_send"] is False
        assert denied["needs_reconfirm"] is True
        assert denied["code"] in {"RELEASE_REQUIRED", "RECEIPT_NOT_FOUND"}
        # 下载预检同样明确 release_required
        pre = release_gate.download_precheck(export_id=entry["id"])
        assert pre["file_ready"] is True
        assert pre["release_required"] is True
        assert pre["can_send"] is False

    def test_r1_r2_two_versions_delivery_and_claim_replay(self):
        entry = _completed_export("R1R2-MAIN")
        receiver = "验收接收方"

        # 1) 交接负责人发起放行 -> R1
        r1 = release_gate.initiate_release(export_id=entry["id"], receiver=receiver)
        assert r1["ok"] is True
        p1 = r1["permit_id"]
        assert r1["revision_label"] == "R1"
        assert r1["scope"]
        assert receiver in r1["scope"]
        assert r1["can_send"] is True

        # 2) 交接人更正批次 -> R2；R1 原样保留
        corrected = release_gate.correct_batch(export_id=entry["id"], reason="修正漏导行")
        assert corrected["ok"] is True
        assert corrected["batch"]["revision_label"] == "R2"
        old_batch = corrected["old_batch"]
        assert old_batch["revision_label"] == "R1"

        old_receipt = release_gate.get_receipt(p1)
        assert old_receipt is not None
        assert old_receipt["revision_label"] == "R1"
        assert old_receipt["verifiable"] is True
        # 版本已变：旧许可不再有效，返回当前 R2 身份提示复核
        assert old_receipt["effective"] is False
        assert old_receipt["needs_reconfirm"] is True
        assert old_receipt["current_revision_label"] == "R2"

        # 3) 接收方领取 R1（携带固定请求号）
        claim = release_gate.claim_receipt(
            permit_id=p1, receiver=receiver, request_id="original-claim-req")
        assert claim["ok"] is True          # 领取事实成立
        assert claim["claim_id"]
        claim_id = claim["claim_id"]
        # 但依据返回的许可身份：R1 已不能发送，需要负责人确认
        assert claim["can_send"] is False
        assert claim["needs_reconfirm"] is True
        assert claim["permit_id"] == p1

        # 4) 重复提交「原领取请求」：同幂等键 + 自然重复都返回同一领取/许可身份
        replay_same_key = release_gate.claim_receipt(
            permit_id=p1, receiver=receiver, request_id="original-claim-req")
        assert replay_same_key["claim_id"] == claim_id
        assert replay_same_key.get("idempotent_replay") is True
        replay_natural = release_gate.claim_receipt(permit_id=p1, receiver=receiver)
        assert replay_natural["claim_id"] == claim_id
        assert replay_natural.get("idempotent_replay") is True
        assert len(release_gate.list_claims(permit_id=p1)) == 1

        # 5) 新接收方不能借用旧批次的许可
        borrow = release_gate.claim_receipt(permit_id=p1, receiver="另一个接收方")
        assert borrow["ok"] is False
        assert borrow["code"] == "RECEIVER_MISMATCH"
        assert borrow["can_send"] is False
        # 即使持有 p1，错误接收方也不能核销
        borrow_redeem = release_gate.redeem_receipt(permit_id=p1, receiver="另一个接收方")
        assert borrow_redeem["ok"] is False
        assert borrow_redeem["code"] == "RECEIVER_MISMATCH"

        # 6) 查 R1 能否启动投递：不能（版本已被更正，只有当前有效放行可核销）
        start_r1 = release_gate.start_delivery(permit_id=p1, receiver=receiver)
        assert start_r1["ok"] is False
        assert start_r1["can_send"] is False
        assert start_r1["code"] == "VERSION_SUPERSEDED"
        assert start_r1["needs_reconfirm"] is True

        # 7) 负责人对 R2 重新放行；另一个操作员提交 R2 -> 可以启动
        r2 = release_gate.initiate_release(export_id=entry["id"], receiver=receiver)
        p2 = r2["permit_id"]
        assert p2 != p1
        assert r2["revision_label"] == "R2"
        assert r2["can_send"] is True

        start_r2 = release_gate.start_delivery(
            export_id=entry["id"], revision="R2", receiver=receiver,
            operator="另一个操作员", request_id="delivery-r2-1")
        assert start_r2["ok"] is True
        assert start_r2["can_send"] is True
        assert start_r2["code"] == "REDEEMED"
        assert start_r2["delivery"]["permit_id"] == p2

        # 8) 两版分别的最终结论
        verdict_r1 = release_gate.evaluate(permit_id=p1)
        verdict_r2 = release_gate.evaluate(permit_id=p2)
        assert verdict_r1["can_send"] is False
        assert verdict_r1["needs_reconfirm"] is True
        assert verdict_r2["can_send"] is True  # 已核销终态仍返回有效许可身份

        # 9) 投递待办已回写，且用回执标出了可发送版本
        todos = release_gate.list_todos()
        assert len(todos) >= 2
        markers = " ".join(t["marker"] for t in todos)
        assert "实际可发送" in markers

    def test_claim_before_correction_keeps_signed_record_but_blocks_new_redeem(self):
        """签收先于更正：签收事实冻结保留；更正后旧许可同样不能再被核销启动新投递。"""
        entry = _completed_export("SIGN-BEFORE-FIX")
        r1 = release_gate.initiate_release(export_id=entry["id"], receiver="Rcv")
        p1 = r1["permit_id"]
        claim = release_gate.claim_receipt(permit_id=p1, receiver="Rcv")
        assert claim["can_send"] is True
        release_gate.correct_batch(export_id=entry["id"])
        # 旧签收记录原样可查
        claims = release_gate.list_claims(permit_id=p1)
        assert len(claims) == 1
        receipt = release_gate.get_receipt(p1)
        assert receipt["status"] == "已签收"
        assert receipt["effective"] is False
        # 更正后核销被拒
        denied = release_gate.redeem_receipt(permit_id=p1, receiver="Rcv")
        assert denied["ok"] is False
        assert denied["code"] == "VERSION_SUPERSEDED"

    def test_concurrent_redeems_only_one_effective(self):
        release = release_gate.initiate_release(batch_no="CONC-1", receiver="Rcv")
        pid = release["permit_id"]
        results: list[dict] = []
        barrier = threading.Barrier(12)

        def redeem(i: int):
            barrier.wait()
            results.append(release_gate.redeem_receipt(
                permit_id=pid, receiver="Rcv", request_id=f"req-{i}"))

        threads = [threading.Thread(target=redeem, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        newly = [r for r in results if r.get("newly_redeemed")]
        replays = [r for r in results if r.get("already_redeemed")]
        assert len(newly) == 1
        assert len(replays) == 11
        records = [d for d in release_gate.overview()["deliveries"] if d["permit_id"] == pid]
        assert len(records) == 1

    def test_concurrent_identical_claims_create_one_record(self):
        release = release_gate.initiate_release(batch_no="CONC-CLAIM", receiver="Rcv")
        pid = release["permit_id"]
        barrier = threading.Barrier(8)
        out: list[dict] = []

        def claim():
            barrier.wait()
            out.append(release_gate.claim_receipt(permit_id=pid, receiver="Rcv"))

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(release_gate.list_claims(permit_id=pid)) == 1
        assert len({r["claim_id"] for r in out}) == 1

    def test_generic_action_endpoint_contract_shape(self):
        """服务层 run_action 遵循 (entry|None, message) 契约，供通用动作路由使用。"""
        svc = SERVICES["export_dataset"]
        v = svc.initiate_release(batch_no="GENERIC-1", receiver="R")
        pid = v["permit_id"]
        receipt_id = v["permit"]["id"]
        entry, message = release_gate.run_action(receipt_id, "核销")
        assert entry is not None
        assert "核销成功" in message
        # 未知动作保持基类式报错
        none_entry, msg = release_gate.run_action(receipt_id, "不存在的动作")
        assert none_entry is None
        assert "不属于" in msg
        # 避免未使用告警：pid 用于表达身份稳定
        assert entry["permit_id"] == pid

"""结算单与结算流水服务。

负责：
- 按账期/演出/场次为各结算方生成结算单
- 结算单确认、驳回、重算、打款结算
- 结算流水记录
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import F, QuerySet, Sum, Q
from django.utils import timezone

from ..models import (
    BoxOfficeFlow,
    Performance,
    SettlementFlow,
    SettlementItem,
    SettlementParty,
    SettlementStatement,
    Show,
    SplitDetail,
)
from .split_engine import q2, ZERO


def _gen_statement_no() -> str:
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"ST{ts}{rand}"


def _gen_settlement_flow_no() -> str:
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"SF{ts}{rand}"


class SettlementService:
    """结算单管理服务。"""

    @staticmethod
    @transaction.atomic
    def generate_statements(
        period_start: date,
        period_end: date,
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        party_ids: Optional[List[int]] = None,
    ) -> List[SettlementStatement]:
        """批量生成结算单（按账期+结算方）。

        返回生成的结算单列表。
        """
        existing = SettlementStatement.objects.filter(
            period_start=period_start,
            period_end=period_end,
        )
        if show_id:
            existing = existing.filter(show_id=show_id)
        if performance_id:
            existing = existing.filter(performance_id=performance_id)
        if party_ids:
            existing = existing.filter(party_id__in=party_ids)
        existing.delete()

        flows_qs = SplitDetail.objects.filter(
            biz_date__gte=period_start,
            biz_date__lte=period_end,
            is_settled=False,
        ).select_related("flow__show", "flow__performance", "flow__order", "party", "rule_item")

        if show_id:
            flows_qs = flows_qs.filter(flow__show_id=show_id)
        if performance_id:
            flows_qs = flows_qs.filter(flow__performance_id=performance_id)
        if party_ids:
            flows_qs = flows_qs.filter(party_id__in=party_ids)

        party_groups: Dict[int, List[SplitDetail]] = {}
        for sd in flows_qs:
            party_groups.setdefault(sd.party_id, []).append(sd)

        statements: List[SettlementStatement] = []
        for party_id, details in party_groups.items():
            party = details[0].party
            total_split = q2(sum(d.split_amount for d in details))
            total_rollback = q2(sum(d.rollback_amount for d in details))
            total_coupon = q2(sum(d.coupon_bear for d in details))
            total_points = q2(sum(d.points_bear for d in details))
            total_refund_bear = q2(sum(d.refund_bear for d in details))
            payable = q2(total_split - total_rollback - total_coupon - total_points - total_refund_bear)

            stmt = SettlementStatement.objects.create(
                statement_no=_gen_statement_no(),
                party=party,
                period_start=period_start,
                period_end=period_end,
                show_id=show_id,
                performance_id=performance_id,
                total_split_amount=total_split,
                total_rollback_amount=total_rollback,
                total_coupon_bear=total_coupon,
                total_points_bear=total_points,
                total_refund_bear=total_refund_bear,
                payable_amount=payable,
                paid_amount=ZERO,
                pending_amount=payable,
                status="generated",
            )
            items = []
            for d in details:
                item_amount = q2(d.split_amount - d.rollback_amount - d.coupon_bear - d.points_bear - d.refund_bear)
                order_no = d.flow.order.order_no if d.flow.order else ""
                perf = d.flow.performance
                items.append(SettlementItem(
                    statement=stmt,
                    split_detail=d,
                    flow=d.flow,
                    order_no=order_no,
                    performance_title=f"{perf.show.title} - {perf.hall}" if perf.show else perf.hall,
                    performance_time=perf.start_at,
                    split_amount=d.split_amount,
                    rollback_amount=d.rollback_amount,
                    coupon_bear=d.coupon_bear,
                    points_bear=d.points_bear,
                    refund_bear=d.refund_bear,
                    item_amount=item_amount,
                ))
            SettlementItem.objects.bulk_create(items, batch_size=500)
            statements.append(stmt)

        return statements

    @staticmethod
    @transaction.atomic
    def confirm_statement(statement_id: int, operator: str = "") -> SettlementStatement:
        """确认结算单。"""
        stmt = SettlementStatement.objects.select_for_update().get(pk=statement_id)
        if stmt.status in ("settled", "confirmed"):
            return stmt
        stmt.status = "confirmed"
        stmt.confirmed_by = operator
        stmt.confirmed_at = timezone.now()
        stmt.save(update_fields=["status", "confirmed_by", "confirmed_at"])
        return stmt

    @staticmethod
    @transaction.atomic
    def reject_statement(statement_id: int, reason: str = "") -> SettlementStatement:
        """驳回结算单。"""
        stmt = SettlementStatement.objects.select_for_update().get(pk=statement_id)
        stmt.status = "rejected"
        if reason:
            stmt.remark = (stmt.remark + "\n驳回原因：" + reason).strip()
        stmt.save(update_fields=["status", "remark"])
        return stmt

    @staticmethod
    @transaction.atomic
    def recalculate_statement(statement_id: int) -> SettlementStatement:
        """重算结算单。"""
        stmt = SettlementStatement.objects.select_for_update().get(pk=statement_id)
        if stmt.status == "settled":
            raise ValueError("已结算的结算单不允许重算")

        details = list(SplitDetail.objects.filter(
            biz_date__gte=stmt.period_start,
            biz_date__lte=stmt.period_end,
            party_id=stmt.party_id,
            is_settled=False,
        ))
        if stmt.show_id:
            details = [d for d in details if d.flow.show_id == stmt.show_id]
        if stmt.performance_id:
            details = [d for d in details if d.flow.performance_id == stmt.performance_id]

        total_split = q2(sum(d.split_amount for d in details))
        total_rollback = q2(sum(d.rollback_amount for d in details))
        total_coupon = q2(sum(d.coupon_bear for d in details))
        total_points = q2(sum(d.points_bear for d in details))
        total_refund_bear = q2(sum(d.refund_bear for d in details))
        payable = q2(total_split - total_rollback - total_coupon - total_points - total_refund_bear - stmt.paid_amount)

        SettlementItem.objects.filter(statement=stmt).delete()
        items = []
        for d in details:
            item_amount = q2(d.split_amount - d.rollback_amount - d.coupon_bear - d.points_bear - d.refund_bear)
            order_no = d.flow.order.order_no if d.flow.order else ""
            perf = d.flow.performance
            items.append(SettlementItem(
                statement=stmt,
                split_detail=d,
                flow=d.flow,
                order_no=order_no,
                performance_title=f"{perf.show.title} - {perf.hall}" if perf.show else perf.hall,
                performance_time=perf.start_at,
                split_amount=d.split_amount,
                rollback_amount=d.rollback_amount,
                coupon_bear=d.coupon_bear,
                points_bear=d.points_bear,
                refund_bear=d.refund_bear,
                item_amount=item_amount,
            ))
        SettlementItem.objects.bulk_create(items, batch_size=500)

        stmt.total_split_amount = total_split
        stmt.total_rollback_amount = total_rollback
        stmt.total_coupon_bear = total_coupon
        stmt.total_points_bear = total_points
        stmt.total_refund_bear = total_refund_bear
        stmt.payable_amount = q2(total_split - total_rollback - total_coupon - total_points - total_refund_bear)
        stmt.pending_amount = q2(stmt.payable_amount - stmt.paid_amount)
        stmt.status = "recalculated"
        stmt.save()
        return stmt

    @staticmethod
    @transaction.atomic
    def settle_statement(
        statement_id: int,
        amount: Optional[Decimal] = None,
        bank_transfer_no: str = "",
        operator: str = "",
        remark: str = "",
    ) -> Tuple[SettlementStatement, SettlementFlow]:
        """执行结算打款。"""
        stmt = SettlementStatement.objects.select_for_update().get(pk=statement_id)
        if stmt.status == "settled":
            raise ValueError("结算单已完成结算")
        if stmt.pending_amount <= ZERO:
            raise ValueError("无待付金额")

        pay_amount = q2(amount) if amount else q2(stmt.pending_amount)
        if pay_amount > stmt.pending_amount + Decimal("0.001"):
            raise ValueError(f"打款金额超过待付金额，待付：{stmt.pending_amount}")

        flow_type = "payout" if pay_amount > 0 else "receive"
        sflow = SettlementFlow.objects.create(
            flow_no=_gen_settlement_flow_no(),
            flow_type=flow_type,
            party=stmt.party,
            statement=stmt,
            amount=abs(pay_amount),
            bank_transfer_no=bank_transfer_no,
            status="completed",
            operator=operator,
            remark=remark,
            transfer_at=timezone.now(),
            confirmed_at=timezone.now(),
        )

        stmt.paid_amount = q2(stmt.paid_amount + abs(pay_amount))
        stmt.pending_amount = q2(stmt.payable_amount - stmt.paid_amount)
        if stmt.pending_amount <= Decimal("0.001"):
            stmt.pending_amount = ZERO
            stmt.status = "settled"
            stmt.settled_at = timezone.now()
        stmt.save()

        SplitDetail.objects.filter(
            settlement_items__statement=stmt
        ).update(is_settled=True)
        BoxOfficeFlow.objects.filter(
            settlement_items__statement=stmt
        ).update(is_settled=True)

        return stmt, sflow

    @staticmethod
    def query_statements(
        party_id: Optional[int] = None,
        status: Optional[str] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
        show_id: Optional[int] = None,
    ) -> QuerySet:
        """查询结算单列表。"""
        qs = SettlementStatement.objects.all().select_related("party", "show", "performance").order_by("-created_at")
        if party_id:
            qs = qs.filter(party_id=party_id)
        if status:
            qs = qs.filter(status=status)
        if period_start:
            qs = qs.filter(period_start__gte=period_start)
        if period_end:
            qs = qs.filter(period_end__lte=period_end)
        if show_id:
            qs = qs.filter(show_id=show_id)
        return qs

    @staticmethod
    @transaction.atomic
    def create_settlement_flow(
        party_id: int,
        flow_type: str,
        amount: Decimal,
        statement_id: Optional[int] = None,
        bank_transfer_no: str = "",
        operator: str = "",
        remark: str = "",
    ) -> SettlementFlow:
        """手动创建结算流水（调账等场景）。"""
        party = SettlementParty.objects.get(pk=party_id)
        stmt = None
        if statement_id:
            stmt = SettlementStatement.objects.get(pk=statement_id)
        sflow = SettlementFlow.objects.create(
            flow_no=_gen_settlement_flow_no(),
            flow_type=flow_type,
            party=party,
            statement=stmt,
            amount=q2(abs(amount)),
            bank_transfer_no=bank_transfer_no,
            status="pending",
            operator=operator,
            remark=remark,
        )
        if stmt and flow_type == "payout" and sflow.status == "completed":
            stmt.paid_amount = q2(stmt.paid_amount + sflow.amount)
            stmt.pending_amount = q2(stmt.payable_amount - stmt.paid_amount)
            if stmt.pending_amount <= ZERO:
                stmt.status = "settled"
                stmt.settled_at = timezone.now()
            stmt.save()
        return sflow

    @staticmethod
    def confirm_settlement_flow(flow_id: int) -> SettlementFlow:
        """确认结算流水到账。"""
        sflow = SettlementFlow.objects.select_for_update().get(pk=flow_id)
        sflow.status = "completed"
        sflow.confirmed_at = timezone.now()
        sflow.save(update_fields=["status", "confirmed_at"])
        if sflow.statement and sflow.flow_type == "payout":
            stmt = sflow.statement
            stmt.paid_amount = q2(stmt.paid_amount + sflow.amount)
            stmt.pending_amount = q2(stmt.payable_amount - stmt.paid_amount)
            if stmt.pending_amount <= ZERO:
                stmt.status = "settled"
                stmt.settled_at = timezone.now()
            stmt.save()
        return sflow

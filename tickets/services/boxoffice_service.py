"""票房归集服务。

负责：
- 将订单/退款转换为票房流水
- 维护票房汇总（按演出/场次/渠道/账期维度）
- 处理订单号、流水号生成
- 调用分账引擎进行分账
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import F, QuerySet, Sum
from django.utils import timezone

from ..models import (
    BoxOfficeFlow,
    BoxOfficeSummary,
    Channel,
    Performance,
    RefundRecord,
    SettlementParty,
    Show,
    SplitDetail,
    SplitRule,
    SplitRollback,
    TicketOrder,
)
from .split_engine import SplitInput, SplitRuleEngine, q2, ZERO


def _gen_flow_no(prefix: str = "BF") -> str:
    """生成票房流水号。"""
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:8].upper()
    return f"{prefix}{ts}{rand}"


def _gen_order_no(prefix: str = "T") -> str:
    """生成订单号。"""
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"{prefix}{ts}{rand}"


class BoxOfficeService:
    """票房归集服务。"""

    @staticmethod
    def _get_active_split_rule(performance: Performance) -> Optional[SplitRule]:
        """获取场次对应的生效分账规则。优先级：场次级 > 演出级。"""
        rule = (
            SplitRule.objects.filter(
                performance=performance,
                status="active",
            )
            .order_by("-updated_at")
            .select_related("show", "performance")
            .prefetch_related("items__party")
            .first()
        )
        if rule:
            return rule
        rule = (
            SplitRule.objects.filter(
                show=performance.show,
                scope_type="show",
                status="active",
            )
            .order_by("-updated_at")
            .select_related("show")
            .prefetch_related("items__party")
            .first()
        )
        return rule

    @staticmethod
    @transaction.atomic
    def collect_from_order(order: TicketOrder) -> Tuple[BoxOfficeFlow, List[SplitDetail]]:
        """从已支付订单归集票房流水并执行分账。"""
        if BoxOfficeFlow.objects.filter(order=order, flow_type="sale").exists():
            flow = BoxOfficeFlow.objects.filter(order=order, flow_type="sale").first()
            splits = list(SplitDetail.objects.filter(flow=flow).select_related("party"))
            return flow, splits

        if not order.order_no:
            order.order_no = _gen_order_no()
            order.save(update_fields=["order_no"])

        perf = order.performance
        show = perf.show
        biz_date = order.created_at.date() if order.created_at else timezone.now().date()

        gross = q2(order.paid_amount if order.paid_amount > 0 else order.amount)
        net_received = q2(gross - order.payment_fee - order.channel_fee)
        should_split = q2(net_received)

        flow = BoxOfficeFlow.objects.create(
            performance=perf,
            show=show,
            order=order,
            channel=order.channel,
            flow_type="sale",
            flow_no=_gen_flow_no("BFS"),
            quantity=order.quantity,
            ticket_amount=q2(order.original_amount),
            coupon_discount=q2(order.coupon_discount),
            points_discount=q2(order.points_discount),
            gross_amount=gross,
            payment_fee=q2(order.payment_fee),
            channel_fee=q2(order.channel_fee),
            refund_amount=ZERO,
            net_received=net_received,
            should_split_amount=should_split,
            is_settled=False,
            biz_date=biz_date,
        )

        rule = BoxOfficeService._get_active_split_rule(perf)
        split_details: List[SplitDetail] = []
        if rule:
            engine = SplitRuleEngine(rule)
            input_data = SplitInput(
                gross_amount=gross,
                refund_amount=ZERO,
                payment_fee=q2(order.payment_fee),
                channel_fee=q2(order.channel_fee),
                coupon_discount=q2(order.coupon_discount),
                points_discount=q2(order.points_discount),
                should_split_amount=should_split,
                coupon_bearer_party_id=order.coupon_bearer_party_id,
                points_bearer_party_id=order.points_bearer_party_id,
            )
            split_details = engine.apply_split(flow, input_data)

        BoxOfficeService._update_summaries(flow)
        return flow, split_details

    @staticmethod
    @transaction.atomic
    def process_refund(
        order: TicketOrder,
        refund_amount: Decimal,
        refund_quantity: int = 0,
        refund_fee: Decimal = ZERO,
        reason: str = "",
        operator: str = "",
    ) -> Tuple[RefundRecord, BoxOfficeFlow, List[SplitDetail]]:
        """处理退款：创建退款记录、生成回滚流水、执行分账回滚。"""
        qty = refund_quantity or order.quantity
        amt = q2(refund_amount)
        if amt <= ZERO:
            raise ValueError("退款金额必须大于0")

        remaining = q2(order.amount - order.refunded_amount)
        if amt > remaining:
            raise ValueError(f"退款金额超过可退金额，剩余可退：{remaining}")

        refund = RefundRecord.objects.create(
            order=order,
            refund_amount=amt,
            refund_quantity=qty,
            refund_fee=q2(refund_fee),
            reason=reason,
            operator=operator,
        )

        order.refunded_amount = q2(order.refunded_amount + amt)
        if order.refunded_amount >= order.amount - Decimal("0.001"):
            order.status = "refunded"
        else:
            order.status = "partial_refunded"
        order.save(update_fields=["refunded_amount", "status"])

        if refund_quantity > 0:
            perf = order.performance
            perf.sold_seats = max(0, perf.sold_seats - refund_quantity)
            perf.save(update_fields=["sold_seats"])

        original_flow = BoxOfficeFlow.objects.filter(order=order, flow_type="sale").first()
        if not original_flow:
            original_flow, _ = BoxOfficeService.collect_from_order(order)

        biz_date = timezone.now().date()
        perf = order.performance
        show = perf.show

        rollback_flow = BoxOfficeFlow.objects.create(
            performance=perf,
            show=show,
            order=order,
            refund=refund,
            channel=order.channel,
            flow_type="refund",
            flow_no=_gen_flow_no("BFR"),
            quantity=-qty,
            ticket_amount=q2(-amt),
            coupon_discount=ZERO,
            points_discount=ZERO,
            gross_amount=q2(-amt),
            payment_fee=q2(-refund_fee),
            channel_fee=ZERO,
            refund_amount=amt,
            net_received=q2(-amt + refund_fee),
            should_split_amount=q2(-amt + refund_fee),
            is_settled=False,
            biz_date=biz_date,
        )

        rule = BoxOfficeService._get_active_split_rule(perf)
        rollback_details: List[SplitDetail] = []
        parent_splits: Dict[int, SplitDetail] = {}
        for sd in SplitDetail.objects.filter(flow=original_flow, rollback_status="normal").select_related("party"):
            parent_splits[sd.party_id] = sd

        if rule and parent_splits:
            engine = SplitRuleEngine(rule)
            input_data = SplitInput(
                gross_amount=q2(-amt),
                refund_amount=amt,
                payment_fee=q2(-refund_fee),
                channel_fee=ZERO,
                coupon_discount=ZERO,
                points_discount=ZERO,
                should_split_amount=q2(-amt + refund_fee),
            )
            rollback_details = engine.apply_split(rollback_flow, input_data, parent_splits=parent_splits)

        SplitRollback.objects.create(
            refund=refund,
            order=order,
            original_flow=original_flow,
            rollback_flow=rollback_flow,
            rollback_reason=reason,
        )

        BoxOfficeService._update_summaries(rollback_flow)
        return refund, rollback_flow, rollback_details

    @staticmethod
    def _update_summaries(flow: BoxOfficeFlow) -> None:
        """更新各维度票房汇总。"""
        dims: List[Tuple[str, str, Dict]] = []
        dims.append((
            "performance",
            f"perf_{flow.performance_id}",
            {"performance": flow.performance, "show": flow.show},
        ))
        dims.append((
            "show",
            f"show_{flow.show_id}",
            {"show": flow.show},
        ))
        if flow.channel_id:
            dims.append((
                "channel",
                f"ch_{flow.channel_id}",
                {"channel": flow.channel, "show": flow.show},
            ))
        dims.append((
            "daily",
            flow.biz_date.isoformat(),
            {"period_start": flow.biz_date, "period_end": flow.biz_date, "show": flow.show},
        ))

        for dim, key, extra in dims:
            summary, _ = BoxOfficeSummary.objects.get_or_create(
                dimension=dim,
                dim_key=key,
                defaults={
                    **extra,
                    "total_orders": 0,
                    "total_quantity": 0,
                    "total_ticket_amount": ZERO,
                    "total_coupon_discount": ZERO,
                    "total_points_discount": ZERO,
                    "total_gross": ZERO,
                    "total_payment_fee": ZERO,
                    "total_channel_fee": ZERO,
                    "total_refund": ZERO,
                    "total_net_received": ZERO,
                    "total_should_split": ZERO,
                    "refund_count": 0,
                    "refund_quantity": 0,
                },
            )
            is_refund = flow.flow_type == "refund"
            summary.total_orders += 0 if is_refund else 1
            summary.total_quantity += flow.quantity
            summary.total_ticket_amount = q2(summary.total_ticket_amount + flow.ticket_amount)
            summary.total_coupon_discount = q2(summary.total_coupon_discount + flow.coupon_discount)
            summary.total_points_discount = q2(summary.total_points_discount + flow.points_discount)
            summary.total_gross = q2(summary.total_gross + flow.gross_amount)
            summary.total_payment_fee = q2(summary.total_payment_fee + flow.payment_fee)
            summary.total_channel_fee = q2(summary.total_channel_fee + flow.channel_fee)
            summary.total_refund = q2(summary.total_refund + flow.refund_amount)
            summary.total_net_received = q2(summary.total_net_received + flow.net_received)
            summary.total_should_split = q2(summary.total_should_split + flow.should_split_amount)
            if is_refund:
                summary.refund_count += 1
                summary.refund_quantity += abs(flow.quantity)
            summary.save()

    @staticmethod
    def rebuild_summaries() -> None:
        """重建所有票房汇总（幂等）。"""
        BoxOfficeSummary.objects.all().delete()
        for flow in BoxOfficeFlow.objects.all().iterator():
            BoxOfficeService._update_summaries(flow)

    @staticmethod
    def collect_all_orders() -> Dict[str, int]:
        """扫描所有已支付但未归集的订单，批量归集。"""
        collected = 0
        refunded = 0
        for order in TicketOrder.objects.filter(status="paid").iterator():
            if not BoxOfficeFlow.objects.filter(order=order, flow_type="sale").exists():
                BoxOfficeService.collect_from_order(order)
                collected += 1
        return {"collected": collected, "refunded": refunded}

    @staticmethod
    def get_period_flows(
        period_start: date,
        period_end: date,
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> QuerySet:
        """获取指定账期的票房流水。"""
        qs = BoxOfficeFlow.objects.filter(biz_date__gte=period_start, biz_date__lte=period_end)
        if show_id:
            qs = qs.filter(show_id=show_id)
        if performance_id:
            qs = qs.filter(performance_id=performance_id)
        if channel_id:
            qs = qs.filter(channel_id=channel_id)
        return qs.select_related("show", "performance", "order", "channel", "refund")

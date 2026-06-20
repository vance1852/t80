"""票房归集服务。

负责：
- 将订单/退款转换为票房流水（明细层，sale/refund）
- 按场次聚合并执行分账（场次结算层 settlement）
- 维护票房汇总（按演出/场次/渠道/账期维度）
- 处理流水号生成
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import F, Max, QuerySet, Sum
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


class BoxOfficeService:
    """票房归集服务（场次级聚合分账架构）。"""

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
    def collect_from_order(order: TicketOrder) -> BoxOfficeFlow:
        """从已支付订单创建 sale 型票房流水（仅明细，不执行分账）。

        分账会在 settle_performance 场次级聚合时执行。
        """
        existing = BoxOfficeFlow.objects.filter(order=order, flow_type="sale").first()
        if existing:
            return existing

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

        BoxOfficeService._update_summaries(flow)

        # 场次增加已售座位数
        if order.status in ("paid", "partial_refunded"):
            Performance.objects.filter(pk=perf.pk).update(sold_seats=F("sold_seats") + order.quantity)

        return flow

    @staticmethod
    @transaction.atomic
    def process_refund(
        order: TicketOrder,
        refund_amount: Decimal,
        refund_quantity: int = 0,
        refund_fee: Decimal = ZERO,
        reason: str = "",
        operator: str = "",
    ) -> Tuple[RefundRecord, BoxOfficeFlow]:
        """处理退款：创建退款记录 + refund 流水（不直接分账），随后自动重算场次结算。"""
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
            original_flow = BoxOfficeService.collect_from_order(order)

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

        SplitRollback.objects.create(
            refund=refund,
            order=order,
            original_flow=original_flow,
            rollback_flow=rollback_flow,
            rollback_reason=reason,
        )

        BoxOfficeService._update_summaries(rollback_flow)

        # 重算该场次的场次结算（聚合后再分账）
        BoxOfficeService.settle_performance(perf.pk)

        return refund, rollback_flow

    @staticmethod
    @transaction.atomic
    def settle_performance(performance_id: int) -> Tuple[Optional[BoxOfficeFlow], List[SplitDetail]]:
        """按场次聚合并执行分账（核心：fixed 租金只应用 1 次/场）。

        流程：
        1. 聚合该场次所有 sale/refund 明细流水
        2. 删除之前的 settlement 流水及其关联分账明细
        3. 创建新的场次结算流水 flow_type="settlement"
        4. 在 settlement 流水上执行分账引擎
        """
        perf = Performance.objects.select_related("show").get(pk=performance_id)
        show = perf.show

        # 1. 聚合所有明细流水（sale / refund）
        detail_flows = BoxOfficeFlow.objects.filter(
            performance_id=performance_id,
            flow_type__in=["sale", "refund"],
        )

        if not detail_flows.exists():
            # 没有明细流水，清除可能存在的旧 settlement
            BoxOfficeFlow.objects.filter(
                performance_id=performance_id,
                flow_type="settlement",
            ).delete()
            return None, []

        agg = detail_flows.aggregate(
            total_quantity=Sum("quantity"),
            total_ticket_amount=Sum("ticket_amount"),
            total_coupon=Sum("coupon_discount"),
            total_points=Sum("points_discount"),
            total_gross=Sum("gross_amount"),
            total_pay_fee=Sum("payment_fee"),
            total_ch_fee=Sum("channel_fee"),
            total_refund_amt=Sum("refund_amount"),
            total_net=Sum("net_received"),
            total_should=Sum("should_split_amount"),
            biz_date_max=Max("biz_date"),
        )

        t_qty = int(agg["total_quantity"] or 0)
        t_ticket = q2(agg["total_ticket_amount"])
        t_coupon = q2(agg["total_coupon"])
        t_points = q2(agg["total_points"])
        t_gross = q2(agg["total_gross"])
        t_pay_fee = q2(agg["total_pay_fee"])
        t_ch_fee = q2(agg["total_ch_fee"])
        t_refund = q2(agg["total_refund_amt"])
        t_net = q2(agg["total_net"])
        t_should = q2(agg["total_should"])
        biz_date = agg["biz_date_max"] or timezone.now().date()

        # 2. 聚合优惠/积分抵扣的承担方（按 party 分组累计）
        coupon_bear_map: Dict[int, Decimal] = {}
        points_bear_map: Dict[int, Decimal] = {}
        for order in TicketOrder.objects.filter(
            pk__in=detail_flows.exclude(order_id__isnull=True).values_list("order_id", flat=True)
        ).select_related("coupon_bearer_party", "points_bearer_party"):
            cpn_bearer_id = order.coupon_bearer_party_id
            pts_bearer_id = order.points_bearer_party_id
            if cpn_bearer_id and order.coupon_discount > ZERO:
                coupon_bear_map[cpn_bearer_id] = q2(
                    coupon_bear_map.get(cpn_bearer_id, ZERO) + order.coupon_discount
                )
            if pts_bearer_id and order.points_discount > ZERO:
                points_bear_map[pts_bearer_id] = q2(
                    points_bear_map.get(pts_bearer_id, ZERO) + order.points_discount
                )

        # 3. 删除之前的 settlement 流水和关联分账（先清分账再清流水，避免 FK 约束）
        old_settlements = BoxOfficeFlow.objects.filter(
            performance_id=performance_id,
            flow_type="settlement",
        )
        if old_settlements.exists():
            old_ids = list(old_settlements.values_list("pk", flat=True))
            # 删除分账明细（先清 FK 关联的自引用 parent_split）
            SplitDetail.objects.filter(flow_id__in=old_ids).update(parent_split=None)
            SplitDetail.objects.filter(flow_id__in=old_ids).delete()
            # 更新 summaries（settlement 流水不参与汇总，但以防万一）
            old_settlements.delete()

        # 4. 创建场次结算流水
        rule = BoxOfficeService._get_active_split_rule(perf)
        settle_flow = BoxOfficeFlow.objects.create(
            performance=perf,
            show=show,
            order=None,
            refund=None,
            channel=None,
            flow_type="settlement",
            flow_no=_gen_flow_no("BFSettle"),
            quantity=t_qty,
            ticket_amount=t_ticket,
            coupon_discount=t_coupon,
            points_discount=t_points,
            gross_amount=t_gross,
            payment_fee=t_pay_fee,
            channel_fee=t_ch_fee,
            refund_amount=t_refund,
            net_received=t_net,
            should_split_amount=t_should,
            is_settled=False,
            biz_date=biz_date,
        )

        # 5. 执行场次级分账
        split_details: List[SplitDetail] = []
        if rule and t_should != ZERO:
            engine = SplitRuleEngine(rule)
            input_data = SplitInput(
                gross_amount=t_gross,
                refund_amount=t_refund,
                payment_fee=t_pay_fee,
                channel_fee=t_ch_fee,
                coupon_discount=t_coupon,
                points_discount=t_points,
                should_split_amount=t_should,
                is_refund=False,
                coupon_bearer_type="platform",  # 默认，会被 by_party 覆盖
                points_bearer_type="platform",
                refund_bearer_type="share",
                coupon_bear_by_party=coupon_bear_map or None,
                points_bear_by_party=points_bear_map or None,
            )
            split_details = engine.apply_split(settle_flow, input_data)

        # 6. 标记所有明细流水为已结算（追溯用）
        detail_flows.update(is_settled=True)

        return settle_flow, split_details

    @staticmethod
    def settle_all_performances() -> Dict[str, int]:
        """批量重新结算所有有明细流水的场次。"""
        perf_ids = (
            BoxOfficeFlow.objects.filter(flow_type__in=["sale", "refund"])
            .values_list("performance_id", flat=True)
            .distinct()
        )
        settled = 0
        for pid in perf_ids:
            BoxOfficeService.settle_performance(pid)
            settled += 1
        return {"settled_performances": settled}

    @staticmethod
    def _update_summaries(flow: BoxOfficeFlow) -> None:
        """更新各维度票房汇总。settlement 类型流水跳过（已由 sale/refund 累计）。"""
        if flow.flow_type == "settlement":
            return

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
        """扫描所有已支付但未归集的订单，批量归集 + 批量执行场次结算。"""
        collected = 0
        for order in TicketOrder.objects.filter(status__in=["paid", "partial_refunded", "refunded"]).iterator():
            if not BoxOfficeFlow.objects.filter(order=order, flow_type="sale").exists():
                BoxOfficeService.collect_from_order(order)
                collected += 1
        settle_stats = BoxOfficeService.settle_all_performances()
        return {"collected": collected, **settle_stats}

    @staticmethod
    def get_period_flows(
        period_start: date,
        period_end: date,
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> QuerySet:
        """获取指定账期的票房流水（sale/refund 明细 + settlement 聚合）。"""
        qs = BoxOfficeFlow.objects.filter(biz_date__gte=period_start, biz_date__lte=period_end)
        if show_id:
            qs = qs.filter(show_id=show_id)
        if performance_id:
            qs = qs.filter(performance_id=performance_id)
        if channel_id:
            qs = qs.filter(channel_id=channel_id)
        return qs.select_related("show", "performance", "order", "channel", "refund")


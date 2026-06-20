"""分账模拟与多维财务报表服务。

负责：
- 分账模拟：给定票房数字和规则预览各方分账结果
- 多维报表：按演出/场地/渠道/时间维度聚合票房、退款、净收入、各方分成
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import F, Q, QuerySet, Sum, Count, CharField, Value
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from ..models import (
    BoxOfficeFlow,
    BoxOfficeSummary,
    Channel,
    Performance,
    SettlementParty,
    Show,
    SplitDetail,
    SplitRule,
)
from .split_engine import SplitEngineResult, SplitRuleEngine, q2, ZERO


class FinanceReportService:
    """多维财务报表与分账模拟。"""

    # ── 分账模拟 ──────────────────────────────────────────────────

    @staticmethod
    def simulate_split(
        rule_id: int,
        gross_amount: Decimal,
        refund_amount: Decimal = ZERO,
        payment_fee: Decimal = ZERO,
        channel_fee: Decimal = ZERO,
        coupon_discount: Decimal = ZERO,
        points_discount: Decimal = ZERO,
    ) -> Dict:
        """分账模拟：给定票房和规则预览各方分多少。

        Returns:
            包含各方分账明细的字典
        """
        rule = SplitRule.objects.prefetch_related("items__party").get(pk=rule_id)
        result: SplitEngineResult = SplitRuleEngine.simulate(
            rule=rule,
            gross_amount=gross_amount,
            refund_amount=refund_amount,
            payment_fee=payment_fee,
            channel_fee=channel_fee,
            coupon_discount=coupon_discount,
            points_discount=points_discount,
        )
        return {
            "rule": {
                "id": rule.id,
                "name": rule.name,
                "tax_rate": float(rule.tax_rate or 0),
                "scope": rule.scope_type,
                "item_count": rule.items.filter(is_active=True).count(),
            },
            "input": {
                "gross_amount": q2(gross_amount),
                "refund_amount": q2(refund_amount),
                "payment_fee": q2(payment_fee),
                "channel_fee": q2(channel_fee),
                "coupon_discount": q2(coupon_discount),
                "points_discount": q2(points_discount),
                "net_after_tax": q2(q2(gross_amount - refund_amount) * (1 - (rule.tax_rate or ZERO))),
            },
            "summary": {
                "tax_amount": result.tax_amount,
                "total_split_amount": result.total_split_amount,
                "total_rollback_amount": result.total_rollback_amount,
                "total_coupon_bear": result.total_coupon_bear,
                "total_points_bear": result.total_points_bear,
                "total_refund_bear": result.total_refund_bear,
                "rounding_adjustment": result.rounding_adjustment,
                "total_net_payable": result.total_net,
                "is_balanced": abs(result.checksum[0] - result.checksum[1]) < Decimal("0.01"),
            },
            "parties": [
                {
                    "party_id": pr.party_id,
                    "party_name": pr.party_name,
                    "party_type": pr.party_type,
                    "rule_item_id": pr.rule_item_id,
                    "base_amount": pr.base_amount,
                    "split_rate": float(pr.split_rate or 0),
                    "split_amount": pr.split_amount,
                    "rollback_amount": pr.rollback_amount,
                    "coupon_bear": pr.coupon_bear,
                    "points_bear": pr.points_bear,
                    "refund_bear": pr.refund_bear,
                    "net_amount": pr.net_amount,
                    "final_amount": pr.final_amount,
                    "calc_note": pr.calc_note,
                }
                for pr in result.party_results
            ],
            "warnings": result.warnings,
        }

    # ── 按演出维度报表 ─────────────────────────────────────────────

    @staticmethod
    def report_by_show(
        show_id: Optional[int] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> List[Dict]:
        """按演出维度的票房与分账报表。"""
        flows = BoxOfficeFlow.objects.select_related("show").filter(
            flow_type__in=["sale", "refund"]
        )
        if show_id:
            flows = flows.filter(show_id=show_id)
        if period_start:
            flows = flows.filter(biz_date__gte=period_start)
        if period_end:
            flows = filter_biz_date_lte(flows, period_end)

        agg = flows.values("show_id", "show__title", "show__genre", "show__status").annotate(
            order_count=Count("id", filter=Q(flow_type="sale")),
            total_quantity=Sum("quantity"),
            total_ticket_amount=Sum("ticket_amount"),
            total_coupon=Sum("coupon_discount"),
            total_points=Sum("points_discount"),
            total_gross=Sum("gross_amount"),
            total_payment_fee=Sum("payment_fee"),
            total_channel_fee=Sum("channel_fee"),
            total_refund=Sum("refund_amount"),
            total_net_received=Sum("net_received"),
            total_should_split=Sum("should_split_amount"),
            refund_count=Count("id", filter=Q(flow_type="refund")),
        ).order_by("show_id")

        result = []
        for row in agg:
            sid = row["show_id"]
            splits = SplitDetail.objects.filter(flow__show_id=sid)
            if period_start:
                splits = splits.filter(biz_date__gte=period_start)
            if period_end:
                splits = splits.filter(biz_date__lte=period_end)
            party_splits = splits.values("party_id", "party__name", "party__party_type").annotate(
                net_amount=Sum("net_amount"),
                coupon_bear=Sum("coupon_bear"),
                points_bear=Sum("points_bear"),
                refund_bear=Sum("refund_bear"),
            )
            party_list = [
                {
                    "party_id": p["party_id"],
                    "party_name": p["party__name"],
                    "party_type": p["party__party_type"],
                    "net_amount": q2(p["net_amount"]),
                    "coupon_bear": q2(p["coupon_bear"]),
                    "points_bear": q2(p["points_bear"]),
                    "refund_bear": q2(p["refund_bear"]),
                    "final_amount": q2(p["net_amount"] - p["coupon_bear"] - p["points_bear"] - p["refund_bear"]),
                }
                for p in party_splits
            ]
            total_split_net = q2(sum(p["net_amount"] for p in party_list))
            result.append({
                "show_id": sid,
                "show_title": row["show__title"],
                "genre": row["show__genre"],
                "status": row["show__status"],
                "order_count": row["order_count"],
                "refund_count": row["refund_count"],
                "total_quantity": row["total_quantity"],
                "total_ticket_amount": q2(row["total_ticket_amount"]),
                "total_coupon_discount": q2(row["total_coupon"]),
                "total_points_discount": q2(row["total_points"]),
                "total_gross": q2(row["total_gross"]),
                "total_payment_fee": q2(row["total_payment_fee"]),
                "total_channel_fee": q2(row["total_channel_fee"]),
                "total_refund": q2(row["total_refund"]),
                "total_net_received": q2(row["total_net_received"]),
                "total_should_split": q2(row["total_should_split"]),
                "total_split_net": total_split_net,
                "check_balance": q2(q2(row["total_net_received"]) - total_split_net),
                "party_splits": party_list,
            })
        return result

    # ── 按场次维度报表 ─────────────────────────────────────────────

    @staticmethod
    def report_by_performance(
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> List[Dict]:
        """按场次维度的票房与分账报表。"""
        flows = BoxOfficeFlow.objects.select_related("performance", "performance__show").filter(
            flow_type__in=["sale", "refund"]
        )
        if show_id:
            flows = flows.filter(show_id=show_id)
        if performance_id:
            flows = flows.filter(performance_id=performance_id)
        if period_start:
            flows = flows.filter(biz_date__gte=period_start)
        if period_end:
            flows = filter_biz_date_lte(flows, period_end)

        agg = flows.values(
            "performance_id", "show_id",
            "show__title", "performance__hall", "performance__start_at",
            "performance__total_seats", "performance__sold_seats", "performance__price",
        ).annotate(
            order_count=Count("id", filter=Q(flow_type="sale")),
            total_quantity=Sum("quantity"),
            total_ticket_amount=Sum("ticket_amount"),
            total_coupon=Sum("coupon_discount"),
            total_points=Sum("points_discount"),
            total_gross=Sum("gross_amount"),
            total_payment_fee=Sum("payment_fee"),
            total_channel_fee=Sum("channel_fee"),
            total_refund=Sum("refund_amount"),
            total_net_received=Sum("net_received"),
        ).order_by("performance__start_at")

        result = []
        for row in agg:
            pid = row["performance_id"]
            splits = SplitDetail.objects.filter(flow__performance_id=pid)
            if period_start:
                splits = splits.filter(biz_date__gte=period_start)
            if period_end:
                splits = splits.filter(biz_date__lte=period_end)
            party_splits = splits.values("party_id", "party__name", "party__party_type").annotate(
                net_amount=Sum("net_amount"),
                coupon_bear=Sum("coupon_bear"),
                points_bear=Sum("points_bear"),
                refund_bear=Sum("refund_bear"),
            )
            party_list = [
                {
                    "party_id": p["party_id"],
                    "party_name": p["party__name"],
                    "party_type": p["party__party_type"],
                    "net_amount": q2(p["net_amount"]),
                    "final_amount": q2(p["net_amount"] - p["coupon_bear"] - p["points_bear"] - p["refund_bear"]),
                }
                for p in party_splits
            ]
            result.append({
                "performance_id": pid,
                "show_id": row["show_id"],
                "show_title": row["show__title"],
                "hall": row["performance__hall"],
                "start_at": row["performance__start_at"],
                "total_seats": row["performance__total_seats"],
                "sold_seats": row["performance__sold_seats"],
                "price": row["performance__price"],
                "occupancy_rate": round(
                    row["performance__sold_seats"] / row["performance__total_seats"] * 100, 2
                ) if row["performance__total_seats"] else 0,
                "order_count": row["order_count"],
                "total_quantity": row["total_quantity"],
                "total_gross": q2(row["total_gross"]),
                "total_coupon_discount": q2(row["total_coupon"]),
                "total_points_discount": q2(row["total_points"]),
                "total_payment_fee": q2(row["total_payment_fee"]),
                "total_channel_fee": q2(row["total_channel_fee"]),
                "total_refund": q2(row["total_refund"]),
                "total_net_received": q2(row["total_net_received"]),
                "party_splits": party_list,
            })
        return result

    # ── 按渠道维度报表 ─────────────────────────────────────────────

    @staticmethod
    def report_by_channel(
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> List[Dict]:
        """按渠道维度的票房与佣金报表。"""
        flows = BoxOfficeFlow.objects.select_related("channel").filter(
            flow_type__in=["sale", "refund"]
        )
        if period_start:
            flows = flows.filter(biz_date__gte=period_start)
        if period_end:
            flows = filter_biz_date_lte(flows, period_end)

        agg = flows.filter(channel__isnull=False).values(
            "channel_id", "channel__name", "channel__code", "channel__default_commission_rate",
        ).annotate(
            order_count=Count("id", filter=Q(flow_type="sale")),
            total_quantity=Sum("quantity"),
            total_gross=Sum("gross_amount"),
            total_refund=Sum("refund_amount"),
            total_channel_fee=Sum("channel_fee"),
            total_net_received=Sum("net_received"),
        ).order_by("-total_gross")

        result = []
        for row in agg:
            cid = row["channel_id"]
            ch = Channel.objects.filter(pk=cid).first()
            party_id = ch.party_id if ch else None
            splits = SplitDetail.objects.filter(flow__channel_id=cid)
            if period_start:
                splits = splits.filter(biz_date__gte=period_start)
            if period_end:
                splits = splits.filter(biz_date__lte=period_end)
            ch_splits = splits.filter(party_id=party_id) if party_id else splits.none()
            ch_agg = ch_splits.aggregate(
                net=Sum("net_amount"),
                cb=Sum("coupon_bear"),
                pb=Sum("points_bear"),
                rb=Sum("refund_bear"),
            )
            result.append({
                "channel_id": cid,
                "channel_name": row["channel__name"],
                "channel_code": row["channel__code"],
                "commission_rate": float(row["channel__default_commission_rate"] or 0),
                "order_count": row["order_count"],
                "total_quantity": row["total_quantity"],
                "total_gross": q2(row["total_gross"]),
                "total_refund": q2(row["total_refund"]),
                "total_channel_fee": q2(row["total_channel_fee"]),
                "total_net_received": q2(row["total_net_received"]),
                "party_commission": q2(ch_agg.get("net") or ZERO),
                "party_final": q2(
                    (ch_agg.get("net") or ZERO)
                    - (ch_agg.get("cb") or ZERO)
                    - (ch_agg.get("pb") or ZERO)
                    - (ch_agg.get("rb") or ZERO)
                ),
            })
        return result

    # ── 按时间维度报表（日/月） ─────────────────────────────────────

    @staticmethod
    def report_by_time(
        granularity: str = "daily",
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
        show_id: Optional[int] = None,
    ) -> List[Dict]:
        """按时间维度（日/月）的趋势报表。"""
        flows = BoxOfficeFlow.objects.filter(flow_type__in=["sale", "refund"])
        if show_id:
            flows = flows.filter(show_id=show_id)
        if period_start:
            flows = flows.filter(biz_date__gte=period_start)
        if period_end:
            flows = filter_biz_date_lte(flows, period_end)

        trunc_func = TruncDate("biz_date") if granularity == "daily" else TruncMonth("biz_date")
        agg = flows.annotate(period=trunc_func).values("period").annotate(
            order_count=Count("id", filter=Q(flow_type="sale")),
            total_quantity=Sum("quantity"),
            total_gross=Sum("gross_amount"),
            total_coupon=Sum("coupon_discount"),
            total_points=Sum("points_discount"),
            total_refund=Sum("refund_amount"),
            total_fee=Sum("payment_fee") + Sum("channel_fee"),
            total_net_received=Sum("net_received"),
        ).order_by("period")

        result = []
        for row in agg:
            period_val = row["period"]
            splits = SplitDetail.objects.filter(biz_date=period_val) if granularity == "daily" else SplitDetail.objects.filter(biz_date__year=period_val.year, biz_date__month=period_val.month)
            if show_id:
                splits = splits.filter(flow__show_id=show_id)
            split_agg = splits.aggregate(
                net=Sum("net_amount"),
            )
            result.append({
                "period": period_val.isoformat() if period_val else None,
                "order_count": row["order_count"],
                "total_quantity": row["total_quantity"],
                "total_gross": q2(row["total_gross"]),
                "total_coupon_discount": q2(row["total_coupon"]),
                "total_points_discount": q2(row["total_points"]),
                "total_refund": q2(row["total_refund"]),
                "total_fee": q2(row["total_fee"]),
                "total_net_received": q2(row["total_net_received"]),
                "total_split_net": q2(split_agg.get("net") or ZERO),
            })
        return result

    # ── 按结算方维度报表 ──────────────────────────────────────────

    @staticmethod
    def report_by_party(
        party_id: Optional[int] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> List[Dict]:
        """按结算方维度的应分/已结/待结报表。"""
        qs = SplitDetail.objects.select_related("party", "flow__show")
        if party_id:
            qs = qs.filter(party_id=party_id)
        if period_start:
            qs = qs.filter(biz_date__gte=period_start)
        if period_end:
            qs = qs.filter(biz_date__lte=period_end)

        agg = qs.values(
            "party_id", "party__name", "party__party_type",
        ).annotate(
            split_amount=Sum("split_amount"),
            rollback_amount=Sum("rollback_amount"),
            coupon_bear=Sum("coupon_bear"),
            points_bear=Sum("points_bear"),
            refund_bear=Sum("refund_bear"),
            settled_split=Sum("net_amount", filter=Q(is_settled=True)),
            unsettled_split=Sum("net_amount", filter=Q(is_settled=False)),
        ).order_by("party__party_type", "party__name")

        from ..models import SettlementStatement, TicketOrder

        # 预计算每个 party 参与的 performance 集合，再统计订单数（场次级分账架构）
        perf_map: Dict[int, set] = {}
        perf_ids_qs = SplitDetail.objects.values_list("party_id", "flow__performance_id")
        if period_start:
            perf_ids_qs = perf_ids_qs.filter(biz_date__gte=period_start)
        if period_end:
            perf_ids_qs = perf_ids_qs.filter(biz_date__lte=period_end)
        for pid, perf_id in perf_ids_qs:
            if perf_id is None:
                continue
            perf_map.setdefault(pid, set()).add(perf_id)

        result = []
        for row in agg:
            pid = row["party_id"]
            perf_ids = list(perf_map.get(pid, set()))
            order_filter = TicketOrder.objects.filter(performance_id__in=perf_ids) if perf_ids else TicketOrder.objects.none()
            if period_start:
                order_filter = order_filter.filter(created_at__date__gte=period_start)
            if period_end:
                order_filter = order_filter.filter(created_at__date__lte=period_end)
            order_count = order_filter.count() if perf_ids else 0

            stmts = SettlementStatement.objects.filter(party_id=pid)
            if period_start:
                stmts = stmts.filter(period_end__gte=period_start)
            if period_end:
                stmts = stmts.filter(period_start__lte=period_end)
            stmt_agg = stmts.aggregate(
                total_payable=Sum("payable_amount"),
                total_paid=Sum("paid_amount"),
                total_pending=Sum("pending_amount"),
            )
            result.append({
                "party_id": pid,
                "party_name": row["party__name"],
                "party_type": row["party__party_type"],
                "order_count": order_count,
                "total_split_amount": q2(row["split_amount"]),
                "total_rollback_amount": q2(row["rollback_amount"]),
                "total_coupon_bear": q2(row["coupon_bear"]),
                "total_points_bear": q2(row["points_bear"]),
                "total_refund_bear": q2(row["refund_bear"]),
                "net_receivable": q2(
                    row["split_amount"] - row["rollback_amount"]
                    - row["coupon_bear"] - row["points_bear"] - row["refund_bear"]
                ),
                "settled_amount": q2(row["settled_split"] or ZERO),
                "unsettled_amount": q2(row["unsettled_split"] or ZERO),
                "stmt_total_payable": q2(stmt_agg.get("total_payable") or ZERO),
                "stmt_total_paid": q2(stmt_agg.get("total_paid") or ZERO),
                "stmt_total_pending": q2(stmt_agg.get("total_pending") or ZERO),
            })
        return result

    # ── 仪表盘财务总览 ─────────────────────────────────────────────

    @staticmethod
    def finance_dashboard(
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> Dict:
        """财务仪表盘总览。"""
        flows = BoxOfficeFlow.objects.filter(flow_type__in=["sale", "refund"])
        splits = SplitDetail.objects.all()
        if period_start:
            flows = flows.filter(biz_date__gte=period_start)
            splits = splits.filter(biz_date__gte=period_start)
        if period_end:
            flows = filter_biz_date_lte(flows, period_end)
            splits = splits.filter(biz_date__lte=period_end)

        flow_agg = flows.aggregate(
            order_count=Count("id", filter=Q(flow_type="sale")),
            refund_count=Count("id", filter=Q(flow_type="refund")),
            total_quantity=Sum("quantity"),
            total_gross=Sum("gross_amount"),
            total_refund=Sum("refund_amount"),
            total_fee=Sum("payment_fee") + Sum("channel_fee"),
            total_net_received=Sum("net_received"),
        )
        split_agg = splits.values("party__party_type").annotate(
            net=Sum("net_amount"),
        )
        party_type_breakdown = {
            row["party__party_type"]: q2(row["net"]) for row in split_agg
        }

        from ..models import SettlementStatement
        stmt_agg = SettlementStatement.objects.aggregate(
            total_pending=Sum("pending_amount", filter=Q(status__in=["generated", "confirmed", "recalculated"])),
            total_settled=Sum("paid_amount"),
        )

        from ..models import ReconciliationRecord
        last_recon = ReconciliationRecord.objects.order_by("-created_at").first()

        return {
            "period": {
                "start": period_start.isoformat() if period_start else None,
                "end": period_end.isoformat() if period_end else None,
            },
            "orders": {
                "total_count": flow_agg.get("order_count") or 0,
                "refund_count": flow_agg.get("refund_count") or 0,
                "total_quantity": flow_agg.get("total_quantity") or 0,
            },
            "box_office": {
                "total_gross": q2(flow_agg.get("total_gross") or ZERO),
                "total_refund": q2(flow_agg.get("total_refund") or ZERO),
                "total_fee": q2(flow_agg.get("total_fee") or ZERO),
                "total_net_received": q2(flow_agg.get("total_net_received") or ZERO),
            },
            "split_breakdown_by_type": party_type_breakdown,
            "settlement": {
                "total_pending": q2(stmt_agg.get("total_pending") or ZERO),
                "total_settled": q2(stmt_agg.get("total_settled") or ZERO),
            },
            "last_reconciliation": {
                "recon_no": last_recon.recon_no if last_recon else None,
                "status": last_recon.status if last_recon else None,
                "difference": str(last_recon.difference) if last_recon else None,
                "created_at": last_recon.created_at.isoformat() if last_recon else None,
            },
        }


def filter_biz_date_lte(qs: QuerySet, d: date) -> QuerySet:
    return qs.filter(biz_date__lte=d)

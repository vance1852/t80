"""财务对账与差异定位服务。

核心平账公式（任意时点）：
    票房实收 = 各方分账净额之和 + 退款 + 手续费(支付+渠道) + 优惠/积分抵扣承担

具体展开：
    实收 = SUM(BoxOfficeFlow.net_received)
    分账 = SUM(SplitDetail.net_amount) 即 (split_amount - rollback_amount)
    退款 = SUM(BoxOfficeFlow.refund_amount where flow_type='refund')
    手续费 = SUM(BoxOfficeFlow.payment_fee + BoxOfficeFlow.channel_fee) 注意符号
    承担 = SUM(SplitDetail.coupon_bear + SplitDetail.points_bear + SplitDetail.refund_bear)

    校验：
    实收 + 退款支出 + 手续费支出 = 分账净额 + 各项承担合计
    即：实收 - 分账净额合计 - 退款(已退) - 手续费(总额) - 承担 = 0
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import F, Q, QuerySet, Sum
from django.utils import timezone

from ..models import (
    BoxOfficeFlow,
    Performance,
    ReconciliationDiff,
    ReconciliationRecord,
    SettlementParty,
    Show,
    SplitDetail,
    SplitRule,
)
from .split_engine import q2, ZERO


def _gen_recon_no() -> str:
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"RC{ts}{rand}"


class ReconciliationService:
    """财务对账与差异定位服务。"""

    @staticmethod
    def _build_filter_kwargs(
        recon_type: str,
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> Tuple[Dict, Dict]:
        """构建 BoxOfficeFlow 和 SplitDetail 的过滤条件。"""
        flow_filters: Dict = {}
        split_filters: Dict = {}
        if recon_type == "show" and show_id:
            flow_filters["show_id"] = show_id
            split_filters["flow__show_id"] = show_id
        elif recon_type == "performance" and performance_id:
            flow_filters["performance_id"] = performance_id
            split_filters["flow__performance_id"] = performance_id
        elif recon_type in ("daily", "period") and period_start and period_end:
            flow_filters["biz_date__gte"] = period_start
            flow_filters["biz_date__lte"] = period_end
            split_filters["biz_date__gte"] = period_start
            split_filters["biz_date__lte"] = period_end
            if show_id:
                flow_filters["show_id"] = show_id
                split_filters["flow__show_id"] = show_id
            if performance_id:
                flow_filters["performance_id"] = performance_id
                split_filters["flow__performance_id"] = performance_id
        elif recon_type == "all":
            pass
        return flow_filters, split_filters

    @staticmethod
    def _aggregate_flow(flow_filters: Dict) -> Dict:
        """聚合票房流水（只统计明细层：sale + refund，排除 settlement 聚合层）。"""
        flows = BoxOfficeFlow.objects.filter(
            **flow_filters, flow_type__in=["sale", "refund"]
        )
        agg = flows.aggregate(
            total_net_received=Sum("net_received"),
            total_gross=Sum("gross_amount"),
            total_payment_fee=Sum("payment_fee"),
            total_channel_fee=Sum("channel_fee"),
            total_refund=Sum("refund_amount"),
            total_coupon=Sum("coupon_discount"),
            total_points=Sum("points_discount"),
            total_ticket=Sum("ticket_amount"),
            total_should_split=Sum("should_split_amount"),
        )
        return {
            "net_received": q2(agg.get("total_net_received") or ZERO),
            "gross": q2(agg.get("total_gross") or ZERO),
            "payment_fee": q2(agg.get("total_payment_fee") or ZERO),
            "channel_fee": q2(agg.get("total_channel_fee") or ZERO),
            "refund": q2(abs(agg.get("total_refund") or ZERO)),
            "coupon": q2(agg.get("total_coupon") or ZERO),
            "points": q2(agg.get("total_points") or ZERO),
            "ticket": q2(agg.get("total_ticket") or ZERO),
            "should_split": q2(agg.get("total_should_split") or ZERO),
            "flow_count": flows.count(),
        }

    @staticmethod
    def _aggregate_split(split_filters: Dict) -> Dict:
        """聚合分账明细。"""
        splits = SplitDetail.objects.filter(**split_filters)
        agg = splits.aggregate(
            total_split=Sum("split_amount"),
            total_rollback=Sum("rollback_amount"),
            total_coupon_bear=Sum("coupon_bear"),
            total_points_bear=Sum("points_bear"),
            total_refund_bear=Sum("refund_bear"),
            total_net=Sum("net_amount"),
        )
        split_amt = q2(agg.get("total_split") or ZERO)
        rollback_amt = q2(agg.get("total_rollback") or ZERO)
        coupon_bear = q2(agg.get("total_coupon_bear") or ZERO)
        points_bear = q2(agg.get("total_points_bear") or ZERO)
        refund_bear = q2(agg.get("total_refund_bear") or ZERO)
        net_amt = q2(agg.get("total_net") or ZERO)

        return {
            "split_amount": split_amt,
            "rollback_amount": rollback_amt,
            "coupon_bear": coupon_bear,
            "points_bear": points_bear,
            "refund_bear": refund_bear,
            "net_amount": net_amt,
            "bear_total": q2(coupon_bear + points_bear + refund_bear),
            "split_count": splits.count(),
        }

    @staticmethod
    def check_balance(
        recon_type: str = "all",
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> Dict:
        """检查平账状态，返回平账指标（不写数据库）。

        核心校验公式（场次级聚合分账架构）：
            净实收(明细层 sale+refund 的 net_received 合计)
                = SUM(settlement 层 SplitDetail.net_amount)

        说明：
            明细层 net_received = 用户实际现金到账
            结算层 SplitDetail 合计 = 按规则分给各方的金额总和
            coupon_bear / points_bear / refund_bear 是各方内部承担（从分账里扣），
                不影响总账现金，只影响各方实际到手金额
            因此总体现金恒等式：净实收 = 各方分账净额合计
        """
        flow_filters, split_filters = ReconciliationService._build_filter_kwargs(
            recon_type, show_id, performance_id, period_start, period_end
        )

        flow_agg = ReconciliationService._aggregate_flow(flow_filters)
        split_agg = ReconciliationService._aggregate_split(split_filters)

        net_received = flow_agg["net_received"]
        split_net = split_agg["net_amount"]
        bear_total = split_agg["bear_total"]
        total_fee = q2(abs(flow_agg["payment_fee"]) + abs(flow_agg["channel_fee"]))
        total_refund = flow_agg["refund"]

        difference = q2(net_received - split_net)

        is_balanced = abs(difference) < Decimal("0.01")

        return {
            "is_balanced": is_balanced,
            "difference": difference,
            "net_received": net_received,
            "split_net": split_net,
            "coupon_bear": split_agg["coupon_bear"],
            "points_bear": split_agg["points_bear"],
            "refund_bear": split_agg["refund_bear"],
            "bear_total": bear_total,
            "payment_fee": flow_agg["payment_fee"],
            "channel_fee": flow_agg["channel_fee"],
            "total_fee": total_fee,
            "refund_amount": total_refund,
            "gross_amount": flow_agg["gross"],
            "flow_count": flow_agg["flow_count"],
            "split_count": split_agg["split_count"],
            "formula": "净实收(明细层sale+refund) = 各方分账净额(SplitDetail合计)",
        }

    @staticmethod
    @transaction.atomic
    def run_reconciliation(
        recon_type: str = "all",
        show_id: Optional[int] = None,
        performance_id: Optional[int] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> ReconciliationRecord:
        """执行对账，生成对账记录和差异明细。"""
        dim_key = ""
        if recon_type == "show":
            dim_key = f"show_{show_id}" if show_id else ""
        elif recon_type == "performance":
            dim_key = f"perf_{performance_id}" if performance_id else ""
        elif recon_type == "daily":
            dim_key = period_start.isoformat() if period_start else ""
        elif recon_type == "period":
            dim_key = f"{period_start}_{period_end}" if period_start else ""
        else:
            dim_key = "all"

        check = ReconciliationService.check_balance(
            recon_type, show_id, performance_id, period_start, period_end
        )

        recon = ReconciliationRecord.objects.create(
            recon_no=_gen_recon_no(),
            recon_type=recon_type,
            dim_key=dim_key,
            period_start=period_start,
            period_end=period_end,
            show_id=show_id,
            performance_id=performance_id,
            total_net_received=check["net_received"],
            total_split_sum=check["split_net"],
            total_refund_sum=check["refund_amount"],
            total_fee_sum=check["total_fee"],
            total_coupon_points=check["bear_total"],
            difference=check["difference"],
            status=("balanced" if check["is_balanced"] else "unbalanced"),
            diff_count=0,
        )

        if not check["is_balanced"]:
            diffs = ReconciliationService._find_diffs(
                recon, recon_type, show_id, performance_id, period_start, period_end
            )
            recon.diff_count = len(diffs)
            recon.save(update_fields=["diff_count"])

        return recon

    @staticmethod
    def _find_diffs(
        recon: ReconciliationRecord,
        recon_type: str,
        show_id: Optional[int],
        performance_id: Optional[int],
        period_start: Optional[date],
        period_end: Optional[date],
    ) -> List[ReconciliationDiff]:
        """逐条流水定位差异。"""
        flow_filters, split_filters = ReconciliationService._build_filter_kwargs(
            recon_type, show_id, performance_id, period_start, period_end
        )

        diffs: List[ReconciliationDiff] = []

        flows = BoxOfficeFlow.objects.filter(**flow_filters).prefetch_related("split_details")
        flow_ids_with_split = set()
        flow_split_totals: Dict[int, Decimal] = {}
        flow_bear_totals: Dict[int, Decimal] = {}

        splits = SplitDetail.objects.filter(**split_filters).select_related("flow")
        for sd in splits:
            fid = sd.flow_id
            flow_ids_with_split.add(fid)
            flow_split_totals[fid] = q2(flow_split_totals.get(fid, ZERO) + sd.net_amount)
            flow_bear_totals[fid] = q2(
                flow_bear_totals.get(fid, ZERO) + sd.coupon_bear + sd.points_bear + sd.refund_bear
            )

        split_flow_ids = set()
        for sd in splits:
            if sd.flow_id:
                split_flow_ids.add(sd.flow_id)

        for flow in flows:
            fee = q2(abs(flow.payment_fee) + abs(flow.channel_fee))
            exp_net = q2(flow.net_received - fee + flow.refund_amount)
            act_net = q2(flow_split_totals.get(flow.id, ZERO) + flow_bear_totals.get(flow.id, ZERO))
            diff_amt = q2(exp_net - act_net)

            diff_type = "amount_mismatch"
            if flow.id not in flow_ids_with_split and flow.flow_type != "refund":
                diff_type = "flow_missing_split"
            elif abs(diff_amt) > Decimal("0.00") and abs(diff_amt) < Decimal("0.02"):
                diff_type = "rounding_error"
            elif abs(diff_amt) >= Decimal("0.02"):
                diff_type = "amount_mismatch"
            else:
                continue

            if flow.flow_type == "refund" and flow.id not in flow_ids_with_split:
                diff_type = "rollback_missing"

            if flow.order_id and flow.order.recon_diffs.filter(is_resolved=False).exists():
                continue

            d = ReconciliationDiff.objects.create(
                recon=recon,
                diff_type=diff_type,
                flow=flow,
                order=flow.order,
                expected_amount=exp_net,
                actual_amount=act_net,
                diff_amount=diff_amt,
                description=(
                    f"流水{flow.flow_no}({flow.flow_type}) "
                    f"期望值={exp_net} 实际分账+承担={act_net} 差异={diff_amt} | "
                    f"实收={flow.net_received} 退款={flow.refund_amount} 手续费={fee}"
                ),
            )
            diffs.append(d)

        for sd in splits:
            if sd.flow_id and sd.flow_id not in set(flows.values_list("id", flat=True)):
                d = ReconciliationDiff.objects.create(
                    recon=recon,
                    diff_type="split_missing_flow",
                    split_detail=sd,
                    expected_amount=ZERO,
                    actual_amount=sd.net_amount,
                    diff_amount=sd.net_amount,
                    description=f"分账明细{sd.id}关联的流水不存在，party={sd.party_id} 净额={sd.net_amount}",
                )
                diffs.append(d)

        return diffs

    @staticmethod
    def resolve_diff(diff_id: int, note: str = "") -> ReconciliationDiff:
        """标记差异已解决。"""
        d = ReconciliationDiff.objects.select_for_update().get(pk=diff_id)
        d.is_resolved = True
        d.resolved_note = note
        d.save(update_fields=["is_resolved", "resolved_note"])

        recon = d.recon
        unresolved = ReconciliationDiff.objects.filter(recon=recon, is_resolved=False).count()
        if unresolved == 0:
            recon.status = "adjusted"
            recon.save(update_fields=["status"])
        return d

"""分账规则引擎核心。

负责：
- 根据分账规则对票房流水进行多方分配
- 处理优先级、固定金额、比例抽成、剩余分配
- 处理优惠/积分抵扣、退票损失的承担方
- 保证四舍五入后总额一致（最后一方承担尾差）
- 退票冲销分账回滚
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from ..models import (
    BoxOfficeFlow,
    SettlementParty,
    SplitDetail,
    SplitRule,
    SplitRuleItem,
)


TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def q2(value) -> Decimal:
    """四舍五入到2位小数。"""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class SplitInput:
    """分账输入参数（一条票房流水）。"""

    gross_amount: Decimal = ZERO
    refund_amount: Decimal = ZERO
    payment_fee: Decimal = ZERO
    channel_fee: Decimal = ZERO
    coupon_discount: Decimal = ZERO
    points_discount: Decimal = ZERO
    should_split_amount: Decimal = ZERO

    is_refund: Optional[bool] = None

    coupon_bearer_type: str = "platform"
    points_bearer_type: str = "platform"
    refund_bearer_type: str = "share"

    coupon_bearer_party_id: Optional[int] = None
    points_bearer_party_id: Optional[int] = None

    coupon_bear_by_party: Optional[Dict[int, Decimal]] = None
    points_bear_by_party: Optional[Dict[int, Decimal]] = None
    refund_bear_by_party: Optional[Dict[int, Decimal]] = None


@dataclass
class PartySplitResult:
    """单个结算方的分账结果。"""

    party_id: int
    party_name: str = ""
    party_type: str = ""

    base_amount: Decimal = ZERO
    split_rate: Decimal = ZERO
    split_amount: Decimal = ZERO
    rollback_amount: Decimal = ZERO
    net_amount: Decimal = ZERO

    coupon_bear: Decimal = ZERO
    points_bear: Decimal = ZERO
    refund_bear: Decimal = ZERO

    rule_item_id: Optional[int] = None
    calc_note: str = ""

    @property
    def final_amount(self) -> Decimal:
        """最终净额 = 分账 - 回滚 - 各项承担。"""
        return q2(self.net_amount - self.coupon_bear - self.points_bear - self.refund_bear)


@dataclass
class SplitEngineResult:
    """分账引擎的完整输出。"""

    is_refund: bool = False
    tax_amount: Decimal = ZERO
    total_base_for_split: Decimal = ZERO
    total_split_amount: Decimal = ZERO
    total_rollback_amount: Decimal = ZERO
    total_coupon_bear: Decimal = ZERO
    total_points_bear: Decimal = ZERO
    total_refund_bear: Decimal = ZERO
    party_results: List[PartySplitResult] = field(default_factory=list)
    rounding_adjustment: Decimal = ZERO
    warnings: List[str] = field(default_factory=list)

    @property
    def total_net(self) -> Decimal:
        return q2(sum(p.final_amount for p in self.party_results))

    @property
    def checksum(self) -> Tuple[Decimal, Decimal]:
        """用于平账校验：(分账基数, 各方分账金额之和)。

        税方的分账已经包含在 party_results 里，所以：
          left = total_base_for_split ( = gross - refund - tax - 手续费，即应分账基数)
          right = SUM(split_amount - rollback_amount) 各方分账净额之和
        由于税也从基数中扣除并分配给了税方，因此所有 party 的 split_amount 合计 == total_base_for_split
        """
        left = self.total_base_for_split
        right = q2(
            sum(p.split_amount for p in self.party_results)
            - sum(p.rollback_amount for p in self.party_results)
        )
        return left, right


class SplitRuleEngine:
    """分账规则引擎。

    分账流程：
    1. 从票房流水中计算应分账基数（扣除退款、手续费）
    2. 按优先级依次处理每个分账规则项
    3. 每项支持：固定金额、比例抽成、剩余全部
    4. 计算基数支持：票房总额、退款后净额、税后净额、剩余可分配
    5. 税费按税率从基数中扣除（作为税务方的分账）
    6. 处理优惠/积分抵扣承担方分配
    7. 处理退票损失承担（仅退款冲销时）
    8. 最后一方补齐四舍五入尾差
    """

    def __init__(self, rule: SplitRule):
        self.rule = rule
        self.items: List[SplitRuleItem] = list(
            rule.items.filter(is_active=True).order_by("priority", "id").select_related("party")
        )
        self._party_id_to_idx: Dict[int, int] = {}
        for i, it in enumerate(self.items):
            if it.party_id not in self._party_id_to_idx:
                self._party_id_to_idx[it.party_id] = i

    def _find_tax_item(self) -> Optional[SplitRuleItem]:
        for it in self.items:
            if it.party.party_type == "tax":
                return it
        return None

    def _get_party_by_type(self, party_type: str) -> Optional[SettlementParty]:
        for it in self.items:
            if it.party.party_type == party_type:
                return it.party
        return None

    def _allocate_bearer(
        self,
        total: Decimal,
        bearer_type: str,
        specific_party_id: Optional[int],
        input_data: SplitInput,
        base_splits: Dict[int, Decimal],
    ) -> Dict[int, Decimal]:
        """根据承担方类型分配优惠/积分/退票损失。

        返回 {party_id: 承担金额}
        """
        result: Dict[int, Decimal] = {}
        if total <= ZERO:
            return result

        if specific_party_id and specific_party_id in self._party_id_to_idx:
            result[specific_party_id] = q2(total)
            return result

        if bearer_type == "share":
            total_split = q2(sum(base_splits.values()))
            if total_split <= ZERO:
                if self.items:
                    result[self.items[-1].party_id] = q2(total)
                return result
            allocated = ZERO
            items_list = list(self.items)
            for idx, it in enumerate(items_list):
                portion = base_splits.get(it.party_id, ZERO)
                if idx == len(items_list) - 1:
                    amt = q2(total - allocated)
                else:
                    amt = q2(total * portion / total_split)
                    allocated = q2(allocated + amt)
                if amt > ZERO:
                    result[it.party_id] = q2(result.get(it.party_id, ZERO) + amt)
            return result

        type_map = {
            "organizer": "organizer",
            "platform": "platform",
            "venue": "venue",
        }
        target_type = type_map.get(bearer_type)
        if target_type:
            party = self._get_party_by_type(target_type)
            if party:
                result[party.id] = q2(total)
                return result

        if self.items:
            result[self.items[-1].party_id] = q2(total)
        return result

    def calculate(self, input_data: SplitInput) -> SplitEngineResult:
        """执行分账计算，返回引擎结果（不写入数据库）。"""
        result = SplitEngineResult()
        if not self.items:
            result.warnings.append("分账规则无生效明细项")
            return result

        is_refund = input_data.is_refund
        if is_refund is None:
            is_refund = input_data.should_split_amount < ZERO or input_data.refund_amount > ZERO
        result.is_refund = is_refund

        gross = q2(input_data.gross_amount)
        refund = q2(input_data.refund_amount)
        pay_fee = q2(input_data.payment_fee)
        ch_fee = q2(input_data.channel_fee)
        coupon = q2(input_data.coupon_discount)
        points = q2(input_data.points_discount)

        net_after_refund = q2(gross - refund)
        tax_rate = self.rule.tax_rate if self.rule.tax_rate else ZERO
        tax_amount = q2(net_after_refund * tax_rate) if net_after_refund > ZERO else ZERO
        net_after_tax = q2(net_after_refund - tax_amount)
        result.tax_amount = tax_amount

        base_for_split = q2(input_data.should_split_amount)
        if base_for_split == ZERO:
            base_for_split = net_after_tax
        result.total_base_for_split = base_for_split

        party_results: List[PartySplitResult] = []
        base_splits: Dict[int, Decimal] = {}
        remaining = q2(base_for_split)
        total_allocated = ZERO

        tax_item = self._find_tax_item()
        processed_tax = False

        items_sorted = sorted(self.items, key=lambda x: (x.priority, x.id))
        n_items = len(items_sorted)

        for idx, it in enumerate(items_sorted):
            pr = PartySplitResult(
                party_id=it.party.id,
                party_name=it.party.name,
                party_type=it.party.party_type,
                rule_item_id=it.id,
            )

            is_tax_party = it.party.party_type == "tax"
            if is_tax_party and tax_item and it.id == tax_item.id and not processed_tax:
                pr.base_amount = net_after_refund
                pr.split_rate = tax_rate
                pr.split_amount = tax_amount
                pr.net_amount = q2(tax_amount)
                pr.calc_note = "税费扣除"
                processed_tax = True
                remaining = q2(remaining - tax_amount)
                total_allocated = q2(total_allocated + tax_amount)
                base_splits[it.party_id] = pr.split_amount
                party_results.append(pr)
                continue

            if it.calc_type == "fixed":
                calc_note_base = "固定金额"
                pr.base_amount = q2(it.fixed_amount)
                amt = q2(it.fixed_amount)
                pr.calc_note = f"{calc_note_base}: {amt}"

            elif it.calc_type == "rate":
                if it.calc_base == "gross":
                    b = gross
                    nb = "票房总额"
                elif it.calc_base == "net_after_refund":
                    b = net_after_refund
                    nb = "退款后净额"
                elif it.calc_base == "net_after_tax":
                    b = net_after_tax
                    nb = "税后净额"
                else:
                    b = remaining
                    nb = "剩余可分配"
                pr.base_amount = q2(b)
                pr.split_rate = it.rate or ZERO
                amt = q2(b * (it.rate or ZERO))
                pr.calc_note = f"{nb}*{float(it.rate or 0):.4%}={amt}"

            else:
                pr.base_amount = q2(remaining)
                pr.split_rate = ZERO
                amt = q2(remaining)
                pr.calc_note = f"剩余全部分配: {amt}"

            if it.min_amount and amt < it.min_amount:
                amt = q2(it.min_amount)
                pr.calc_note += f"，保底{amt}"
            if it.max_amount and it.max_amount > ZERO and amt > it.max_amount:
                amt = q2(it.max_amount)
                pr.calc_note += f"，封顶{amt}"

            if idx == n_items - 1 and not is_tax_party:
                remaining_after = q2(remaining - amt)
                if abs(remaining_after) > Decimal("0.001"):
                    result.rounding_adjustment = q2(result.rounding_adjustment + remaining_after)
                    amt = q2(amt + remaining_after)
                    pr.calc_note += f"，尾差调整{remaining_after}"

            if amt > remaining and it.calc_type == "remaining":
                amt = q2(remaining)

            if is_refund and amt != ZERO:
                pr.rollback_amount = q2(abs(amt))
                pr.split_amount = ZERO
                pr.net_amount = q2(-pr.rollback_amount)
            else:
                pr.split_amount = q2(amt)
                pr.net_amount = q2(pr.split_amount)

            base_splits[it.party_id] = q2(base_splits.get(it.party_id, ZERO) + pr.split_amount)
            total_allocated = q2(total_allocated + pr.split_amount)
            remaining = q2(remaining - pr.split_amount)
            party_results.append(pr)

        coupon_alloc = self._allocate_bearer(
            coupon,
            input_data.coupon_bearer_type,
            input_data.coupon_bearer_party_id,
            input_data,
            base_splits,
        )
        points_alloc = self._allocate_bearer(
            points,
            input_data.points_bearer_type,
            input_data.points_bearer_party_id,
            input_data,
            base_splits,
        )
        refund_alloc: Dict[int, Decimal] = {}
        if is_refund and refund > ZERO:
            refund_alloc = self._allocate_bearer(
                refund,
                input_data.refund_bearer_type,
                None,
                input_data,
                base_splits,
            )

        for pr in party_results:
            pr.coupon_bear = q2(coupon_alloc.get(pr.party_id, ZERO))
            pr.points_bear = q2(points_alloc.get(pr.party_id, ZERO))
            pr.refund_bear = q2(refund_alloc.get(pr.party_id, ZERO))

            if input_data.coupon_bear_by_party and pr.party_id in input_data.coupon_bear_by_party:
                pr.coupon_bear = q2(pr.coupon_bear + input_data.coupon_bear_by_party[pr.party_id])
            if input_data.points_bear_by_party and pr.party_id in input_data.points_bear_by_party:
                pr.points_bear = q2(pr.points_bear + input_data.points_bear_by_party[pr.party_id])
            if input_data.refund_bear_by_party and pr.party_id in input_data.refund_bear_by_party:
                pr.refund_bear = q2(pr.refund_bear + input_data.refund_bear_by_party[pr.party_id])

        result.party_results = party_results
        result.total_split_amount = q2(sum(p.split_amount for p in party_results))
        result.total_rollback_amount = q2(sum(p.rollback_amount for p in party_results))
        result.total_coupon_bear = q2(sum(p.coupon_bear for p in party_results))
        result.total_points_bear = q2(sum(p.points_bear for p in party_results))
        result.total_refund_bear = q2(sum(p.refund_bear for p in party_results))

        return result

    def apply_split(
        self,
        flow: BoxOfficeFlow,
        input_data: Optional[SplitInput] = None,
        parent_splits: Optional[Dict[int, SplitDetail]] = None,
    ) -> List[SplitDetail]:
        """执行分账并写入数据库。

        Args:
            flow: 票房流水
            input_data: 分账输入（若为None则从flow中提取）
            parent_splits: 冲销时对应的原始分账明细 {party_id: SplitDetail}

        Returns:
            创建的分账明细列表
        """
        if input_data is None:
            input_data = SplitInput(
                gross_amount=flow.gross_amount,
                refund_amount=flow.refund_amount,
                payment_fee=flow.payment_fee,
                channel_fee=flow.channel_fee,
                coupon_discount=flow.coupon_discount,
                points_discount=flow.points_discount,
                should_split_amount=flow.should_split_amount,
                is_refund=(flow.flow_type == "refund"),
            )

        calc_result = self.calculate(input_data)
        now = timezone.now()
        biz_date = now.date()

        details: List[SplitDetail] = []
        with transaction.atomic():
            for pr in calc_result.party_results:
                rollback_status = "normal"
                parent = None
                if calc_result.is_refund and parent_splits:
                    parent = parent_splits.get(pr.party_id)
                    if parent:
                        if pr.rollback_amount >= parent.net_amount:
                            rollback_status = "rollback_full"
                        else:
                            rollback_status = "rollback_partial"

                detail = SplitDetail.objects.create(
                    flow=flow,
                    rule=self.rule,
                    rule_item_id=pr.rule_item_id,
                    party_id=pr.party_id,
                    base_amount=pr.base_amount,
                    split_rate=pr.split_rate,
                    split_amount=pr.split_amount,
                    rollback_amount=pr.rollback_amount,
                    net_amount=pr.net_amount,
                    coupon_bear=pr.coupon_bear,
                    points_bear=pr.points_bear,
                    refund_bear=pr.refund_bear,
                    is_settled=False,
                    rollback_status=rollback_status,
                    parent_split=parent,
                    biz_date=biz_date,
                )
                details.append(detail)

                if parent and calc_result.is_refund:
                    parent.rollback_amount = q2(parent.rollback_amount + pr.rollback_amount)
                    parent.net_amount = q2(parent.split_amount - parent.rollback_amount)
                    if parent.net_amount <= ZERO:
                        parent.rollback_status = "rollback_full"
                    else:
                        parent.rollback_status = "rollback_partial"
                    parent.save(update_fields=["rollback_amount", "net_amount", "rollback_status"])

        return details

    @staticmethod
    def simulate(
        rule: SplitRule,
        gross_amount: Decimal,
        refund_amount: Decimal = ZERO,
        payment_fee: Decimal = ZERO,
        channel_fee: Decimal = ZERO,
        coupon_discount: Decimal = ZERO,
        points_discount: Decimal = ZERO,
    ) -> SplitEngineResult:
        """静态方法：给定票房和规则预览分账结果。"""
        engine = SplitRuleEngine(rule)
        should_split = q2(gross_amount - refund_amount - payment_fee - channel_fee)
        tax_rate = rule.tax_rate or ZERO
        net_after_refund = q2(gross_amount - refund_amount)
        tax = q2(net_after_refund * tax_rate)
        should_split = q2(net_after_refund - tax)
        input_data = SplitInput(
            gross_amount=q2(gross_amount),
            refund_amount=q2(refund_amount),
            payment_fee=q2(payment_fee),
            channel_fee=q2(channel_fee),
            coupon_discount=q2(coupon_discount),
            points_discount=q2(points_discount),
            should_split_amount=should_split,
            is_refund=False,
        )
        return engine.calculate(input_data)

from django.db import models
from django.db.models import F, Sum


class Show(models.Model):
    """演出剧目。"""

    GENRE_CHOICES = [
        ("concert", "演唱会"),
        ("drama", "话剧"),
        ("musical", "音乐剧"),
        ("opera", "戏曲"),
        ("other", "其他"),
    ]
    STATUS_CHOICES = [
        ("on_sale", "售票中"),
        ("upcoming", "待开票"),
        ("ended", "已结束"),
    ]

    title = models.CharField(max_length=128)
    troupe = models.CharField(max_length=128, blank=True, default="")
    genre = models.CharField(max_length=16, choices=GENRE_CHOICES, default="concert")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="upcoming")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shows"


class Performance(models.Model):
    """场次。"""

    show = models.ForeignKey(Show, on_delete=models.CASCADE, related_name="performances")
    hall = models.CharField(max_length=64, default="")
    start_at = models.DateTimeField()
    total_seats = models.IntegerField(default=0)
    sold_seats = models.IntegerField(default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "performances"


class SettlementParty(models.Model):
    """结算方：主办方、场地、渠道、平台、税务等。"""

    PARTY_TYPE_CHOICES = [
        ("organizer", "主办方"),
        ("venue", "场地"),
        ("channel", "渠道"),
        ("platform", "平台"),
        ("tax", "税务"),
        ("other", "其他"),
    ]

    name = models.CharField(max_length=128)
    party_type = models.CharField(max_length=16, choices=PARTY_TYPE_CHOICES)
    contact = models.CharField(max_length=64, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    bank_account = models.CharField(max_length=64, blank=True, default="")
    bank_name = models.CharField(max_length=128, blank=True, default="")
    remark = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "settlement_parties"
        indexes = [models.Index(fields=["party_type"])]


class Channel(models.Model):
    """售票渠道。"""

    name = models.CharField(max_length=64, unique=True)
    code = models.CharField(max_length=32, unique=True)
    default_commission_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    party = models.ForeignKey(SettlementParty, on_delete=models.SET_NULL, null=True, blank=True, related_name="channels")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "channels"


class TicketOrder(models.Model):
    """购票订单。"""

    STATUS_CHOICES = [
        ("paid", "已支付"),
        ("refunded", "已退款"),
        ("partial_refunded", "部分退款"),
        ("cancelled", "已取消"),
    ]

    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="orders")
    channel = models.ForeignKey(Channel, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    customer_name = models.CharField(max_length=64)
    phone = models.CharField(max_length=32, blank=True, default="")
    quantity = models.IntegerField(default=1)

    original_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    coupon_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    points_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    payment_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    channel_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    coupon_bearer_party = models.ForeignKey(
        SettlementParty, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="coupon_bearer_orders",
        help_text="优惠抵扣承担方",
    )
    points_bearer_party = models.ForeignKey(
        SettlementParty, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="points_bearer_orders",
        help_text="积分抵扣承担方",
    )

    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="paid")
    order_no = models.CharField(max_length=64, unique=True, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ticket_orders"
        indexes = [
            models.Index(fields=["performance", "status"]),
            models.Index(fields=["channel"]),
            models.Index(fields=["created_at"]),
        ]


class RefundRecord(models.Model):
    """退款记录。"""

    order = models.ForeignKey(TicketOrder, on_delete=models.CASCADE, related_name="refunds")
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refund_quantity = models.IntegerField(default=0)
    refund_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=256, blank=True, default="")
    operator = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "refund_records"
        indexes = [models.Index(fields=["order"]), models.Index(fields=["created_at"])]


class SplitRule(models.Model):
    """分账规则（按演出/场次配置）。"""

    SCOPE_CHOICES = [
        ("show", "演出"),
        ("performance", "场次"),
    ]
    STATUS_CHOICES = [
        ("draft", "草稿"),
        ("active", "生效"),
        ("inactive", "失效"),
    ]

    name = models.CharField(max_length=128)
    scope_type = models.CharField(max_length=16, choices=SCOPE_CHOICES, default="show")
    show = models.ForeignKey(Show, on_delete=models.CASCADE, related_name="split_rules", null=True, blank=True)
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="split_rules", null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="draft")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0.06, help_text="税率，如6%则填0.06")
    tax_priority = models.IntegerField(default=1, help_text="税费扣除优先级，数字越小越先扣")
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_to = models.DateTimeField(null=True, blank=True)
    remark = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "split_rules"
        indexes = [
            models.Index(fields=["show", "status"]),
            models.Index(fields=["performance", "status"]),
        ]


class SplitRuleItem(models.Model):
    """分账规则明细项。"""

    CALC_TYPE_CHOICES = [
        ("fixed", "固定金额"),
        ("rate", "比例抽成"),
        ("remaining", "剩余全部分配"),
    ]
    BEARER_TYPE_CHOICES = [
        ("organizer", "主办方承担"),
        ("platform", "平台承担"),
        ("venue", "场地承担"),
        ("share", "按分账比例分摊"),
    ]

    rule = models.ForeignKey(SplitRule, on_delete=models.CASCADE, related_name="items")
    party = models.ForeignKey(SettlementParty, on_delete=models.CASCADE, related_name="split_rule_items")

    calc_type = models.CharField(max_length=16, choices=CALC_TYPE_CHOICES, default="rate")
    fixed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rate = models.DecimalField(max_digits=5, decimal_places=4, default=0, help_text="比例，如50%则填0.50")

    priority = models.IntegerField(default=10, help_text="分配优先级，数字越小越先分配")
    calc_base = models.CharField(max_length=16, choices=[
        ("gross", "票房总额"),
        ("net_after_refund", "退款后净额"),
        ("net_after_tax", "税后净额"),
        ("remaining", "剩余可分配"),
    ], default="remaining")

    coupon_bearer_type = models.CharField(max_length=16, choices=BEARER_TYPE_CHOICES, default="platform", help_text="优惠抵扣承担方类型")
    points_bearer_type = models.CharField(max_length=16, choices=BEARER_TYPE_CHOICES, default="platform", help_text="积分抵扣承担方类型")
    refund_bearer_type = models.CharField(max_length=16, choices=BEARER_TYPE_CHOICES, default="share", help_text="退票损失承担方类型")

    min_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    max_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="0表示不限制")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "split_rule_items"
        ordering = ["priority", "id"]
        indexes = [
            models.Index(fields=["rule", "priority"]),
            models.Index(fields=["party"]),
        ]


class BoxOfficeFlow(models.Model):
    """票房流水：每一笔订单/退款对应的票房归集明细。"""

    FLOW_TYPE_CHOICES = [
        ("sale", "售票"),
        ("refund", "退款"),
        ("fee", "手续费"),
        ("adjust", "调账"),
    ]

    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="boxoffice_flows")
    show = models.ForeignKey(Show, on_delete=models.CASCADE, related_name="boxoffice_flows")
    order = models.ForeignKey(TicketOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="boxoffice_flows")
    refund = models.ForeignKey(RefundRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="boxoffice_flows")
    channel = models.ForeignKey(Channel, on_delete=models.SET_NULL, null=True, blank=True, related_name="boxoffice_flows")

    flow_type = models.CharField(max_length=16, choices=FLOW_TYPE_CHOICES)
    flow_no = models.CharField(max_length=64, unique=True)

    quantity = models.IntegerField(default=0)
    ticket_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="座位/套票原始金额")
    coupon_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    points_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="实收 = amount - 退款 - 手续费等前")
    payment_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    channel_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_received = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="实际到账净额")

    should_split_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="应分账基数（扣除退款/手续费等）")
    is_settled = models.BooleanField(default=False)
    biz_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "boxoffice_flows"
        indexes = [
            models.Index(fields=["performance", "flow_type"]),
            models.Index(fields=["show", "flow_type"]),
            models.Index(fields=["order"]),
            models.Index(fields=["created_at"]),
        ]


class BoxOfficeSummary(models.Model):
    """票房汇总（按演出/场次/账期维度预聚合）。"""

    DIMENSION_CHOICES = [
        ("show", "演出"),
        ("performance", "场次"),
        ("channel", "渠道"),
        ("daily", "按日"),
        ("period", "账期"),
    ]

    dimension = models.CharField(max_length=16, choices=DIMENSION_CHOICES)
    dim_key = models.CharField(max_length=64, help_text="维度值：演出ID/场次ID/渠道ID/日期/账期编号")
    show = models.ForeignKey(Show, on_delete=models.CASCADE, related_name="boxoffice_summaries", null=True, blank=True)
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="boxoffice_summaries", null=True, blank=True)
    channel = models.ForeignKey(Channel, on_delete=models.SET_NULL, null=True, blank=True, related_name="boxoffice_summaries")
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    total_orders = models.IntegerField(default=0)
    total_quantity = models.IntegerField(default=0)
    total_ticket_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_coupon_discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_points_discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_gross = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_payment_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_channel_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_refund = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_net_received = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_should_split = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    refund_count = models.IntegerField(default=0)
    refund_quantity = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "boxoffice_summaries"
        unique_together = [["dimension", "dim_key"]]
        indexes = [
            models.Index(fields=["dimension", "dim_key"]),
            models.Index(fields=["show"]),
            models.Index(fields=["performance"]),
        ]


class SplitDetail(models.Model):
    """分账明细：每一笔票房流水对应的各方分账金额。"""

    ROLLBACK_STATUS_CHOICES = [
        ("normal", "正常"),
        ("rollback_partial", "部分回滚"),
        ("rollback_full", "全部回滚"),
    ]

    flow = models.ForeignKey(BoxOfficeFlow, on_delete=models.CASCADE, related_name="split_details")
    rule = models.ForeignKey(SplitRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="split_details")
    rule_item = models.ForeignKey(SplitRuleItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="split_details")
    party = models.ForeignKey(SettlementParty, on_delete=models.CASCADE, related_name="split_details")

    base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="计算基数")
    split_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    split_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="应分金额（正）")
    rollback_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="已回滚金额（退票冲销）")
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="净额 = split_amount - rollback_amount")

    coupon_bear = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="本方承担的优惠抵扣")
    points_bear = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="本方承担的积分抵扣")
    refund_bear = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="本方承担的退票损失")

    is_settled = models.BooleanField(default=False)
    rollback_status = models.CharField(max_length=16, choices=ROLLBACK_STATUS_CHOICES, default="normal")
    parent_split = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="rollback_splits", help_text="冲销对应的原始分账明细")
    biz_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "split_details"
        indexes = [
            models.Index(fields=["flow"]),
            models.Index(fields=["party", "biz_date"]),
            models.Index(fields=["rule"]),
            models.Index(fields=["is_settled"]),
        ]


class SplitRollback(models.Model):
    """分账回滚记录（退票冲销）。"""

    refund = models.ForeignKey(RefundRecord, on_delete=models.CASCADE, related_name="rollbacks")
    order = models.ForeignKey(TicketOrder, on_delete=models.CASCADE, related_name="rollbacks")
    original_flow = models.ForeignKey(BoxOfficeFlow, on_delete=models.CASCADE, related_name="rollback_original_flows")
    rollback_flow = models.ForeignKey(BoxOfficeFlow, on_delete=models.CASCADE, related_name="rollback_flows")
    rollback_reason = models.CharField(max_length=256, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "split_rollbacks"


class SettlementStatement(models.Model):
    """结算单：按账期+结算方生成。"""

    STATUS_CHOICES = [
        ("generated", "已生成"),
        ("confirmed", "已确认"),
        ("settled", "已结算"),
        ("rejected", "已驳回"),
        ("recalculated", "已重算"),
    ]

    statement_no = models.CharField(max_length=64, unique=True)
    party = models.ForeignKey(SettlementParty, on_delete=models.CASCADE, related_name="statements")
    period_start = models.DateField()
    period_end = models.DateField()

    show = models.ForeignKey(Show, on_delete=models.SET_NULL, null=True, blank=True, related_name="statements")
    performance = models.ForeignKey(Performance, on_delete=models.SET_NULL, null=True, blank=True, related_name="statements")

    total_split_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_rollback_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_coupon_bear = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_points_bear = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_refund_bear = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="应付 = 应分 - 回滚 - 承担")
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    pending_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="待付 = 应付 - 已付")

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="generated")
    remark = models.TextField(blank=True, default="")
    confirmed_by = models.CharField(max_length=64, blank=True, default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "settlement_statements"
        indexes = [
            models.Index(fields=["party", "period_start", "period_end"]),
            models.Index(fields=["status"]),
            models.Index(fields=["statement_no"]),
        ]


class SettlementItem(models.Model):
    """结算单项：结算单关联的具体分账明细。"""

    statement = models.ForeignKey(SettlementStatement, on_delete=models.CASCADE, related_name="items")
    split_detail = models.ForeignKey(SplitDetail, on_delete=models.CASCADE, related_name="settlement_items")
    flow = models.ForeignKey(BoxOfficeFlow, on_delete=models.CASCADE, related_name="settlement_items")

    order_no = models.CharField(max_length=64, blank=True, default="")
    performance_title = models.CharField(max_length=256, blank=True, default="")
    performance_time = models.DateTimeField(null=True, blank=True)

    split_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rollback_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    coupon_bear = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    points_bear = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refund_bear = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    item_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "settlement_items"
        indexes = [models.Index(fields=["statement"]), models.Index(fields=["split_detail"])]


class ReconciliationRecord(models.Model):
    """对账记录。"""

    STATUS_CHOICES = [
        ("pending", "待对账"),
        ("balanced", "平账"),
        ("unbalanced", "不平"),
        ("adjusted", "已调账"),
    ]

    recon_no = models.CharField(max_length=64, unique=True)
    recon_type = models.CharField(max_length=16, choices=[
        ("show", "演出对账"),
        ("performance", "场次对账"),
        ("daily", "按日对账"),
        ("period", "账期对账"),
        ("all", "全域对账"),
    ], default="all")

    dim_key = models.CharField(max_length=64, blank=True, default="")
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    show = models.ForeignKey(Show, on_delete=models.SET_NULL, null=True, blank=True, related_name="recons")
    performance = models.ForeignKey(Performance, on_delete=models.SET_NULL, null=True, blank=True, related_name="recons")

    total_net_received = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="票房实收合计")
    total_split_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="各方分账净额之和")
    total_refund_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="退款合计")
    total_fee_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="手续费合计（支付+渠道）")
    total_coupon_points = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="优惠+积分抵扣承担合计")
    difference = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="差额 = 实收 - 分账 - 退款 - 手续费 - 承担")

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    diff_count = models.IntegerField(default=0)
    remark = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reconciliation_records"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["recon_type", "dim_key"]),
        ]


class ReconciliationDiff(models.Model):
    """对账差异明细。"""

    DIFF_TYPE_CHOICES = [
        ("flow_missing_split", "流水无分账"),
        ("split_missing_flow", "分账无流水"),
        ("amount_mismatch", "金额不匹配"),
        ("rounding_error", "四舍五入误差"),
        ("rollback_missing", "缺少回滚记录"),
        ("duplicate_split", "重复分账"),
        ("unknown", "未知差异"),
    ]

    recon = models.ForeignKey(ReconciliationRecord, on_delete=models.CASCADE, related_name="diffs")
    diff_type = models.CharField(max_length=32, choices=DIFF_TYPE_CHOICES, default="unknown")

    flow = models.ForeignKey(BoxOfficeFlow, on_delete=models.SET_NULL, null=True, blank=True, related_name="recon_diffs")
    split_detail = models.ForeignKey(SplitDetail, on_delete=models.SET_NULL, null=True, blank=True, related_name="recon_diffs")
    order = models.ForeignKey(TicketOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="recon_diffs")

    expected_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    actual_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    diff_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    description = models.TextField(blank=True, default="")
    is_resolved = models.BooleanField(default=False)
    resolved_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reconciliation_diffs"
        indexes = [
            models.Index(fields=["recon", "diff_type"]),
            models.Index(fields=["is_resolved"]),
        ]


class SettlementFlow(models.Model):
    """结算流水记录：实际打款/收款流水。"""

    FLOW_TYPE_CHOICES = [
        ("payout", "我方打款给结算方"),
        ("receive", "结算方打款给我方"),
        ("adjust", "调账"),
    ]
    STATUS_CHOICES = [
        ("pending", "待确认"),
        ("completed", "已完成"),
        ("failed", "失败"),
        ("cancelled", "已取消"),
    ]

    flow_no = models.CharField(max_length=64, unique=True)
    flow_type = models.CharField(max_length=16, choices=FLOW_TYPE_CHOICES)
    party = models.ForeignKey(SettlementParty, on_delete=models.CASCADE, related_name="settlement_flows")
    statement = models.ForeignKey(SettlementStatement, on_delete=models.SET_NULL, null=True, blank=True, related_name="settlement_flows")

    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default="CNY")
    bank_transfer_no = models.CharField(max_length=128, blank=True, default="")

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    operator = models.CharField(max_length=64, blank=True, default="")
    remark = models.TextField(blank=True, default="")

    transfer_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "settlement_flows"
        indexes = [
            models.Index(fields=["party", "flow_type"]),
            models.Index(fields=["statement"]),
            models.Index(fields=["status"]),
        ]

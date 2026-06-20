from rest_framework import serializers

from .models import (
    BoxOfficeFlow,
    BoxOfficeSummary,
    Channel,
    Performance,
    ReconciliationDiff,
    ReconciliationRecord,
    RefundRecord,
    SettlementFlow,
    SettlementItem,
    SettlementParty,
    SettlementStatement,
    Show,
    SplitDetail,
    SplitRollback,
    SplitRule,
    SplitRuleItem,
    TicketOrder,
)


# ── 基础序列化器（原有） ──────────────────────────────────────────

class ShowSerializer(serializers.ModelSerializer):
    class Meta:
        model = Show
        fields = ["id", "title", "troupe", "genre", "status", "created_at"]
        read_only_fields = ["id", "created_at"]


class PerformanceSerializer(serializers.ModelSerializer):
    show_title = serializers.CharField(source="show.title", read_only=True)
    remaining_seats = serializers.SerializerMethodField()

    class Meta:
        model = Performance
        fields = [
            "id", "show", "show_title", "hall", "start_at",
            "total_seats", "sold_seats", "remaining_seats", "price", "created_at",
        ]
        read_only_fields = ["id", "sold_seats", "created_at"]

    def get_remaining_seats(self, obj):
        return obj.total_seats - obj.sold_seats


class OrderSerializer(serializers.ModelSerializer):
    show_title = serializers.CharField(source="performance.show.title", read_only=True)
    channel_name = serializers.CharField(source="channel.name", read_only=True, default="")

    class Meta:
        model = TicketOrder
        fields = [
            "id", "performance", "show_title", "channel", "channel_name",
            "customer_name", "phone", "quantity",
            "original_amount", "coupon_discount", "points_discount",
            "amount", "paid_amount", "payment_fee", "channel_fee",
            "refunded_amount", "status", "order_no", "created_at",
        ]
        read_only_fields = ["id", "original_amount", "amount", "paid_amount", "status", "order_no", "created_at"]


class OrderCreateSerializer(serializers.Serializer):
    performance = serializers.IntegerField()
    channel = serializers.IntegerField(required=False, allow_null=True)
    customer_name = serializers.CharField(max_length=64)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    quantity = serializers.IntegerField(min_value=1, max_value=10)
    coupon_discount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    points_discount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    payment_fee = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    channel_fee = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    coupon_bearer_party = serializers.IntegerField(required=False, allow_null=True)
    points_bearer_party = serializers.IntegerField(required=False, allow_null=True)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()


# ── 结算方 / 渠道 ───────────────────────────────────────────────

class SettlementPartySerializer(serializers.ModelSerializer):
    class Meta:
        model = SettlementParty
        fields = [
            "id", "name", "party_type", "contact", "phone",
            "bank_account", "bank_name", "remark", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ChannelSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True, default="")

    class Meta:
        model = Channel
        fields = [
            "id", "name", "code", "default_commission_rate",
            "party", "party_name", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


# ── 退款 ────────────────────────────────────────────────────────

class RefundRecordSerializer(serializers.ModelSerializer):
    order_no = serializers.CharField(source="order.order_no", read_only=True, default="")

    class Meta:
        model = RefundRecord
        fields = [
            "id", "order", "order_no", "refund_amount", "refund_quantity",
            "refund_fee", "reason", "operator", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class RefundCreateSerializer(serializers.Serializer):
    order = serializers.IntegerField()
    refund_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    refund_quantity = serializers.IntegerField(min_value=1, required=False, default=1)
    refund_fee = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    reason = serializers.CharField(max_length=256, required=False, allow_blank=True, default="")
    operator = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


# ── 分账规则 ────────────────────────────────────────────────────

class SplitRuleItemSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True, default="")
    party_type = serializers.CharField(source="party.party_type", read_only=True, default="")

    class Meta:
        model = SplitRuleItem
        fields = [
            "id", "party", "party_name", "party_type",
            "calc_type", "fixed_amount", "rate",
            "priority", "calc_base",
            "coupon_bearer_type", "points_bearer_type", "refund_bearer_type",
            "min_amount", "max_amount", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class SplitRuleSerializer(serializers.ModelSerializer):
    items = SplitRuleItemSerializer(many=True, required=False)
    show_title = serializers.CharField(source="show.title", read_only=True, default="")
    performance_info = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = SplitRule
        fields = [
            "id", "name", "scope_type", "show", "show_title",
            "performance", "performance_info", "status",
            "tax_rate", "tax_priority",
            "effective_from", "effective_to", "remark",
            "item_count", "items",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_performance_info(self, obj):
        if obj.performance:
            return f"{obj.performance.show.title} - {obj.performance.hall} {obj.performance.start_at}"
        return ""

    def get_item_count(self, obj):
        return obj.items.count()


class SplitRuleCreateUpdateSerializer(serializers.ModelSerializer):
    items = SplitRuleItemSerializer(many=True)

    class Meta:
        model = SplitRule
        fields = [
            "name", "scope_type", "show", "performance", "status",
            "tax_rate", "tax_priority",
            "effective_from", "effective_to", "remark", "items",
        ]

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        rule = SplitRule.objects.create(**validated_data)
        for item_data in items_data:
            SplitRuleItem.objects.create(rule=rule, **item_data)
        return rule

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if items_data is not None:
            instance.items.all().delete()
            for item_data in items_data:
                SplitRuleItem.objects.create(rule=instance, **item_data)
        return instance


# ── 票房流水 / 汇总 ─────────────────────────────────────────────

class BoxOfficeFlowSerializer(serializers.ModelSerializer):
    show_title = serializers.CharField(source="show.title", read_only=True, default="")
    performance_hall = serializers.CharField(source="performance.hall", read_only=True, default="")
    channel_name = serializers.CharField(source="channel.name", read_only=True, default="")
    order_no = serializers.CharField(source="order.order_no", read_only=True, default="")
    split_details = serializers.SerializerMethodField()

    class Meta:
        model = BoxOfficeFlow
        fields = [
            "id", "flow_no", "flow_type",
            "show", "show_title", "performance", "performance_hall",
            "order", "order_no", "channel", "channel_name",
            "quantity", "ticket_amount",
            "coupon_discount", "points_discount",
            "gross_amount", "payment_fee", "channel_fee",
            "refund_amount", "net_received", "should_split_amount",
            "is_settled", "biz_date", "created_at",
            "split_details",
        ]
        read_only_fields = ["id", "flow_no", "created_at"]

    def get_split_details(self, obj):
        return list(obj.split_details.values(
            "id", "party_id", "party__name", "party__party_type",
            "split_amount", "rollback_amount", "net_amount",
            "coupon_bear", "points_bear", "refund_bear",
        ))


class BoxOfficeSummarySerializer(serializers.ModelSerializer):
    show_title = serializers.CharField(source="show.title", read_only=True, default="")
    channel_name = serializers.CharField(source="channel.name", read_only=True, default="")

    class Meta:
        model = BoxOfficeSummary
        fields = [
            "id", "dimension", "dim_key",
            "show", "show_title", "performance", "channel", "channel_name",
            "period_start", "period_end",
            "total_orders", "total_quantity",
            "total_ticket_amount", "total_coupon_discount", "total_points_discount",
            "total_gross", "total_payment_fee", "total_channel_fee",
            "total_refund", "total_net_received", "total_should_split",
            "refund_count", "refund_quantity",
            "created_at", "updated_at",
        ]


# ── 分账明细 ────────────────────────────────────────────────────

class SplitDetailSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True, default="")
    party_type = serializers.CharField(source="party.party_type", read_only=True, default="")
    flow_no = serializers.CharField(source="flow.flow_no", read_only=True, default="")
    rule_name = serializers.CharField(source="rule.name", read_only=True, default="")
    order_no = serializers.CharField(source="flow.order.order_no", read_only=True, default="")
    show_title = serializers.CharField(source="flow.show.title", read_only=True, default="")
    final_amount = serializers.SerializerMethodField()

    class Meta:
        model = SplitDetail
        fields = [
            "id", "flow", "flow_no", "rule", "rule_name",
            "party", "party_name", "party_type",
            "base_amount", "split_rate",
            "split_amount", "rollback_amount", "net_amount",
            "coupon_bear", "points_bear", "refund_bear",
            "final_amount",
            "is_settled", "rollback_status",
            "order_no", "show_title",
            "biz_date", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_final_amount(self, obj):
        return float(obj.net_amount) - float(obj.coupon_bear) - float(obj.points_bear) - float(obj.refund_bear)


class SplitRollbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = SplitRollback
        fields = [
            "id", "refund", "order",
            "original_flow", "rollback_flow",
            "rollback_reason", "created_at",
        ]


# ── 结算单 / 结算流水 ───────────────────────────────────────────

class SettlementItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SettlementItem
        fields = [
            "id", "statement", "split_detail", "flow",
            "order_no", "performance_title", "performance_time",
            "split_amount", "rollback_amount",
            "coupon_bear", "points_bear", "refund_bear",
            "item_amount", "created_at",
        ]


class SettlementStatementSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True, default="")
    party_type = serializers.CharField(source="party.party_type", read_only=True, default="")
    show_title = serializers.CharField(source="show.title", read_only=True, default="")
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = SettlementStatement
        fields = [
            "id", "statement_no",
            "party", "party_name", "party_type",
            "period_start", "period_end",
            "show", "show_title", "performance",
            "total_split_amount", "total_rollback_amount",
            "total_coupon_bear", "total_points_bear", "total_refund_bear",
            "payable_amount", "paid_amount", "pending_amount",
            "status", "remark",
            "confirmed_by", "confirmed_at", "settled_at",
            "item_count",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "statement_no", "created_at", "updated_at"]

    def get_item_count(self, obj):
        return obj.items.count()


class SettlementStatementDetailSerializer(SettlementStatementSerializer):
    items = SettlementItemSerializer(many=True, read_only=True)

    class Meta(SettlementStatementSerializer.Meta):
        fields = SettlementStatementSerializer.Meta.fields + ["items"]


class SettlementGenerateSerializer(serializers.Serializer):
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    show_id = serializers.IntegerField(required=False, allow_null=True)
    performance_id = serializers.IntegerField(required=False, allow_null=True)
    party_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=True)


class SettlementConfirmSerializer(serializers.Serializer):
    operator = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


class SettlementRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")


class SettlementSettleSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    bank_transfer_no = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    operator = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    remark = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")


class SettlementFlowSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True, default="")
    statement_no = serializers.CharField(source="statement.statement_no", read_only=True, default="")

    class Meta:
        model = SettlementFlow
        fields = [
            "id", "flow_no", "flow_type",
            "party", "party_name", "statement", "statement_no",
            "amount", "currency", "bank_transfer_no",
            "status", "operator", "remark",
            "transfer_at", "confirmed_at", "created_at",
        ]
        read_only_fields = ["id", "flow_no", "created_at"]


class SettlementFlowCreateSerializer(serializers.Serializer):
    party_id = serializers.IntegerField()
    flow_type = serializers.ChoiceField(choices=["payout", "receive", "adjust"])
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    statement_id = serializers.IntegerField(required=False, allow_null=True)
    bank_transfer_no = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    operator = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    remark = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")


# ── 对账 ────────────────────────────────────────────────────────

class ReconciliationDiffSerializer(serializers.ModelSerializer):
    flow_no = serializers.CharField(source="flow.flow_no", read_only=True, default="")
    order_no = serializers.CharField(source="order.order_no", read_only=True, default="")

    class Meta:
        model = ReconciliationDiff
        fields = [
            "id", "recon", "diff_type",
            "flow", "flow_no", "split_detail", "order", "order_no",
            "expected_amount", "actual_amount", "diff_amount",
            "description", "is_resolved", "resolved_note",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ReconciliationRecordSerializer(serializers.ModelSerializer):
    show_title = serializers.CharField(source="show.title", read_only=True, default="")
    diffs = ReconciliationDiffSerializer(many=True, read_only=True)

    class Meta:
        model = ReconciliationRecord
        fields = [
            "id", "recon_no", "recon_type", "dim_key",
            "period_start", "period_end",
            "show", "show_title", "performance",
            "total_net_received", "total_split_sum",
            "total_refund_sum", "total_fee_sum", "total_coupon_points",
            "difference", "status", "diff_count", "remark",
            "diffs",
            "created_at",
        ]
        read_only_fields = ["id", "recon_no", "created_at"]


class ReconciliationRunSerializer(serializers.Serializer):
    recon_type = serializers.ChoiceField(
        choices=["show", "performance", "daily", "period", "all"],
        required=False, default="all",
    )
    show_id = serializers.IntegerField(required=False, allow_null=True)
    performance_id = serializers.IntegerField(required=False, allow_null=True)
    period_start = serializers.DateField(required=False, allow_null=True)
    period_end = serializers.DateField(required=False, allow_null=True)


class ReconciliationResolveSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")


# ── 分账模拟 ────────────────────────────────────────────────────

class SplitSimulateSerializer(serializers.Serializer):
    rule_id = serializers.IntegerField()
    gross_amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    refund_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    payment_fee = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    channel_fee = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    coupon_discount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    points_discount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)


# ── 票房归集触发 ────────────────────────────────────────────────

class BoxOfficeCollectSerializer(serializers.Serializer):
    order_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=True)
    rebuild = serializers.BooleanField(required=False, default=False)

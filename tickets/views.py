from django.contrib.auth import authenticate
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from datetime import date

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
from .serializers import (
    BoxOfficeCollectSerializer,
    BoxOfficeFlowSerializer,
    BoxOfficeSummarySerializer,
    ChannelSerializer,
    LoginSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    PerformanceSerializer,
    ReconciliationDiffSerializer,
    ReconciliationRecordSerializer,
    ReconciliationResolveSerializer,
    ReconciliationRunSerializer,
    RefundCreateSerializer,
    RefundRecordSerializer,
    SettlementConfirmSerializer,
    SettlementFlowCreateSerializer,
    SettlementFlowSerializer,
    SettlementGenerateSerializer,
    SettlementPartySerializer,
    SettlementRejectSerializer,
    SettlementSettleSerializer,
    SettlementStatementDetailSerializer,
    SettlementStatementSerializer,
    ShowSerializer,
    SplitDetailSerializer,
    SplitRollbackSerializer,
    SplitRuleCreateUpdateSerializer,
    SplitRuleSerializer,
    SplitSimulateSerializer,
)
from .services.boxoffice_service import BoxOfficeService
from .services.finance_report_service import FinanceReportService
from .services.reconciliation_service import ReconciliationService
from .services.settlement_service import SettlementService
from .services.split_engine import SplitRuleEngine, q2, ZERO


# ── 认证 ────────────────────────────────────────────────────────

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = authenticate(username=s.validated_data["username"], password=s.validated_data["password"])
        if user is None:
            return Response({"detail": "用户名或密码错误"}, status=status.HTTP_401_UNAUTHORIZED)
        token = RefreshToken.for_user(user)
        return Response({"access_token": str(token.access_token), "token_type": "bearer"})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    u = request.user
    return Response({"id": u.id, "username": u.username, "display_name": u.get_full_name() or "平台管理员"})


# ── 基础（演出 / 场次 / 订单） ──────────────────────────────────

class ShowViewSet(viewsets.ModelViewSet):
    queryset = Show.objects.all().order_by("id")
    serializer_class = ShowSerializer


class PerformanceViewSet(viewsets.ModelViewSet):
    queryset = Performance.objects.select_related("show").all().order_by("start_at")
    serializer_class = PerformanceSerializer


class OrderViewSet(viewsets.ModelViewSet):
    queryset = TicketOrder.objects.select_related("performance", "performance__show", "channel").all().order_by("-id")
    http_method_names = ["get", "post"]

    def get_serializer_class(self):
        if self.action == "create":
            return OrderCreateSerializer
        return OrderSerializer

    def create(self, request, *args, **kwargs):
        s = OrderCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        try:
            perf = Performance.objects.select_related("show").get(pk=data["performance"])
        except Performance.DoesNotExist:
            return Response({"detail": "场次不存在"}, status=status.HTTP_404_NOT_FOUND)

        remaining = perf.total_seats - perf.sold_seats
        if data["quantity"] > remaining:
            return Response({"detail": "余票不足"}, status=status.HTTP_409_CONFLICT)

        channel = None
        if data.get("channel"):
            try:
                channel = Channel.objects.get(pk=data["channel"])
            except Channel.DoesNotExist:
                pass

        coupon_bearer = None
        if data.get("coupon_bearer_party"):
            try:
                coupon_bearer = SettlementParty.objects.get(pk=data["coupon_bearer_party"])
            except SettlementParty.DoesNotExist:
                pass

        points_bearer = None
        if data.get("points_bearer_party"):
            try:
                points_bearer = SettlementParty.objects.get(pk=data["points_bearer_party"])
            except SettlementParty.DoesNotExist:
                pass

        original_amount = q2(perf.price * data["quantity"])
        coupon_disc = q2(data.get("coupon_discount") or 0)
        points_disc = q2(data.get("points_discount") or 0)
        amount = q2(original_amount - coupon_disc - points_disc)
        pay_fee = q2(data.get("payment_fee") or 0)
        ch_fee = q2(data.get("channel_fee") or 0)
        paid = q2(amount)

        order = TicketOrder.objects.create(
            performance=perf,
            channel=channel,
            customer_name=data["customer_name"],
            phone=data.get("phone", ""),
            quantity=data["quantity"],
            original_amount=original_amount,
            coupon_discount=coupon_disc,
            points_discount=points_disc,
            amount=amount,
            paid_amount=paid,
            payment_fee=pay_fee,
            channel_fee=ch_fee,
            coupon_bearer_party=coupon_bearer,
            points_bearer_party=points_bearer,
            refunded_amount=ZERO,
            status="paid",
        )

        BoxOfficeService.collect_from_order(order)
        BoxOfficeService.settle_performance(perf.pk)

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_stats(request):
    show_total = Show.objects.count()
    show_on_sale = Show.objects.filter(status="on_sale").count()
    perf_total = Performance.objects.count()
    order_paid = TicketOrder.objects.filter(status="paid").count()
    sold = sum(p.sold_seats for p in Performance.objects.all())
    capacity = sum(p.total_seats for p in Performance.objects.all())
    return Response({
        "show_total": show_total,
        "show_on_sale": show_on_sale,
        "performance_total": perf_total,
        "order_paid": order_paid,
        "seats_sold": sold,
        "seats_capacity": capacity,
    })


# ── 结算方 / 渠道 ───────────────────────────────────────────────

class SettlementPartyViewSet(viewsets.ModelViewSet):
    queryset = SettlementParty.objects.all().order_by("party_type", "name")
    serializer_class = SettlementPartySerializer


class ChannelViewSet(viewsets.ModelViewSet):
    queryset = Channel.objects.select_related("party").all().order_by("name")
    serializer_class = ChannelSerializer


# ── 退款 ────────────────────────────────────────────────────────

class RefundViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    queryset = RefundRecord.objects.select_related("order").all().order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return RefundCreateSerializer
        return RefundRecordSerializer

    def create(self, request, *args, **kwargs):
        s = RefundCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        try:
            order = TicketOrder.objects.get(pk=data["order"])
        except TicketOrder.DoesNotExist:
            return Response({"detail": "订单不存在"}, status=status.HTTP_404_NOT_FOUND)

        try:
            refund, flow, splits = BoxOfficeService.process_refund(
                order=order,
                refund_amount=data["refund_amount"],
                refund_quantity=data.get("refund_quantity") or 0,
                refund_fee=data.get("refund_fee") or ZERO,
                reason=data.get("reason", ""),
                operator=data.get("operator", ""),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(RefundRecordSerializer(refund).data, status=status.HTTP_201_CREATED)


# ── 分账规则 ────────────────────────────────────────────────────

class SplitRuleViewSet(viewsets.ModelViewSet):
    queryset = SplitRule.objects.prefetch_related("items__party").select_related("show", "performance").all().order_by("-updated_at")

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return SplitRuleCreateUpdateSerializer
        return SplitRuleSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        show_id = self.request.query_params.get("show_id")
        perf_id = self.request.query_params.get("performance_id")
        status = self.request.query_params.get("status")
        if show_id:
            qs = qs.filter(show_id=show_id)
        if perf_id:
            qs = qs.filter(performance_id=perf_id)
        if status:
            qs = qs.filter(status=status)
        return qs

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        rule = self.get_object()
        rule.status = "active"
        rule.save(update_fields=["status"])
        return Response(SplitRuleSerializer(rule).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        rule = self.get_object()
        rule.status = "inactive"
        rule.save(update_fields=["status"])
        return Response(SplitRuleSerializer(rule).data)


# ── 票房流水 / 汇总 ─────────────────────────────────────────────

class BoxOfficeFlowViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BoxOfficeFlow.objects.select_related(
        "show", "performance", "order", "channel", "refund"
    ).prefetch_related("split_details__party").all().order_by("-created_at")
    serializer_class = BoxOfficeFlowSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        show_id = self.request.query_params.get("show_id")
        perf_id = self.request.query_params.get("performance_id")
        channel_id = self.request.query_params.get("channel_id")
        order_id = self.request.query_params.get("order_id")
        flow_type = self.request.query_params.get("flow_type")
        biz_start = self.request.query_params.get("biz_date_start")
        biz_end = self.request.query_params.get("biz_date_end")
        if show_id:
            qs = qs.filter(show_id=show_id)
        if perf_id:
            qs = qs.filter(performance_id=perf_id)
        if channel_id:
            qs = qs.filter(channel_id=channel_id)
        if order_id:
            qs = qs.filter(order_id=order_id)
        if flow_type:
            qs = qs.filter(flow_type=flow_type)
        if biz_start:
            qs = qs.filter(biz_date__gte=biz_start)
        if biz_end:
            qs = qs.filter(biz_date__lte=biz_end)
        return qs


class BoxOfficeSummaryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BoxOfficeSummary.objects.select_related("show", "channel").all()
    serializer_class = BoxOfficeSummarySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        dim = self.request.query_params.get("dimension")
        if dim:
            qs = qs.filter(dimension=dim)
        return qs


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def boxoffice_collect(request):
    """手动触发票房归集。"""
    s = BoxOfficeCollectSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    data = s.validated_data

    if data.get("rebuild"):
        BoxOfficeService.rebuild_summaries()
    result = BoxOfficeService.collect_all_orders()
    return Response(result)


# ── 分账明细 / 回滚 ─────────────────────────────────────────────

class SplitDetailViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SplitDetail.objects.select_related(
        "flow__show", "flow__performance", "flow__order",
        "party", "rule", "rule_item",
    ).all().order_by("-biz_date", "-id")
    serializer_class = SplitDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        party_id = self.request.query_params.get("party_id")
        flow_id = self.request.query_params.get("flow_id")
        show_id = self.request.query_params.get("show_id")
        perf_id = self.request.query_params.get("performance_id")
        biz_start = self.request.query_params.get("biz_date_start")
        biz_end = self.request.query_params.get("biz_date_end")
        is_settled = self.request.query_params.get("is_settled")
        if party_id:
            qs = qs.filter(party_id=party_id)
        if flow_id:
            qs = qs.filter(flow_id=flow_id)
        if show_id:
            qs = qs.filter(flow__show_id=show_id)
        if perf_id:
            qs = qs.filter(flow__performance_id=perf_id)
        if biz_start:
            qs = qs.filter(biz_date__gte=biz_start)
        if biz_end:
            qs = qs.filter(biz_date__lte=biz_end)
        if is_settled is not None:
            qs = qs.filter(is_settled=is_settled in ("1", "true", "True"))
        return qs


class SplitRollbackViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SplitRollback.objects.select_related(
        "refund", "order", "original_flow", "rollback_flow"
    ).all().order_by("-created_at")
    serializer_class = SplitRollbackSerializer


# ── 结算单 / 结算流水 ───────────────────────────────────────────

class SettlementStatementViewSet(viewsets.ModelViewSet):
    queryset = SettlementStatement.objects.select_related(
        "party", "show", "performance"
    ).prefetch_related("items").all().order_by("-created_at")
    http_method_names = ["get", "post", "patch"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return SettlementStatementDetailSerializer
        return SettlementStatementSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        party_id = self.request.query_params.get("party_id")
        status = self.request.query_params.get("status")
        show_id = self.request.query_params.get("show_id")
        period_start = self.request.query_params.get("period_start")
        period_end = self.request.query_params.get("period_end")
        if party_id:
            qs = qs.filter(party_id=party_id)
        if status:
            qs = qs.filter(status=status)
        if show_id:
            qs = qs.filter(show_id=show_id)
        if period_start:
            qs = qs.filter(period_start__gte=period_start)
        if period_end:
            qs = qs.filter(period_end__lte=period_end)
        return qs

    @action(detail=False, methods=["post"])
    def generate(self, request):
        s = SettlementGenerateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        statements = SettlementService.generate_statements(
            period_start=d["period_start"],
            period_end=d["period_end"],
            show_id=d.get("show_id"),
            performance_id=d.get("performance_id"),
            party_ids=d.get("party_ids"),
        )
        return Response(
            SettlementStatementSerializer(statements, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        s = SettlementConfirmSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        stmt = SettlementService.confirm_statement(int(pk), s.validated_data.get("operator", ""))
        return Response(SettlementStatementSerializer(stmt).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        s = SettlementRejectSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        stmt = SettlementService.reject_statement(int(pk), s.validated_data.get("reason", ""))
        return Response(SettlementStatementSerializer(stmt).data)

    @action(detail=True, methods=["post"])
    def recalculate(self, request, pk=None):
        try:
            stmt = SettlementService.recalculate_statement(int(pk))
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SettlementStatementSerializer(stmt).data)

    @action(detail=True, methods=["post"])
    def settle(self, request, pk=None):
        s = SettlementSettleSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            stmt, sflow = SettlementService.settle_statement(
                statement_id=int(pk),
                amount=d.get("amount"),
                bank_transfer_no=d.get("bank_transfer_no", ""),
                operator=d.get("operator", ""),
                remark=d.get("remark", ""),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "statement": SettlementStatementSerializer(stmt).data,
            "settlement_flow": SettlementFlowSerializer(sflow).data,
        })


class SettlementFlowViewSet(viewsets.ModelViewSet):
    queryset = SettlementFlow.objects.select_related("party", "statement").all().order_by("-created_at")
    http_method_names = ["get", "post", "patch"]

    def get_serializer_class(self):
        if self.action == "create":
            return SettlementFlowCreateSerializer
        return SettlementFlowSerializer

    def create(self, request, *args, **kwargs):
        s = SettlementFlowCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            flow = SettlementService.create_settlement_flow(
                party_id=d["party_id"],
                flow_type=d["flow_type"],
                amount=d["amount"],
                statement_id=d.get("statement_id"),
                bank_transfer_no=d.get("bank_transfer_no", ""),
                operator=d.get("operator", ""),
                remark=d.get("remark", ""),
            )
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SettlementFlowSerializer(flow).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        try:
            flow = SettlementService.confirm_settlement_flow(int(pk))
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SettlementFlowSerializer(flow).data)


# ── 对账 ────────────────────────────────────────────────────────

class ReconciliationViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = ReconciliationRecord.objects.prefetch_related("diffs").select_related("show").all().order_by("-created_at")
    serializer_class = ReconciliationRecordSerializer

    @action(detail=False, methods=["post"])
    def check(self, request):
        """检查平账状态（不写数据库）。"""
        s = ReconciliationRunSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        result = ReconciliationService.check_balance(
            recon_type=d.get("recon_type", "all"),
            show_id=d.get("show_id"),
            performance_id=d.get("performance_id"),
            period_start=d.get("period_start"),
            period_end=d.get("period_end"),
        )
        return Response(result)

    @action(detail=False, methods=["post"])
    def run(self, request):
        """执行对账，生成对账记录和差异明细。"""
        s = ReconciliationRunSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        recon = ReconciliationService.run_reconciliation(
            recon_type=d.get("recon_type", "all"),
            show_id=d.get("show_id"),
            performance_id=d.get("performance_id"),
            period_start=d.get("period_start"),
            period_end=d.get("period_end"),
        )
        return Response(ReconciliationRecordSerializer(recon).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resolve_recon_diff(request, diff_id):
    s = ReconciliationResolveSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    try:
        d = ReconciliationService.resolve_diff(diff_id, s.validated_data.get("note", ""))
    except ReconciliationDiff.DoesNotExist:
        return Response({"detail": "差异记录不存在"}, status=status.HTTP_404_NOT_FOUND)
    return Response(ReconciliationDiffSerializer(d).data)


# ── 分账模拟 ────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def split_simulate(request):
    s = SplitSimulateSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data
    try:
        result = FinanceReportService.simulate_split(
            rule_id=d["rule_id"],
            gross_amount=d["gross_amount"],
            refund_amount=d.get("refund_amount") or ZERO,
            payment_fee=d.get("payment_fee") or ZERO,
            channel_fee=d.get("channel_fee") or ZERO,
            coupon_discount=d.get("coupon_discount") or ZERO,
            points_discount=d.get("points_discount") or ZERO,
        )
    except SplitRule.DoesNotExist:
        return Response({"detail": "分账规则不存在"}, status=status.HTTP_404_NOT_FOUND)
    return Response(result)


# ── 多维财务报表 ────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_by_show(request):
    show_id = request.query_params.get("show_id")
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    result = FinanceReportService.report_by_show(
        show_id=int(show_id) if show_id else None,
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
    )
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_by_performance(request):
    show_id = request.query_params.get("show_id")
    perf_id = request.query_params.get("performance_id")
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    result = FinanceReportService.report_by_performance(
        show_id=int(show_id) if show_id else None,
        performance_id=int(perf_id) if perf_id else None,
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
    )
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_by_channel(request):
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    result = FinanceReportService.report_by_channel(
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
    )
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_by_time(request):
    granularity = request.query_params.get("granularity", "daily")
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    show_id = request.query_params.get("show_id")
    result = FinanceReportService.report_by_time(
        granularity=granularity,
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
        show_id=int(show_id) if show_id else None,
    )
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_by_party(request):
    party_id = request.query_params.get("party_id")
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    result = FinanceReportService.report_by_party(
        party_id=int(party_id) if party_id else None,
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
    )
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def finance_dashboard(request):
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    result = FinanceReportService.finance_dashboard(
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
    )
    return Response(result)

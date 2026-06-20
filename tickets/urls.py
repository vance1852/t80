from django.http import JsonResponse
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BoxOfficeFlowViewSet,
    BoxOfficeSummaryViewSet,
    ChannelViewSet,
    LoginView,
    OrderViewSet,
    PerformanceViewSet,
    ReconciliationViewSet,
    RefundViewSet,
    SettlementFlowViewSet,
    SettlementPartyViewSet,
    SettlementStatementViewSet,
    ShowViewSet,
    SplitDetailViewSet,
    SplitRollbackViewSet,
    SplitRuleViewSet,
    boxoffice_collect,
    dashboard_stats,
    finance_dashboard,
    me,
    report_by_channel,
    report_by_party,
    report_by_performance,
    report_by_show,
    report_by_time,
    resolve_recon_diff,
    split_simulate,
)


def health(_request):
    return JsonResponse({"status": "ok", "service": "show-ticketing-admin"})


router = DefaultRouter(trailing_slash=False)
router.register("shows", ShowViewSet)
router.register("performances", PerformanceViewSet)
router.register("orders", OrderViewSet)

router.register("parties", SettlementPartyViewSet, basename="parties")
router.register("channels", ChannelViewSet, basename="channels")
router.register("refunds", RefundViewSet, basename="refunds")
router.register("split-rules", SplitRuleViewSet, basename="split-rules")
router.register("boxoffice-flows", BoxOfficeFlowViewSet, basename="boxoffice-flows")
router.register("boxoffice-summaries", BoxOfficeSummaryViewSet, basename="boxoffice-summaries")
router.register("split-details", SplitDetailViewSet, basename="split-details")
router.register("split-rollbacks", SplitRollbackViewSet, basename="split-rollbacks")
router.register("statements", SettlementStatementViewSet, basename="statements")
router.register("settlement-flows", SettlementFlowViewSet, basename="settlement-flows")
router.register("reconciliations", ReconciliationViewSet, basename="reconciliations")

urlpatterns = [
    path("health", health),
    path("auth/login", LoginView.as_view()),
    path("auth/me", me),
    path("dashboard/stats", dashboard_stats),

    path("boxoffice/collect", boxoffice_collect),
    path("split/simulate", split_simulate),

    path("reconciliations/diffs/<int:diff_id>/resolve", resolve_recon_diff),

    path("reports/by-show", report_by_show),
    path("reports/by-performance", report_by_performance),
    path("reports/by-channel", report_by_channel),
    path("reports/by-time", report_by_time),
    path("reports/by-party", report_by_party),
    path("finance/dashboard", finance_dashboard),
]

urlpatterns += router.urls

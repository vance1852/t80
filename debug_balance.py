import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from tickets.models import *
from django.db.models import Sum
from decimal import Decimal

print("=" * 80)
print("DETAIL LAYER (sale + refund flows):")
print("=" * 80)
detail_qs = BoxOfficeFlow.objects.filter(flow_type__in=["sale", "refund"])
for f in detail_qs.order_by("id"):
    print(f"  #{f.id:>2} [{f.flow_type:>10}] perf={f.performance_id or '-':>2} ord={f.order_id or '-':>3} "
          f"qty={f.quantity:>4} ticket={float(f.ticket_amount):>10.2f} "
          f"gross={float(f.gross_amount):>10.2f} "
          f"pay_fee={float(f.payment_fee):>8.2f} ch_fee={float(f.channel_fee):>8.2f} "
          f"refund={float(f.refund_amount):>8.2f} "
          f"net={float(f.net_received):>10.2f} "
          f"coupon={float(f.coupon_discount):>8.2f} points={float(f.points_discount):>8.2f}")

print()
da = detail_qs.aggregate(
    ticket=Sum("ticket_amount"), gross=Sum("gross_amount"),
    pay_fee=Sum("payment_fee"), ch_fee=Sum("channel_fee"),
    refund=Sum("refund_amount"), net=Sum("net_received"),
    coupon=Sum("coupon_discount"), points=Sum("points_discount"),
)
print("  detail summary:")
for k, v in da.items():
    print(f"    {k:>10}: {float(v or 0):>12.2f}")

print()
print("=" * 80)
print("SETTLEMENT LAYER (flows + splits):")
print("=" * 80)
for f in BoxOfficeFlow.objects.filter(flow_type="settlement").order_by("id"):
    splits = list(SplitDetail.objects.filter(flow=f))
    split_sum = sum(float(s.split_amount) for s in splits)
    bear_sum = sum(float(s.coupon_bear) + float(s.points_bear) + float(s.refund_bear) for s in splits)
    net_sum = sum(float(s.net_amount) for s in splits)
    print(f"  #{f.id:>2} perf={f.performance_id or '-':>2} "
          f"ticket={float(f.ticket_amount):>10.2f} gross={float(f.gross_amount):>10.2f} "
          f"pay_fee={float(f.payment_fee):>8.2f} ch_fee={float(f.channel_fee):>8.2f} "
          f"refund={float(f.refund_amount):>8.2f} "
          f"net={float(f.net_received):>10.2f} should_split={float(f.should_split_amount):>10.2f} "
          f"|| split_sum={split_sum:>10.2f} bear_sum={bear_sum:>10.2f} net_sum={net_sum:>10.2f}")
    for s in splits:
        print(f"    -> {s.party.party_type:>10}/{s.party.name[:12]:<12} "
              f"split={float(s.split_amount):>10.2f} coupon={float(s.coupon_bear):>8.2f} "
              f"points={float(s.points_bear):>8.2f} refund_b={float(s.refund_bear):>8.2f} "
              f"net={float(s.net_amount):>10.2f}")

print()
print("=" * 80)
print("BALANCE CHECK:")
print("=" * 80)
sa = SplitDetail.objects.aggregate(
    net=Sum("net_amount"), split=Sum("split_amount"),
    cb=Sum("coupon_bear"), pb=Sum("points_bear"), rb=Sum("refund_bear"),
)
bear_sum = float(sa["cb"] or 0) + float(sa["pb"] or 0) + float(sa["rb"] or 0)
net_recv = float(da["net"] or 0)
split_net = float(sa["net"] or 0)
diff = net_recv - split_net - bear_sum

print(f"  明细层 net_received          : {net_recv:>12.2f}")
print(f"  结算层 SplitDetail.net_amount: {split_net:>12.2f}")
print(f"  各项承担合计 (coupon+points+refund): {bear_sum:>12.2f}")
print(f"  差额 (net_recv - split_net - bear)  : {diff:>12.2f}")
print()
print(f"  公式推导：")
print(f"    明细层: net_received = gross - pay_fee - ch_fee = {float(da['gross']):.2f} - {float(da['pay_fee']):.2f} - {float(da['ch_fee']):.2f} = {float(da['gross']) - float(da['pay_fee']) - float(da['ch_fee']):.2f}")
print()
print(f"  按场次明细对比（找出差距来源）：")
perfs = Performance.objects.all()
for p in perfs:
    d_flows = BoxOfficeFlow.objects.filter(performance=p.id, flow_type__in=["sale", "refund"])
    d_agg = d_flows.aggregate(
        gross=Sum("gross_amount"), pay_fee=Sum("payment_fee"), ch_fee=Sum("channel_fee"),
        refund=Sum("refund_amount"), net=Sum("net_received"),
        coupon=Sum("coupon_discount"), points=Sum("points_discount"),
    )
    s_flow = BoxOfficeFlow.objects.filter(performance=p.id, flow_type="settlement").first()
    if s_flow:
        s_splits = SplitDetail.objects.filter(flow=s_flow)
        s_agg = s_splits.aggregate(net=Sum("net_amount"), cb=Sum("coupon_bear"), pb=Sum("points_bear"), rb=Sum("refund_bear"))
        s_bear = float(s_agg["cb"] or 0) + float(s_agg["pb"] or 0) + float(s_agg["rb"] or 0)
        s_net = float(s_agg["net"] or 0)
        d_net = float(d_agg["net"] or 0)
        p_diff = d_net - s_net - s_bear
        print(f"  perf#{p.id} [{p.show.title[:12]} {p.hall[:6]}]: "
              f"detail_net={d_net:>10.2f} settle_net={s_net:>10.2f} "
              f"bear={s_bear:>8.2f} || diff={p_diff:>10.2f}  (should_split={float(s_flow.should_split_amount):>10.2f})")
    else:
        print(f"  perf#{p.id}: no settlement flow")

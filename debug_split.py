import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from tickets.models import *
from tickets.services.split_engine import SplitRuleEngine
from tickets.services.boxoffice_service import BoxOfficeService
from datetime import date
from decimal import Decimal

print('规则总数:', SplitRule.objects.count())
for r in SplitRule.objects.all():
    items = list(r.items.all().values_list(
        'party_id', 'calc_type', 'rate', 'fixed_amount', 'priority'
    ))
    print(f'  rule={r.name} show={r.show_id} status={r.status} '
          f'eff_from={r.effective_from} eff_to={r.effective_to} '
          f'items={items}')

print()
print('演出:')
for s in Show.objects.all():
    print(f'  show_id={s.id} name={s.name} status={s.status}')

print()
print('场次:')
for p in Performance.objects.all():
    print(f'  perf_id={p.id} show_id={p.show_id} show={p.show.name} time={p.time}')

print()
print('尝试规则匹配 (biz=2026-06-20):')
for s in Show.objects.filter(status='running'):
    biz = date(2026, 6, 20)
    rules = SplitRuleEngine.find_rules(s, biz)
    print(f'  show={s.name} biz={biz} matched_rules={len(rules)}')
    for r in rules:
        print(f'    - {r.name}')

print()
print('BoxOfficeFlow 统计:')
for t in ['sale', 'refund', 'settlement']:
    cnt = BoxOfficeFlow.objects.filter(flow_type=t).count()
    agg = BoxOfficeFlow.objects.filter(flow_type=t).aggregate(
        gross=Sum('gross_amount'), net=Sum('net_received'),
        should=Sum('should_split_amount'),
    )
    print(f'  {t}: count={cnt} gross={agg["gross"]} net={agg["net"]} should={agg["should"]}')

print()
print('SplitDetail 统计:')
for p in SettlementParty.objects.all():
    agg = SplitDetail.objects.filter(party=p).aggregate(
        split=Sum('split_amount'), rollback=Sum('rollback_amount'),
        coupon=Sum('coupon_bear'), points=Sum('points_bear'),
        refund=Sum('refund_bear'), net=Sum('net_amount'),
    )
    print(f'  {p.name}({p.party_type}): split={agg["split"]} rollback={agg["rollback"]} '
          f'coupon={agg["coupon"]} points={agg["points"]} refund={agg["refund"]} net={agg["net"]}')

print()
print('场次 1 的 BoxOfficeFlow:')
for f in BoxOfficeFlow.objects.filter(performance_id=1):
    print(f'  id={f.id} type={f.flow_type} order={f.order_id} biz_date={f.biz_date} '
          f'gross={f.gross_amount} net={f.net_received} should={f.should_split_amount} '
          f'is_settled={f.is_settled}')

print()
print('场次 1 的 SplitDetail 数:', SplitDetail.objects.filter(flow__performance_id=1).count())
print('场次 1 的 settlement 类型 flow 数:', BoxOfficeFlow.objects.filter(performance_id=1, flow_type='settlement').count())

print()
print('直接调用 SplitRuleEngine.calculate 测试（用第1条规则）:')
rule = SplitRule.objects.first()
print(f'  选中规则: {rule.name} show_id={rule.show_id} show={rule.show.name if rule.show else "GLOBAL"}')
if rule:
    from tickets.services.split_engine import SplitInput
    inp = SplitInput(
        ticket_amount=Decimal('100000.00'),
        quantity=100,
        coupon_discount=Decimal('500.00'),
        points_discount=Decimal('200.00'),
        refund_amount=Decimal('0.00'),
        payment_fee=Decimal('100.00'),
        channel_fee=Decimal('8000.00'),
    )
    result = SplitRuleEngine.calculate(rule, inp)
    print(f'  输入: ticket_amount=100000')
    for r in result.results:
        print(f'    {r.party_name}: split={r.split_amount} coupon={r.coupon_bear} points={r.points_bear} refund={r.refund_bear} net={r.net_amount}')

print()
print('重新计算场次 1 结算:')
try:
    flow, splits = BoxOfficeService.settle_performance(1)
    if flow:
        print(f'  settlement_flow: id={flow.id} gross={flow.gross_amount} '
              f'net={flow.net_received} should={flow.should_split_amount}')
        for s in splits:
            print(f'    - {s.party.name}: split={s.split_amount} net={s.net_amount}')
    else:
        print('  没有返回 flow')
except Exception as e:
    import traceback
    traceback.print_exc()

"""初始化内置管理员与种子业务数据（幂等）。"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from tickets.models import (
    BoxOfficeFlow,
    Channel,
    Performance,
    SettlementParty,
    Show,
    SplitRule,
    SplitRuleItem,
    TicketOrder,
)
from tickets.services.boxoffice_service import BoxOfficeService
from tickets.services.split_engine import ZERO


class Command(BaseCommand):
    help = "初始化管理员与演出票务种子数据"

    def handle(self, *args, **options):
        username = settings.DEFAULT_ADMIN_USERNAME
        password = settings.DEFAULT_ADMIN_PASSWORD
        if not User.objects.filter(username=username).exists():
            User.objects.create_superuser(username=username, password=password, first_name="平台管理员")
            self.stdout.write("已创建管理员账号")

        data_exists = Show.objects.exists() and SettlementParty.objects.exists()
        if data_exists and SplitRule.objects.exists():
            self.stdout.write("业务数据已存在，跳过")
            return

        shows = list(Show.objects.all())
        if not shows:
            shows = [
                Show.objects.create(title="星河巡回演唱会", troupe="星河乐团", genre="concert", status="on_sale"),
                Show.objects.create(title="金陵往事话剧", troupe="城南剧社", genre="drama", status="on_sale"),
                Show.objects.create(title="敦煌音乐剧", troupe="丝路艺术团", genre="musical", status="upcoming"),
                Show.objects.create(title="经典戏曲专场", troupe="梨园名家", genre="opera", status="ended"),
            ]
            self.stdout.write("已创建演出剧目")

        perfs = list(Performance.objects.all())
        if not perfs:
            now = datetime.now().replace(microsecond=0)
            perfs = [
                Performance.objects.create(show=shows[0], hall="一号厅", start_at=now + timedelta(days=3), total_seats=1200, sold_seats=0, price=380),
                Performance.objects.create(show=shows[0], hall="一号厅", start_at=now + timedelta(days=4), total_seats=1200, sold_seats=0, price=380),
                Performance.objects.create(show=shows[1], hall="小剧场", start_at=now + timedelta(days=2), total_seats=300, sold_seats=0, price=180),
                Performance.objects.create(show=shows[2], hall="大剧院", start_at=now + timedelta(days=20), total_seats=900, sold_seats=0, price=280),
                Performance.objects.create(show=shows[3], hall="戏曲厅", start_at=now - timedelta(days=10), total_seats=500, sold_seats=0, price=120),
            ]
            self.stdout.write("已创建场次")

        parties = list(SettlementParty.objects.all())
        if not parties:
            parties = [
                SettlementParty.objects.create(
                    name="星河文化传播有限公司", party_type="organizer",
                    contact="王经理", phone="13800000001",
                    bank_account="6222020000000001", bank_name="工商银行北京分行",
                ),
                SettlementParty.objects.create(
                    name="城南文化艺术中心", party_type="venue",
                    contact="李主任", phone="13800000002",
                    bank_account="6222020000000002", bank_name="建设银行南京分行",
                ),
                SettlementParty.objects.create(
                    name="票务在线科技有限公司", party_type="channel",
                    contact="张总监", phone="13800000003",
                    bank_account="6222020000000003", bank_name="招商银行上海分行",
                ),
                SettlementParty.objects.create(
                    name="大麦网", party_type="channel",
                    contact="赵经理", phone="13800000004",
                    bank_account="6222020000000004", bank_name="农业银行上海分行",
                ),
                SettlementParty.objects.create(
                    name="本平台运营方", party_type="platform",
                    contact="系统", phone="13800000000",
                    bank_account="6222020000000000", bank_name="平台账户",
                ),
                SettlementParty.objects.create(
                    name="国家税务总局", party_type="tax",
                    contact="税务局", phone="12366",
                    bank_account="TAX0000000000000", bank_name="国库",
                ),
            ]
            self.stdout.write("已创建结算方")

        organizer_p = parties[0]
        venue_p = parties[1]
        ch1_p = parties[2]
        ch2_p = parties[3]
        platform_p = parties[4]
        tax_p = parties[5]

        channels = list(Channel.objects.all())
        if not channels:
            channels = [
                Channel.objects.create(name="官方直销", code="OFFICIAL", default_commission_rate=0, party=platform_p),
                Channel.objects.create(name="票务在线", code="PWZX", default_commission_rate=Decimal("0.08"), party=ch1_p),
                Channel.objects.create(name="大麦网", code="DAMAI", default_commission_rate=Decimal("0.10"), party=ch2_p),
            ]
            self.stdout.write("已创建渠道")

        rules = list(SplitRule.objects.all())
        if not rules:
            rule_concert = SplitRule.objects.create(
                name="星河演唱会-标准分账", scope_type="show",
                show=shows[0], status="active",
                tax_rate=Decimal("0.06"), tax_priority=1,
                remark="演唱会标准分账方案：场地先抽固定租金+10%，平台5%，渠道按约定，剩余归主办方",
            )
            SplitRuleItem.objects.bulk_create([
                SplitRuleItem(
                    rule=rule_concert, party=tax_p,
                    calc_type="rate", rate=Decimal("0.06"),
                    priority=1, calc_base="net_after_refund",
                    coupon_bearer_type="platform", points_bearer_type="platform",
                    refund_bearer_type="share",
                ),
                SplitRuleItem(
                    rule=rule_concert, party=venue_p,
                    calc_type="fixed", fixed_amount=Decimal("50000.00"),
                    priority=2, calc_base="remaining",
                    min_amount=Decimal("50000.00"),
                ),
                SplitRuleItem(
                    rule=rule_concert, party=venue_p,
                    calc_type="rate", rate=Decimal("0.10"),
                    priority=3, calc_base="net_after_tax",
                ),
                SplitRuleItem(
                    rule=rule_concert, party=platform_p,
                    calc_type="rate", rate=Decimal("0.05"),
                    priority=4, calc_base="remaining",
                ),
                SplitRuleItem(
                    rule=rule_concert, party=organizer_p,
                    calc_type="remaining",
                    priority=99, calc_base="remaining",
                ),
            ])

            rule_drama = SplitRule.objects.create(
                name="金陵往事话剧-小剧场分账", scope_type="show",
                show=shows[1], status="active",
                tax_rate=Decimal("0.06"), tax_priority=1,
                remark="话剧小场地方案：场地抽20%，平台3%，主办方剩余",
            )
            SplitRuleItem.objects.bulk_create([
                SplitRuleItem(
                    rule=rule_drama, party=tax_p,
                    calc_type="rate", rate=Decimal("0.06"),
                    priority=1, calc_base="net_after_refund",
                ),
                SplitRuleItem(
                    rule=rule_drama, party=venue_p,
                    calc_type="rate", rate=Decimal("0.20"),
                    priority=2, calc_base="net_after_tax",
                ),
                SplitRuleItem(
                    rule=rule_drama, party=platform_p,
                    calc_type="rate", rate=Decimal("0.03"),
                    priority=3, calc_base="remaining",
                ),
                SplitRuleItem(
                    rule=rule_drama, party=organizer_p,
                    calc_type="remaining",
                    priority=99, calc_base="remaining",
                ),
            ])

            rule_musical = SplitRule.objects.create(
                name="敦煌音乐剧-待开票规则", scope_type="show",
                show=shows[2], status="draft",
                tax_rate=Decimal("0.06"),
                remark="待开票演出占位规则",
            )

            rule_opera = SplitRule.objects.create(
                name="经典戏曲专场-已结项", scope_type="show",
                show=shows[3], status="inactive",
                tax_rate=Decimal("0.03"),
                remark="戏曲低税率",
            )
            SplitRuleItem.objects.bulk_create([
                SplitRuleItem(
                    rule=rule_opera, party=tax_p,
                    calc_type="rate", rate=Decimal("0.03"), priority=1,
                ),
                SplitRuleItem(
                    rule=rule_opera, party=venue_p,
                    calc_type="fixed", fixed_amount=Decimal("5000"), priority=2,
                ),
                SplitRuleItem(
                    rule=rule_opera, party=organizer_p,
                    calc_type="remaining", priority=99,
                ),
            ])

            rule_perf = SplitRule.objects.create(
                name="星河演唱会-加开场次特惠", scope_type="performance",
                performance=perfs[1], status="active",
                tax_rate=Decimal("0.06"),
                remark="加开场次降低平台分成",
            )
            SplitRuleItem.objects.bulk_create([
                SplitRuleItem(
                    rule=rule_perf, party=tax_p,
                    calc_type="rate", rate=Decimal("0.06"), priority=1, calc_base="net_after_refund",
                ),
                SplitRuleItem(
                    rule=rule_perf, party=venue_p,
                    calc_type="fixed", fixed_amount=Decimal("30000.00"), priority=2,
                ),
                SplitRuleItem(
                    rule=rule_perf, party=venue_p,
                    calc_type="rate", rate=Decimal("0.08"), priority=3, calc_base="net_after_tax",
                ),
                SplitRuleItem(
                    rule=rule_perf, party=platform_p,
                    calc_type="rate", rate=Decimal("0.03"), priority=4,
                ),
                SplitRuleItem(
                    rule=rule_perf, party=organizer_p,
                    calc_type="remaining", priority=99,
                ),
            ])

            self.stdout.write("已创建分账规则")

        if not TicketOrder.objects.filter(order_no__startswith="T20").exists():
            now = datetime.now().replace(microsecond=0)
            ch_off = channels[0]
            ch_pwzx = channels[1]
            ch_damai = channels[2]

            orders_data = [
                {"perf": perfs[0], "ch": ch_off, "name": "陈静", "phone": "13900001111", "qty": 2,
                 "price": 380, "coupon": 0, "points": 0, "pay_fee": Decimal("1.50"), "ch_fee": 0, "status": "paid"},
                {"perf": perfs[0], "ch": ch_pwzx, "name": "李明", "phone": "13900002222", "qty": 3,
                 "price": 380, "coupon": Decimal("50.00"), "points": Decimal("20.00"), "pay_fee": Decimal("2.50"),
                 "ch_fee": Decimal("88.80"), "status": "paid"},
                {"perf": perfs[0], "ch": ch_damai, "name": "王芳", "phone": "13900003333", "qty": 5,
                 "price": 380, "coupon": Decimal("100.00"), "points": 0, "pay_fee": Decimal("3.80"),
                 "ch_fee": Decimal("180.00"), "status": "paid"},
                {"perf": perfs[0], "ch": ch_off, "name": "张伟", "phone": "13900004444", "qty": 1,
                 "price": 380, "coupon": 0, "points": 0, "pay_fee": Decimal("0.60"), "ch_fee": 0, "status": "paid"},
                {"perf": perfs[0], "ch": ch_off, "name": "孙琳", "phone": "13900005555", "qty": 1,
                 "price": 380, "coupon": 0, "points": 0, "pay_fee": Decimal("0.60"), "ch_fee": 0, "status": "paid"},
                {"perf": perfs[0], "ch": ch_pwzx, "name": "周强", "phone": "13900006666", "qty": 4,
                 "price": 380, "coupon": 0, "points": Decimal("50.00"), "pay_fee": Decimal("2.80"),
                 "ch_fee": Decimal("116.40"), "status": "paid"},
                {"perf": perfs[0], "ch": ch_damai, "name": "吴敏", "phone": "13900007777", "qty": 2,
                 "price": 380, "coupon": Decimal("30.00"), "points": 0, "pay_fee": Decimal("1.50"),
                 "ch_fee": Decimal("73.00"), "status": "paid"},
                {"perf": perfs[1], "ch": ch_off, "name": "郑华", "phone": "13900008888", "qty": 3,
                 "price": 380, "coupon": 0, "points": 0, "pay_fee": Decimal("2.00"), "ch_fee": 0, "status": "paid"},
                {"perf": perfs[1], "ch": ch_pwzx, "name": "冯刚", "phone": "13900009999", "qty": 6,
                 "price": 380, "coupon": Decimal("200.00"), "points": Decimal("80.00"), "pay_fee": Decimal("5.00"),
                 "ch_fee": Decimal("195.60"), "status": "paid"},
                {"perf": perfs[1], "ch": ch_damai, "name": "陈晓", "phone": "13900010000", "qty": 2,
                 "price": 380, "coupon": 0, "points": 0, "pay_fee": Decimal("1.50"),
                 "ch_fee": Decimal("76.00"), "status": "paid"},
                {"perf": perfs[2], "ch": ch_off, "name": "刘洋", "phone": "13900011111", "qty": 4,
                 "price": 180, "coupon": 0, "points": 0, "pay_fee": Decimal("2.00"), "ch_fee": 0, "status": "paid"},
                {"perf": perfs[2], "ch": ch_pwzx, "name": "赵磊", "phone": "13900012222", "qty": 2,
                 "price": 180, "coupon": Decimal("20.00"), "points": Decimal("10.00"), "pay_fee": Decimal("1.00"),
                 "ch_fee": Decimal("27.60"), "status": "paid"},
                {"perf": perfs[2], "ch": ch_off, "name": "黄敏", "phone": "13900013333", "qty": 6,
                 "price": 180, "coupon": Decimal("100.00"), "points": 0, "pay_fee": Decimal("3.00"),
                 "ch_fee": 0, "status": "paid"},
                {"perf": perfs[4], "ch": ch_off, "name": "林芳", "phone": "13900014444", "qty": 3,
                 "price": 120, "coupon": 0, "points": 0, "pay_fee": Decimal("1.00"), "ch_fee": 0, "status": "paid"},
                {"perf": perfs[4], "ch": ch_off, "name": "徐军", "phone": "13900015555", "qty": 5,
                 "price": 120, "coupon": Decimal("50.00"), "points": 0, "pay_fee": Decimal("1.80"),
                 "ch_fee": 0, "status": "paid"},
            ]

            created_orders = []
            for d in orders_data:
                orig = Decimal(str(d["qty"] * d["price"]))
                cpn = Decimal(str(d["coupon"])) if d["coupon"] else ZERO
                pts = Decimal(str(d["points"])) if d["points"] else ZERO
                amt = orig - cpn - pts
                pay = Decimal(str(d["pay_fee"]))
                chf = Decimal(str(d["ch_fee"]))
                order = TicketOrder.objects.create(
                    performance=d["perf"],
                    channel=d["ch"],
                    customer_name=d["name"],
                    phone=d["phone"],
                    quantity=d["qty"],
                    original_amount=orig,
                    coupon_discount=cpn,
                    points_discount=pts,
                    amount=amt,
                    paid_amount=amt,
                    payment_fee=pay,
                    channel_fee=chf,
                    coupon_bearer_party=platform_p if cpn > 0 else None,
                    points_bearer_party=platform_p if pts > 0 else None,
                    refunded_amount=ZERO,
                    status=d["status"],
                )
                created_orders.append(order)
                d["perf"].sold_seats += d["qty"]
                d["perf"].save(update_fields=["sold_seats"])
            self.stdout.write(f"已创建 {len(created_orders)} 条订单")

            refund_candidates = [
                (created_orders[4], Decimal("380.00"), 1, Decimal("5.00"), "用户个人原因"),
                (created_orders[6], Decimal("350.00"), 1, Decimal("3.00"), "演出时间变更"),
                (created_orders[9], Decimal("380.00"), 1, Decimal("2.00"), "重复下单"),
                (created_orders[12], Decimal("480.00"), 3, Decimal("3.00"), "行程冲突"),
            ]
            for order, amt, qty, fee, reason in refund_candidates:
                try:
                    BoxOfficeService.process_refund(
                        order=order, refund_amount=amt, refund_quantity=qty,
                        refund_fee=fee, reason=reason, operator="system",
                    )
                except Exception as e:
                    self.stdout.write(f"退款失败: {e}")

            self.stdout.write(f"已处理 {len(refund_candidates)} 条退款")

        if not BoxOfficeFlow.objects.exists():
            result = BoxOfficeService.collect_all_orders()
            self.stdout.write(f"票房归集完成: {result}")

        self.stdout.write("种子数据初始化完成")

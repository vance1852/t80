import requests
import json

BASE = "http://127.0.0.1:7652/api"

def login():
    r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]

def h(token):
    return {"Authorization": f"Bearer {token}"}

def p(title, r, show_body=True):
    print(f"\n=== {title} ===")
    print(f"  Status: {r.status_code}")
    if show_body:
        try:
            data = r.json()
            print(f"  Body: {json.dumps(data, ensure_ascii=False, indent=2)[:800]}")
        except:
            print(f"  Body: {r.text[:600]}")
    return r

def get_list(r):
    """统一处理 list 和 paginated 两种返回格式。"""
    data = r.json()
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data

token = login()
print(f"Token: {token[:30]}...")

# ============================================================
# 1. 退款接口测试
# ============================================================
print("\n" + "=" * 80)
print("TEST 1: 退款接口")
print("=" * 80)

# 先获取一个订单
r = requests.get(f"{BASE}/orders", headers=h(token))
orders = get_list(r)
order = orders[0]
order_id = order["id"]
order_amount = float(order["amount"])
order_refunded = float(order.get("refunded_amount", 0))
order_perf_id = order.get("performance")
if isinstance(order_perf_id, dict):
    order_perf_id = order_perf_id.get("id")
print(f"\n选中订单 #{order_id}, amount={order_amount}, refunded={order_refunded}, perf={order_perf_id}")

# 1a. 成功退款
print("\n--- 1a. 正常退款（金额=380, 数量=1）---")
r = requests.post(f"{BASE}/refunds", json={
    "order": order_id,
    "refund_amount": 380.00,
    "refund_quantity": 1,
    "reason": "测试正常退款",
    "operator": "test_admin",
}, headers=h(token))
p("POST /refunds 成功退款", r)
assert r.status_code == 201, f"退款应该返回 201, 实际 {r.status_code}"
refund_id = r.json()["id"]
print(f"  ✅ 退款成功，refund_id={refund_id}")

# 验证订单退款金额已更新
r = requests.get(f"{BASE}/orders/{order_id}", headers=h(token))
new_refunded = float(r.json().get("refunded_amount", 0))
print(f"  订单 refunded_amount: {order_refunded} -> {new_refunded}")
assert new_refunded > order_refunded, "退款后订单退款金额应该增加"
print("  ✅ 订单退款金额已更新")

# 验证场次 sold_seats 减少
if order_perf_id:
    r = requests.get(f"{BASE}/performances/{order_perf_id}", headers=h(token))
    print(f"  场次 sold_seats = {r.json().get('sold_seats')}")

# 1b. 失败退款（金额过大）- 验证事务回滚
print("\n--- 1b. 失败退款（金额超过可退）- 验证事务回滚 ---")
before_refund_list = get_list(requests.get(f"{BASE}/refunds", headers=h(token)))
before_refund_count = len(before_refund_list)
before_order_refunded = new_refunded

r = requests.post(f"{BASE}/refunds", json={
    "order": order_id,
    "refund_amount": 999999.00,  # 远超可退金额
    "refund_quantity": 1000,
    "reason": "测试失败退款",
    "operator": "test_admin",
}, headers=h(token))
p("POST /refunds 失败退款", r)
assert r.status_code == 400, f"失败退款应该返回 400, 实际 {r.status_code}"
print(f"  ✅ 返回 400，错误信息: {r.json()['detail']}")

# 验证事务回滚 - 退款记录没有增加
after_refund_list = get_list(requests.get(f"{BASE}/refunds", headers=h(token)))
after_refund_count = len(after_refund_list)
print(f"  退款记录数: 之前={before_refund_count}, 之后={after_refund_count}")
assert after_refund_count == before_refund_count, "失败退款不应该创建新的退款记录"
print("  ✅ 退款记录数未增加（事务回滚成功）")

# 验证订单退款金额没变
r = requests.get(f"{BASE}/orders/{order_id}", headers=h(token))
after_order_refunded = float(r.json().get("refunded_amount", 0))
print(f"  订单 refunded_amount: {before_order_refunded} -> {after_order_refunded}")
assert abs(after_order_refunded - before_order_refunded) < 0.01, "失败退款不应该修改订单"
print("  ✅ 订单退款金额未变（事务回滚成功）")

# ============================================================
# 2. 结算流水 + 确认 测试
# ============================================================
print("\n" + "=" * 80)
print("TEST 2: 结算流水创建 + 确认")
print("=" * 80)

# 2a. 手动创建结算流水
print("\n--- 2a. 创建结算流水（pending）---")
r = requests.post(f"{BASE}/settlement-flows", json={
    "party_id": 2,  # 场地
    "flow_type": "payout",
    "amount": 10000.00,
    "bank_transfer_no": "TEST-TRANSFER-001",
    "operator": "test_admin",
    "remark": "测试打款流水",
}, headers=h(token))
p("POST /settlement-flows 创建流水", r)
assert r.status_code == 201, f"创建结算流水应该返回 201, 实际 {r.status_code}"
flow_id = r.json()["id"]
flow_status = r.json()["status"]
print(f"  ✅ 结算流水创建成功，flow_id={flow_id}, status={flow_status}")
assert flow_status == "pending", f"新创建的流水状态应为 pending，实际 {flow_status}"

# 2b. 确认结算流水
print("\n--- 2b. 确认结算流水（pending -> completed）---")
r = requests.post(f"{BASE}/settlement-flows/{flow_id}/confirm", headers=h(token))
p(f"POST /settlement-flows/{flow_id}/confirm", r)
assert r.status_code == 200, f"确认结算流水应该返回 200, 实际 {r.status_code}"
confirmed_status = r.json()["status"]
print(f"  ✅ 结算流水确认成功，status={confirmed_status}")
assert confirmed_status == "completed", f"确认后状态应为 completed，实际 {confirmed_status}"

# 2c. 重复确认应该失败
print("\n--- 2c. 重复确认应该失败 ---")
r = requests.post(f"{BASE}/settlement-flows/{flow_id}/confirm", headers=h(token))
p(f"POST /settlement-flows/{flow_id}/confirm 重复确认", r)
assert r.status_code == 400, f"重复确认应该返回 400, 实际 {r.status_code}"
print(f"  ✅ 重复确认返回 400，错误信息: {r.json()['detail']}")

# 2d. 不存在的流水确认应该 400（不是 500）
print("\n--- 2d. 确认不存在的流水应该 400（不是 500）---")
r = requests.post(f"{BASE}/settlement-flows/99999/confirm", headers=h(token))
p(f"POST /settlement-flows/99999/confirm 不存在", r)
assert r.status_code == 400, f"不存在的流水应该返回 400, 实际 {r.status_code}"
print(f"  ✅ 不存在流水返回 400，错误信息: {r.json()['detail']}")

# ============================================================
# 3. 最终对账验证（退款后仍平账）
# ============================================================
print("\n" + "=" * 80)
print("TEST 3: 退款后对账验证（仍应平账）")
print("=" * 80)

r = requests.post(f"{BASE}/reconciliations/check", json={"recon_type": "all"}, headers=h(token))
p("POST /reconciliations/check", r)
assert r.status_code == 200
result = r.json()
print(f"  is_balanced = {result['is_balanced']}")
print(f"  difference  = {result['difference']}")
print(f"  net_received = {result['net_received']}")
print(f"  split_net    = {result['split_net']}")
assert result["is_balanced"], f"退款后仍应平账，实际 difference={result['difference']}"
print("  ✅ 退款后对账仍然平账")

print("\n" + "=" * 80)
print("ALL TESTS PASSED! ✅")
print("=" * 80)

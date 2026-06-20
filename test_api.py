import requests
import json

BASE = "http://localhost:7652/api"

def test(method, url, body=None, headers=None):
    full_url = f"{BASE}{url}"
    print(f"\n=== {method.upper()} {url} ===")
    try:
        if method.upper() == "GET":
            resp = requests.get(full_url, headers=headers, timeout=15)
        else:
            resp = requests.post(full_url, json=body, headers=headers, timeout=15)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
            if len(json.dumps(data, ensure_ascii=False)) > 1500:
                print("... (truncated)")
        else:
            print(f"Response: {resp.text[:500]}")
    except Exception as e:
        print(f"ERROR: {e}")

# 1. 健康检查
print("=" * 60)
print("1. HEALTH CHECK (no auth)")
print("=" * 60)
test("GET", "/health")

# 2. 登录
print("\n" + "=" * 60)
print("2. LOGIN")
print("=" * 60)
login_resp = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"}, timeout=15)
print(f"Status: {login_resp.status_code}")
login_data = login_resp.json()
print(f"Login response keys: {list(login_data.keys())}")
token = login_data.get("access_token") or login_data.get("access")
if not token:
    print("ERROR: No token! Full response:", json.dumps(login_data, ensure_ascii=False))
    exit(1)
print(f"Token first 30 chars: {token[:30]}...")
headers = {"Authorization": f"Bearer {token}"}

# 3. 核心 CRUD 列表（验证接口存在）
print("\n" + "=" * 60)
print("3. CORE ENDPOINTS (GET lists, first page)")
print("=" * 60)
for name, url in [
    ("Shows", "/shows"),
    ("Parties", "/parties"),
    ("Split Rules", "/split-rules"),
    ("Statements", "/statements"),
    ("Reconciliations", "/reconciliations"),
    ("BoxOffice Flows", "/boxoffice-flows?page_size=3"),
    ("Split Details", "/split-details?page_size=3"),
]:
    test("GET", url, headers=headers)

# 4. 动作端点
print("\n" + "=" * 60)
print("4. ACTION ENDPOINTS (POST)")
print("=" * 60)
test("POST", "/reconciliations/check", {"scope": "all"}, headers)

test("POST", "/split/simulate", {
    "rule_id": 1,
    "gross_amount": 100000.00,
    "refund_amount": 0,
    "payment_fee": 100.00,
    "channel_fee": 2000.00,
    "coupon_discount": 500.00,
    "points_discount": 200.00,
}, headers)

test("POST", "/statements/generate", {
    "period_start": "2026-01-01",
    "period_end": "2026-12-31",
}, headers)

test("POST", "/reconciliations/run", {"scope": "all"}, headers)

# 5. 再次对账检查
print("\n" + "=" * 60)
print("5. POST-ACTION BALANCE CHECK")
print("=" * 60)
test("POST", "/reconciliations/check", {"scope": "all"}, headers)

# 6. 报表和 Dashboard
print("\n" + "=" * 60)
print("6. REPORTS AND DASHBOARD")
print("=" * 60)
for name, url in [
    ("By Show", "/reports/by-show"),
    ("By Party", "/reports/by-party"),
    ("By Channel", "/reports/by-channel"),
    ("By Time (monthly)", "/reports/by-time?granularity=monthly"),
    ("Dashboard", "/finance/dashboard"),
]:
    test("GET", url, headers=headers)

print("\n" + "=" * 60)
print("ALL TESTS COMPLETED")
print("=" * 60)

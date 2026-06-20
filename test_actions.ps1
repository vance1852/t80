$BASE = "http://localhost:7652/api/v1"

# 登录
Write-Host "=== 登录 ===" -ForegroundColor Cyan
$loginBody = @{ username = "admin"; password = "admin123" } | ConvertTo-Json
$loginResp = Invoke-RestMethod -Uri "$BASE/auth/login/" -Method Post -Body $loginBody -ContentType "application/json"
$token = $loginResp.access
Write-Host "Token 获取成功"

$headers = @{ Authorization = "Bearer $token" }

function Test-Post($name, $url, $body) {
    Write-Host ""
    Write-Host "=== POST $url  [$name] ===" -ForegroundColor Yellow
    $jsonBody = $body | ConvertTo-Json -Depth 5
    try {
        $resp = Invoke-RestMethod -Uri "$BASE$url" -Method Post -Body $jsonBody -Headers $headers -ContentType "application/json"
        Write-Host "Status: 200" -ForegroundColor Green
        Write-Host ($resp | ConvertTo-Json -Depth 10)
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        if ($_.Exception.Response) {
            try {
                $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                Write-Host $sr.ReadToEnd()
            } catch {}
        }
    }
}

# 1. 平账检查
Test-Post "平账检查" "/reconciliations/check" @{ scope = "all" }

# 2. 分账模拟（10万票房）
Test-Post "分账模拟10万" "/split/simulate" @{
    rule_id = 1
    gross_amount = 100000.00
    refund_amount = 0
    payment_fee = 100.00
    channel_fee = 2000.00
    coupon_discount = 500.00
    points_discount = 200.00
}

# 3. 生成结算单
Test-Post "生成结算单（全年）" "/statements/generate" @{
    period_start = "2026-01-01"
    period_end = "2026-12-31"
}

# 4. 对账
Test-Post "执行对账" "/reconciliations/run" @{ scope = "all" }

# 5. 再次查询平账检查
Test-Post "对账后再次平账检查" "/reconciliations/check" @{ scope = "all" }

# 6. 查询结算单列表（生成后）
Write-Host ""
Write-Host "=== GET /statements  [生成结算单后查询] ===" -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "$BASE/statements" -Method Get -Headers $headers
    Write-Host "Status: 200" -ForegroundColor Green
    Write-Host ($resp | ConvertTo-Json -Depth 10)
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "====== ALL KEY TESTS DONE ======" -ForegroundColor Cyan

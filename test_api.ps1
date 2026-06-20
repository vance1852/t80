$Base = "http://127.0.0.1:7652/api"
$Token = $null
$Headers = @{}

function Test-Endpoint($name, $method, $path, $body = $null) {
    Write-Host "=== $method $path  [$name] ===" -ForegroundColor Cyan
    try {
        $params = @{
            Uri = "$Base$path"
            Method = $method
            UseBasicParsing = $true
            Headers = $Headers
            ContentType = "application/json"
            ErrorAction = "Stop"
        }
        if ($body) { $params["Body"] = ($body | ConvertTo-Json -Depth 10 -Compress) }
        $resp = Invoke-WebRequest @params
        $content = $resp.Content
        if ($content.Length -gt 1200) {
            $obj = $content | ConvertFrom-Json
            $count = 0
            if ($obj.results) { $count = $obj.results.Count }
            elseif ($obj -is [array]) { $count = $obj.Count }
            elseif ($obj.data -is [array]) { $count = $obj.data.Count }
            Write-Host "Status: $($resp.StatusCode) | Response (truncated, items=$($count)): "
            Write-Host $content.Substring(0, [Math]::Min(1000, $content.Length))
        } else {
            Write-Host "Status: $($resp.StatusCode)"
            Write-Host $content
        }
        return $content
    } catch {
        $errResp = $_.Exception.Response
        $errBody = ""
        if ($errResp) {
            try {
                $reader = New-Object System.IO.StreamReader($errResp.GetResponseStream())
                $reader.BaseStream.Position = 0
                $reader.DiscardBufferedData()
                $errBody = $reader.ReadToEnd()
            } catch {}
        }
        Write-Host "ERROR: $_" -ForegroundColor Red
        if ($errBody) { Write-Host "Response: $errBody" -ForegroundColor Red }
        return $null
    }
    Write-Host ""
}

# 1. Health check
Test-Endpoint "HealthCheck" GET "/health"

# 2. Login
Write-Host ""
Write-Host "=== Login ===" -ForegroundColor Cyan
$loginBody = @{ username = "admin"; password = "admin123" }
$loginResp = Test-Endpoint "Login" POST "/auth/login" $loginBody
if ($loginResp) {
    $loginObj = $loginResp | ConvertFrom-Json
    if ($loginObj.access_token) {
        $Token = $loginObj.access_token
    } elseif ($loginObj.access) {
        $Token = $loginObj.access
    }
    if ($Token) {
        $Headers["Authorization"] = "Bearer $Token"
        Write-Host "Token obtained (first 30 chars): $($Token.Substring(0, [Math]::Min(30, $Token.Length)))..." -ForegroundColor Green
    } else {
        Write-Host "ERROR: Token missing in login response: $loginResp" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
# 3. Core business endpoints
Test-Endpoint "Shows list" GET "/shows"
Test-Endpoint "Performances list" GET "/performances"
Test-Endpoint "Orders list" GET "/orders?page_size=5"
Test-Endpoint "Settlement parties" GET "/parties"
Test-Endpoint "Channels" GET "/channels"
Test-Endpoint "Split rules" GET "/split-rules"
Test-Endpoint "BoxOffice flows" GET "/boxoffice-flows?page_size=5"
Test-Endpoint "BoxOffice summaries" GET "/boxoffice-summaries"
Test-Endpoint "Split details" GET "/split-details?page_size=5"
Test-Endpoint "Split rollbacks" GET "/split-rollbacks"
Test-Endpoint "Settlement statements" GET "/statements"
Test-Endpoint "Settlement flows" GET "/settlement-flows"
Test-Endpoint "Reconciliation list" GET "/reconciliations"

Write-Host ""
Write-Host "=== Action endpoints ===" -ForegroundColor Yellow
# 4. Reconciliation check
Test-Endpoint "Recon check (平账检查)" POST "/reconciliations/check" @{ scope = "all" }

# 5. Split simulate
Test-Endpoint "Split simulate (分账模拟10万)" POST "/split/simulate" @{
    rule_id = 1
    gross_amount = 100000.00
    refund_amount = 0
    payment_fee = 100.00
    channel_fee = 2000.00
    coupon_discount = 500.00
    points_discount = 200.00
}

# 6. Generate statements
Test-Endpoint "Generate statements (生成结算单)" POST "/statements/generate" @{
    period_start = "2026-01-01"
    period_end = "2026-12-31"
}

# 7. Run reconciliation
Test-Endpoint "Run reconciliation (对账)" POST "/reconciliations/run" @{ scope = "all" }

Write-Host ""
Write-Host "=== Report endpoints ===" -ForegroundColor Yellow
Test-Endpoint "Report by show" GET "/reports/by-show"
Test-Endpoint "Report by performance" GET "/reports/by-performance"
Test-Endpoint "Report by channel" GET "/reports/by-channel"
Test-Endpoint "Report by time (monthly)" GET "/reports/by-time?granularity=monthly"
Test-Endpoint "Report by party" GET "/reports/by-party"
Test-Endpoint "Finance dashboard" GET "/finance/dashboard"

Write-Host ""
Write-Host "====== ALL TESTS DONE ======" -ForegroundColor Green

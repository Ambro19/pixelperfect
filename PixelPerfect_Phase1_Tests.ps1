# =============================================================================
# PixelPerfect Screenshot API — Phase 1 Testing Script
# Custom JavaScript Execution + Device Emulation (Pro+)
# =============================================================================
# File:    PixelPerfect_Phase1_Tests.ps1
# Author:  OneTechly
# Created: May 2026
#
# USAGE:
#   # Run all tests against local dev server:
#   .\PixelPerfect_Phase1_Tests.ps1
#
#   # Run against production:
#   .\PixelPerfect_Phase1_Tests.ps1 -Env prod
#
#   # Skip the 10-second wait_for_selector timeout test (TC-WFS-02):
#   .\PixelPerfect_Phase1_Tests.ps1 -SkipSlow
#
#   # Run only one section (tier gate tests only):
#   .\PixelPerfect_Phase1_Tests.ps1 -Section TIER
#
#   # Run a single test case by ID:
#   .\PixelPerfect_Phase1_Tests.ps1 -Only TC-JS-05
#
#   # Suppress log file:
#   .\PixelPerfect_Phase1_Tests.ps1 -NoLog
#
# REQUIRES:
#   - PowerShell 5.1+ or PowerShell 7+
#   - Three test accounts configured in the CONFIG block below
#   - Backend running (local or prod) with Phase 1 files deployed
# =============================================================================

param(
    [string]$Env      = "local",   # "local" or "prod"
    [switch]$SkipSlow,             # skip TC-WFS-02 (waits 10 seconds)
    [string]$Section  = "ALL",     # TIER | JS | DE | WFS | REG | ALL
    [string]$Only     = "",        # run a single test case by ID
    [switch]$NoLog                 # suppress log file output
)

# =============================================================================
# CONFIGURATION — edit these to match your accounts before running
# =============================================================================

$LOCAL_BASE = "http://192.168.1.185:8000"
$PROD_BASE  = "https://api.pixelperfectapi.net"

# Free user — any account on the free tier
$FREE_USER = "UserProdTest"
$FREE_PASS = "Rt%@gP35="

# Pro user — subscription_tier = 'pro' in DB (e.g. UserProdTest_002)
$PRO_USER  = "UserProdTest_002"
$PRO_PASS  = "7Rt;Lk+5(-"

# Business user — subscription_tier = 'business' (leave empty to skip biz tests)
$BIZ_USER  = ""
$BIZ_PASS  = ""

# Test URL used for most screenshot calls
$TEST_URL  = "https://example.com"

# Log file path
$LOG_FILE  = ".\Phase1_TestResults_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

# =============================================================================
# SETUP
# =============================================================================

$BASE = if ($Env -eq "prod") { $PROD_BASE } else { $LOCAL_BASE }

$script:PASS    = 0
$script:FAIL    = 0
$script:SKIP    = 0
$script:RESULTS = @()
$script:Log     = @()

function Write-Log {
    param([string]$Line, [string]$Color = "White")
    Write-Host $Line -ForegroundColor $Color
    if (-not $NoLog) { $script:Log += $Line }
}

function Write-Section {
    param([string]$Title)
    $bar = "=" * 68
    Write-Log ""
    Write-Log $bar "Cyan"
    Write-Log "  $Title" "Cyan"
    Write-Log $bar "Cyan"
    Write-Log ""
}

# =============================================================================
# TOKEN MANAGEMENT
# =============================================================================

function Get-Token {
    param([string]$Username, [string]$Password, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Username) -or [string]::IsNullOrWhiteSpace($Password)) {
        Write-Log "  WARNING: $Label credentials not set in config — tests using this account will SKIP" "Yellow"
        return $null
    }
    try {
        $b = "{`"username`":`"$Username`",`"password`":`"$Password`"}"
        $h = @{ "Content-Type" = "application/json" }
        $r = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BASE/token_json" -Headers $h -Body $b -ErrorAction Stop
        $d = $r.Content | ConvertFrom-Json
        if ($d.access_token) {
            Write-Log "  OK  Token obtained for $Label ($Username)" "Green"
            return $d.access_token
        }
        Write-Log "  FAIL  No access_token for $Label" "Red"
        return $null
    }
    catch {
        Write-Log "  FAIL  Login failed for $Label ($Username): $_" "Red"
        return $null
    }
}

function Make-Headers {
    param([string]$Token)
    return @{ "Authorization" = "Bearer $Token"; "Content-Type" = "application/json" }
}

# =============================================================================
# HTTP HELPERS
# =============================================================================

function PP-Request {
    param([string]$Method, [string]$Path, [hashtable]$Headers, [string]$Body = $null)
    $p = @{
        UseBasicParsing = $true
        Method          = $Method
        Uri             = "$BASE$Path"
        Headers         = $Headers
        ErrorAction     = "Stop"
    }
    if ($Body) { $p.Body = $Body }
    try {
        $r = Invoke-WebRequest @p
        return @{ StatusCode=[int]$r.StatusCode; Content=$r.Content; Data=($r.Content|ConvertFrom-Json -EA SilentlyContinue); Error=$null }
    }
    catch {
        $sc=$null; $bc=$null
        if ($_.Exception.Response) {
            $sc = [int]$_.Exception.Response.StatusCode
            try {
                $sr = $_.Exception.Response.GetResponseStream()
                $rd = New-Object System.IO.StreamReader($sr)
                $bc = $rd.ReadToEnd()
            } catch {}
        }
        return @{ StatusCode=$sc; Content=$bc; Data=($bc|ConvertFrom-Json -EA SilentlyContinue); Error=$_.Exception.Message }
    }
}

function PP-Get    { param([string]$p, [hashtable]$h) PP-Request -Method GET    -Path $p -Headers $h }
function PP-Post   { param([string]$p, [hashtable]$h, [string]$b) PP-Request -Method POST   -Path $p -Headers $h -Body $b }
function PP-Delete { param([string]$p, [hashtable]$h) PP-Request -Method DELETE -Path $p -Headers $h }

function Shot {
    param([hashtable]$Headers, [hashtable]$Fields)
    PP-Post -p "/api/v1/screenshot/" -h $Headers -b ($Fields | ConvertTo-Json -Compress)
}

# =============================================================================
# TEST RUNNER
# =============================================================================

function Run-Test {
    param(
        [string]$Id,
        [string]$Title,
        [scriptblock]$TestBlock,
        [scriptblock]$AssertBlock,
        [string]$Notes   = "",
        [switch]$Skip,
        [string]$SkipWhy = ""
    )

    if ($Only -ne "" -and $Only -ne $Id) { return }
    $tag = $Id -replace "-\d+$","" -replace "TC-",""
    if ($Section -ne "ALL" -and $Section -ne $tag -and $Only -eq "") { return }

    if ($Skip) {
        Write-Log "  o  [$Id] $Title" "DarkGray"
        Write-Log "     SKIP: $SkipWhy" "DarkGray"
        $script:SKIP++
        $script:RESULTS += [PSCustomObject]@{ Id=$Id; Title=$Title; Result="SKIP"; Reason=$SkipWhy }
        return
    }

    Write-Log ""
    Write-Log "  > [$Id] $Title" "White"

    try {
        $result = & $TestBlock
        $assert = & $AssertBlock $result

        if ($assert.Pass) {
            Write-Log "    PASS  $($assert.Reason)" "Green"
            $script:PASS++
            $script:RESULTS += [PSCustomObject]@{ Id=$Id; Title=$Title; Result="PASS"; Reason=$assert.Reason }
        }
        else {
            Write-Log "    FAIL  $($assert.Reason)" "Red"
            if ($result.StatusCode) { Write-Log "     HTTP $($result.StatusCode)" "DarkRed" }
            if ($result.Content)    {
                $preview = $result.Content.Substring(0,[Math]::Min(250,$result.Content.Length))
                Write-Log "     Response: $preview" "DarkRed"
            }
            $script:FAIL++
            $script:RESULTS += [PSCustomObject]@{ Id=$Id; Title=$Title; Result="FAIL"; Reason=$assert.Reason }
        }
    }
    catch {
        Write-Log "    FAIL  Exception: $_" "Red"
        $script:FAIL++
        $script:RESULTS += [PSCustomObject]@{ Id=$Id; Title=$Title; Result="FAIL"; Reason="Exception: $_" }
    }

    if ($Notes) { Write-Log "     NOTE: $Notes" "DarkGray" }
}

# =============================================================================
# SCRIPT START
# =============================================================================

Clear-Host
Write-Log ("=" * 68) "Cyan"
Write-Log "  PixelPerfect Phase 1 Test Suite" "Cyan"
Write-Log "  Custom JavaScript Execution + Device Emulation (Pro+)" "Cyan"
Write-Log ("=" * 68) "Cyan"
Write-Log "  Environment : $Env  ($BASE)" "Gray"
Write-Log "  Started     : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "Gray"
Write-Log "  Section     : $Section   SkipSlow: $SkipSlow" "Gray"
Write-Log "  Log file    : $(if($NoLog){'disabled'}else{$LOG_FILE})" "Gray"
Write-Log ("=" * 68) "Cyan"
Write-Log ""
Write-Log "Authenticating..." "Yellow"

$FREE_TOKEN = Get-Token $FREE_USER $FREE_PASS "Free user"
$PRO_TOKEN  = Get-Token $PRO_USER  $PRO_PASS  "Pro user"
$BIZ_TOKEN  = Get-Token $BIZ_USER  $BIZ_PASS  "Business user"
Write-Log ""

$FREE_HDR = if ($FREE_TOKEN) { Make-Headers $FREE_TOKEN } else { $null }
$PRO_HDR  = if ($PRO_TOKEN)  { Make-Headers $PRO_TOKEN  } else { $null }
$BIZ_HDR  = if ($BIZ_TOKEN)  { Make-Headers $BIZ_TOKEN  } else { $null }

# =============================================================================
# SECTION 1 — PREREQUISITES
# =============================================================================
Write-Section "SECTION 1 — Prerequisites"

Run-Test -Id "P-01" -Title "Backend health — screenshot_service is ready" `
    -TestBlock { PP-Get "/health" @{} } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="HTTP $($r.StatusCode)" } }
        $svc = $r.Data.services.screenshot_service
        if ($svc -ne "ready") { return @{ Pass=$false; Reason="screenshot_service=$svc (expected ready)" } }
        return @{ Pass=$true; Reason="HTTP 200  screenshot_service=ready" }
    }

Run-Test -Id "P-02" -Title "Phase 1 features visible in /health response" `
    -TestBlock { PP-Get "/health" @{} } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="HTTP $($r.StatusCode)" } }
        $f = $r.Data.phase1_features
        if (-not $f) { return @{ Pass=$false; Reason="phase1_features key missing from /health" } }
        if ($f.custom_js -eq "Pro+" -and $f.device_emulation -eq "Pro+") {
            return @{ Pass=$true; Reason="custom_js=Pro+  device_emulation=Pro+" }
        }
        return @{ Pass=$false; Reason="Values wrong: custom_js=$($f.custom_js)  device_emulation=$($f.device_emulation)" }
    }

Run-Test -Id "P-03" -Title "Pro user token valid" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { PP-Get "/users/me" $PRO_HDR } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="Authenticated as $($r.Data.username)" }
    }

Run-Test -Id "P-04" -Title "Free user token valid" `
    -Skip:($null -eq $FREE_TOKEN) -SkipWhy "FREE_USER not configured" `
    -TestBlock { PP-Get "/users/me" $FREE_HDR } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="Authenticated as $($r.Data.username)" }
    }

Run-Test -Id "P-05" -Title "Route without trailing slash redirects or resolves" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { PP-Post "/api/v1/screenshot" $PRO_HDR "{`"url`":`"$TEST_URL`"}" } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -in 200,307,308) { return @{ Pass=$true; Reason="HTTP $($r.StatusCode) — redirect_slashes working" } }
        return @{ Pass=$false; Reason="Expected 200/307/308, got HTTP $($r.StatusCode)" }
    }

# =============================================================================
# SECTION 2 — TIER GATE TESTS
# =============================================================================
Write-Section "SECTION 2 — Tier Gate Tests (TC-TIER-*)"

Run-Test -Id "TC-TIER-01" -Title "Free user blocked from custom_js — expect 403" `
    -Skip:($null -eq $FREE_TOKEN) -SkipWhy "FREE_USER not configured" `
    -TestBlock { Shot $FREE_HDR @{ url=$TEST_URL; custom_js="document.title='test';" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -eq 403) { return @{ Pass=$true; Reason="HTTP 403 — tier gate working" } }
        return @{ Pass=$false; Reason="Expected 403, got HTTP $($r.StatusCode)" }
    }

Run-Test -Id "TC-TIER-02" -Title "Free user blocked from device emulation — expect 403" `
    -Skip:($null -eq $FREE_TOKEN) -SkipWhy "FREE_USER not configured" `
    -TestBlock { Shot $FREE_HDR @{ url=$TEST_URL; device="iphone_13" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -eq 403) { return @{ Pass=$true; Reason="HTTP 403 — tier gate working" } }
        return @{ Pass=$false; Reason="Expected 403, got HTTP $($r.StatusCode)" }
    }

Run-Test -Id "TC-TIER-03" -Title "Free user blocked from GET /devices — expect 403" `
    -Skip:($null -eq $FREE_TOKEN) -SkipWhy "FREE_USER not configured" `
    -TestBlock { PP-Get "/api/v1/screenshot/devices" $FREE_HDR } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -eq 403) { return @{ Pass=$true; Reason="HTTP 403 — /devices endpoint gated" } }
        return @{ Pass=$false; Reason="Expected 403, got HTTP $($r.StatusCode)" }
    }

Run-Test -Id "TC-TIER-04" -Title "Pro user allowed — custom_js returns 200" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; custom_js="document.body.style.background='red';" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if (-not $r.Data.screenshot_url) { return @{ Pass=$false; Reason="screenshot_url missing" } }
        return @{ Pass=$true; Reason="HTTP 200  screenshot_url present  js_warning=$($r.Data.js_warning)" }
    }

Run-Test -Id "TC-TIER-05" -Title "Pro user allowed — device emulation returns 200" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; device="iphone_13" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $du = $r.Data.device_used
        if ($du -ne "iphone_13") { return @{ Pass=$false; Reason="device_used='$du' (expected iphone_13)" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=iphone_13" }
    }

Run-Test -Id "TC-TIER-06" -Title "Pro user reads /devices — expect exactly 9 devices" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { PP-Get "/api/v1/screenshot/devices" $PRO_HDR } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $cnt = $r.Data.devices.Count
        if ($cnt -ne 9) { return @{ Pass=$false; Reason="Expected 9 devices, got $cnt" } }
        $want = @("iphone_13","iphone_13_pro_max","iphone_se","pixel_5","pixel_7","ipad_pro","ipad_mini","galaxy_s9","galaxy_tab_s4")
        $miss = $want | Where-Object { $_ -notin $r.Data.devices }
        if ($miss) { return @{ Pass=$false; Reason="Missing: $($miss -join ', ')" } }
        return @{ Pass=$true; Reason="HTTP 200  all 9 devices present" }
    }

Run-Test -Id "TC-TIER-07" -Title "Business user — device + custom_js in one request" `
    -Skip:($null -eq $BIZ_TOKEN) -SkipWhy "BIZ_USER not configured" `
    -TestBlock { Shot $BIZ_HDR @{ url=$TEST_URL; device="ipad_pro"; custom_js="document.title='biz';" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $du = $r.Data.device_used
        if ($du -ne "ipad_pro") { return @{ Pass=$false; Reason="device_used='$du'" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=ipad_pro  js_warning=$($r.Data.js_warning)" }
    }

Run-Test -Id "TC-TIER-08" -Title "/stats/usage features block — correct tier flags" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { PP-Get "/api/v1/screenshot/stats/usage" $PRO_HDR } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $f = $r.Data.features
        if (-not $f) { return @{ Pass=$false; Reason="features block missing" } }
        if ($f.custom_js        -ne $true)  { return @{ Pass=$false; Reason="custom_js=$($f.custom_js) (expected True)" } }
        if ($f.device_emulation -ne $true)  { return @{ Pass=$false; Reason="device_emulation=$($f.device_emulation) (expected True)" } }
        if ($f.element_selection -ne $false) { return @{ Pass=$false; Reason="element_selection=$($f.element_selection) (expected False - Phase 2)" } }
        return @{ Pass=$true; Reason="custom_js=True  device_emulation=True  element_selection=False (correct)" }
    }
Write-Log "     NOTE: Confirms TIER_FEATURES dict in models.py and has_feature() are wired correctly" "DarkGray"

# =============================================================================
# SECTION 3 — CUSTOM JAVASCRIPT TESTS
# =============================================================================
Write-Section "SECTION 3 — Custom JavaScript Execution (TC-JS-*)"

Run-Test -Id "TC-JS-01" -Title "Hide h1 element via custom_js" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; custom_js="document.querySelector('h1')?.remove();" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if (-not $r.Data.screenshot_url) { return @{ Pass=$false; Reason="screenshot_url missing" } }
        if ($r.Data.js_warning) { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null  URL present" }
    } `
    -Notes "VISUAL: open screenshot_url — h1 heading should be absent"

Run-Test -Id "TC-JS-02" -Title "Modify background color via custom_js" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; custom_js="document.body.style.backgroundColor='#FF0000';" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.js_warning) { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null" }
    } `
    -Notes "VISUAL: screenshot should have bright red background"

Run-Test -Id "TC-JS-03" -Title "Inject custom banner element via custom_js" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $js = "var b=document.createElement('div');b.style='position:fixed;top:0;left:0;width:100%;background:blue;color:white;font-size:24px;padding:10px;z-index:9999;text-align:center;';b.textContent='CUSTOM BANNER';document.body.prepend(b);"
        Shot $PRO_HDR @{ url=$TEST_URL; custom_js=$js }
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.js_warning) { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null" }
    } `
    -Notes "VISUAL: blue banner with CUSTOM BANNER text at top of screenshot"

Run-Test -Id "TC-JS-04" -Title "Scroll to bottom via custom_js" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; full_page=$false; custom_js="window.scrollTo(0,document.body.scrollHeight);" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=$($r.Data.js_warning)" }
    } `
    -Notes "VISUAL: viewport shows bottom of page (example.com is short — try a taller site for a stronger test)"

Run-Test -Id "TC-JS-05" -Title "OPTION-C CRITICAL: malformed JS — must return 200 + js_warning" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; custom_js="this is not valid javascript !!@#$" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="CRITICAL: Expected 200, got HTTP $($r.StatusCode) — option-c broken" } }
        if (-not $r.Data.screenshot_url) { return @{ Pass=$false; Reason="screenshot_url missing despite 200" } }
        if (-not $r.Data.js_warning)     { return @{ Pass=$false; Reason="js_warning is null — should contain the syntax error" } }
        $jw = $r.Data.js_warning.Substring(0,[Math]::Min(80,$r.Data.js_warning.Length))
        return @{ Pass=$true; Reason="HTTP 200  screenshot_url present  js_warning='$jw...'" }
    }

Run-Test -Id "TC-JS-06" -Title "OPTION-C: runtime ReferenceError — must return 200 + js_warning" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; custom_js="undefinedFunction();" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode) — option-c broken" } }
        if (-not $r.Data.js_warning) { return @{ Pass=$false; Reason="js_warning is null — should contain ReferenceError" } }
        $jw = $r.Data.js_warning.Substring(0,[Math]::Min(80,$r.Data.js_warning.Length))
        return @{ Pass=$true; Reason="HTTP 200  js_warning='$jw...'" }
    }

Run-Test -Id "TC-JS-07" -Title "JS at max length (10,000 chars) — expect 200" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $js   = "// " + ("x" * 9997)   # exactly 10,000 chars
        $json = "{`"url`":`"$TEST_URL`",`"custom_js`":`"$js`"}"
        PP-Post "/api/v1/screenshot/" $PRO_HDR $json
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="HTTP 200 — 10,000-char payload accepted" }
    }

Run-Test -Id "TC-JS-08" -Title "JS over 10,000 chars — expect 422 (Pydantic)" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $js   = "x" * 10001
        $json = "{`"url`":`"$TEST_URL`",`"custom_js`":`"$js`"}"
        PP-Post "/api/v1/screenshot/" $PRO_HDR $json
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -eq 422) { return @{ Pass=$true; Reason="HTTP 422 — Pydantic max_length=10000 enforced" } }
        return @{ Pass=$false; Reason="Expected 422, got HTTP $($r.StatusCode)" }
    }

Run-Test -Id "TC-JS-09" -Title "custom_js + wait_for_selector + delay=1 — all three together" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $start = Get-Date
        $r = Shot $PRO_HDR @{ url=$TEST_URL; wait_for_selector="h1"; custom_js="document.querySelector('h1').style.color='blue';"; delay=1 }
        $r["Elapsed"] = ((Get-Date) - $start).TotalSeconds
        return $r
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.js_warning)    { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        $e = [math]::Round($r.Elapsed,1)
        if ($e -lt 1.0) { return @{ Pass=$false; Reason="Elapsed ${e}s — expected >1s (delay=1 not applied?)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null  elapsed=${e}s" }
    } `
    -Notes "VISUAL: h1 heading should be blue"

Run-Test -Id "TC-JS-10" -Title "custom_js + remove_elements (existing feature) combined" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; custom_js="document.body.style.background='lime';"; remove_elements=@("h1") } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.js_warning)    { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null — both params processed" }
    } `
    -Notes "VISUAL: lime green background, h1 hidden"

# =============================================================================
# SECTION 4 — DEVICE EMULATION TESTS
# =============================================================================
Write-Section "SECTION 4 — Device Emulation (TC-DE-*)"

$ALL_DEVICES = @("iphone_13","iphone_13_pro_max","iphone_se","pixel_5","pixel_7","ipad_pro","ipad_mini","galaxy_s9","galaxy_tab_s4")

Run-Test -Id "TC-DE-01" -Title "iPhone 13 — mobile viewport" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; device="iphone_13" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $du = $r.Data.device_used
        if ($du -ne "iphone_13") { return @{ Pass=$false; Reason="device_used='$du'" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=iphone_13" }
    } `
    -Notes "VISUAL: narrow mobile layout (~390px)"

Run-Test -Id "TC-DE-02" -Title "Pixel 5 — Android Chrome UA visible on UA-detection page" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url="https://www.whatismybrowser.com/detect/what-is-my-user-agent/"; device="pixel_5" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.device_used -ne "pixel_5") { return @{ Pass=$false; Reason="device_used='$($r.Data.device_used)'" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=pixel_5" }
    } `
    -Notes "VISUAL: open screenshot — page should show Chrome/Android in UA string"

Run-Test -Id "TC-DE-03" -Title "iPad Pro — tablet viewport" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; device="ipad_pro" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.device_used -ne "ipad_pro") { return @{ Pass=$false; Reason="device_used='$($r.Data.device_used)'" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=ipad_pro" }
    } `
    -Notes "VISUAL: wide tablet layout (1024px)"

Run-Test -Id "TC-DE-04" -Title "All 9 devices smoke test — each returns 200 with correct device_used" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $fails = @()
        foreach ($dev in $ALL_DEVICES) {
            Write-Log "     Testing device: $dev..." "DarkGray"
            $r = Shot $PRO_HDR @{ url=$TEST_URL; device=$dev }
            if ($r.StatusCode -ne 200)            { $fails += "$dev HTTP$($r.StatusCode)" }
            elseif ($r.Data.device_used -ne $dev)  { $fails += "$dev device_used=$($r.Data.device_used)" }
        }
        return @{ StatusCode=200; Content=""; Data=$null; Error=$null; Failures=$fails }
    } `
    -AssertBlock {
        param($r)
        if ($r.Failures.Count -eq 0) { return @{ Pass=$true; Reason="All 9 devices returned HTTP 200 with correct device_used" } }
        return @{ Pass=$false; Reason="Failures: $($r.Failures -join ' | ')" }
    } `
    -Notes "This test makes 9 API calls — takes 30-60 seconds"

Run-Test -Id "TC-DE-05" -Title "Unknown device key 'nokia_3310' — expect 400" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; device="nokia_3310" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -eq 400) { return @{ Pass=$true; Reason="HTTP 400 — unknown device rejected cleanly" } }
        return @{ Pass=$false; Reason="Expected 400, got HTTP $($r.StatusCode)" }
    }

Run-Test -Id "TC-DE-06" -Title "device overrides width/height — device_used set despite width=1920" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; device="iphone_13"; width=1920; height=1080 } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.device_used -ne "iphone_13") { return @{ Pass=$false; Reason="device_used='$($r.Data.device_used)'" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=iphone_13 (device override active)" }
    } `
    -Notes "VISUAL: screenshot is mobile width (~390px), NOT 1920px"

Run-Test -Id "TC-DE-07" -Title "device + dark_mode combined" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; device="pixel_5"; dark_mode=$true } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.device_used -ne "pixel_5") { return @{ Pass=$false; Reason="device_used='$($r.Data.device_used)'" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=pixel_5  dark_mode accepted" }
    } `
    -Notes "VISUAL: dark background + mobile layout (if site supports prefers-color-scheme)"

Run-Test -Id "TC-DE-08" -Title "Phase 1 full combo: device + custom_js + wait_for_selector + delay" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        Shot $PRO_HDR @{ url=$TEST_URL; device="iphone_13"; custom_js="document.body.style.background='lime';"; wait_for_selector="h1"; delay=1 }
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.device_used -ne "iphone_13") { return @{ Pass=$false; Reason="device_used='$($r.Data.device_used)'" } }
        if ($r.Data.js_warning) { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  device_used=iphone_13  js_warning=null" }
    } `
    -Notes "VISUAL: lime green mobile screenshot"

Run-Test -Id "TC-DE-09" -Title "GET /devices — 9 devices with descriptions" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { PP-Get "/api/v1/screenshot/devices" $PRO_HDR } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $dc = $r.Data.devices.Count
        $dd = ($r.Data.descriptions | Get-Member -MemberType NoteProperty).Count
        if ($dc -ne 9) { return @{ Pass=$false; Reason="devices count=$dc (expected 9)" } }
        if ($dd -ne 9) { return @{ Pass=$false; Reason="descriptions count=$dd (expected 9)" } }
        return @{ Pass=$true; Reason="HTTP 200  devices=$dc  descriptions=$dd" }
    }

# =============================================================================
# SECTION 5 — WAIT_FOR_SELECTOR TESTS
# =============================================================================
Write-Section "SECTION 5 — wait_for_selector Tests (TC-WFS-*)"

Run-Test -Id "TC-WFS-01" -Title "Wait for existing element (h1) — resolves quickly" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; wait_for_selector="h1" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.js_warning) { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null — selector found" }
    }

$wfsSkipReason = if ($SkipSlow) { "SkipSlow flag set — use -SkipSlow:`$false to run (waits 10 seconds)" } else { "PRO_USER not configured" }
Run-Test -Id "TC-WFS-02" -Title "Non-existent selector — 10s timeout, non-fatal, still returns 200" `
    -Skip:($null -eq $PRO_TOKEN -or $SkipSlow) `
    -SkipWhy $wfsSkipReason `
    -TestBlock {
        Write-Log "     Waiting up to 10 seconds for timeout (expected behavior)..." "DarkGray"
        Shot $PRO_HDR @{ url=$TEST_URL; wait_for_selector="#element-that-does-not-exist-xyz999" }
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200 (non-fatal), got HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="HTTP 200 — timeout was non-fatal, capture continued" }
    } `
    -Notes "Check backend logs for: warning wait_for_selector timed out"

Run-Test -Id "TC-WFS-03" -Title "wait_for_selector + custom_js — correct execution order" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; wait_for_selector="h1"; custom_js="document.querySelector('h1').textContent='Modified by JS';" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.js_warning) { return @{ Pass=$false; Reason="Unexpected js_warning: $($r.Data.js_warning)" } }
        return @{ Pass=$true; Reason="HTTP 200  js_warning=null" }
    } `
    -Notes "VISUAL: h1 should read 'Modified by JS'"

# =============================================================================
# SECTION 6 — REGRESSION TESTS
# =============================================================================
Write-Section "SECTION 6 — Regression Tests (TC-REG-*)"

Run-Test -Id "TC-REG-01" -Title "Basic screenshot — no advanced params (critical baseline)" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if (-not $r.Data.screenshot_url) { return @{ Pass=$false; Reason="screenshot_url missing" } }
        return @{ Pass=$true; Reason="HTTP 200  screenshot_url present  js_warning=$($r.Data.js_warning)" }
    } `
    -Notes "All existing integrations depend on this working unchanged"

Run-Test -Id "TC-REG-02" -Title "dark_mode still works" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; dark_mode=$true } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="HTTP 200  dark_mode accepted" }
    }

Run-Test -Id "TC-REG-03" -Title "delay=2 still works and adds >2s to processing time" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $s = Get-Date
        $r = Shot $PRO_HDR @{ url=$TEST_URL; delay=2 }
        $r["Elapsed"] = ((Get-Date) - $s).TotalSeconds
        return $r
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        $e = [math]::Round($r.Elapsed,1)
        if ($e -lt 2.0) { return @{ Pass=$false; Reason="Elapsed ${e}s — delay=2 not applied?" } }
        return @{ Pass=$true; Reason="HTTP 200  elapsed=${e}s (delay working)" }
    }

Run-Test -Id "TC-REG-04" -Title "remove_elements still works" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; remove_elements=@("h1") } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        return @{ Pass=$true; Reason="HTTP 200  remove_elements accepted" }
    } `
    -Notes "VISUAL: h1 heading should be absent"

Run-Test -Id "TC-REG-05" -Title "full_page still works" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; full_page=$true } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if (-not $r.Data.screenshot_url) { return @{ Pass=$false; Reason="screenshot_url missing" } }
        return @{ Pass=$true; Reason="HTTP 200  full_page accepted" }
    }

Run-Test -Id "TC-REG-06" -Title "WebP format still works (requires Pillow)" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock { Shot $PRO_HDR @{ url=$TEST_URL; format="webp" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if ($r.Data.format -ne "webp") { return @{ Pass=$false; Reason="format=$($r.Data.format) (expected webp)" } }
        return @{ Pass=$true; Reason="HTTP 200  format=webp" }
    }

Run-Test -Id "TC-REG-07" -Title "Usage counter increments after Phase 1 capture" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $before = (PP-Get "/subscription_status" $PRO_HDR).Data.usage.screenshots
        Shot $PRO_HDR @{ url=$TEST_URL; device="iphone_13" } | Out-Null
        $after  = (PP-Get "/subscription_status" $PRO_HDR).Data.usage.screenshots
        return @{ StatusCode=200; Content=""; Data=$null; Error=$null; Before=$before; After=$after }
    } `
    -AssertBlock {
        param($r)
        if ($r.After -gt $r.Before) { return @{ Pass=$true; Reason="Usage: $($r.Before) -> $($r.After) (incremented)" } }
        return @{ Pass=$false; Reason="Usage did not increment: before=$($r.Before)  after=$($r.After)" }
    }

Run-Test -Id "TC-REG-08" -Title "Batch processing unaffected (batch.py unchanged)" `
    -Skip:($null -eq $PRO_TOKEN) -SkipWhy "PRO_USER not configured" `
    -TestBlock {
        $body = "{`"urls`":[`"$TEST_URL`",`"https://github.com`"],`"format`":`"png`"}"
        PP-Post "/api/v1/batch/submit" $PRO_HDR $body
    } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if (-not $r.Data.id)       { return @{ Pass=$false; Reason="No job id in response" } }
        if ($r.Data.total -ne 2)   { return @{ Pass=$false; Reason="total=$($r.Data.total) (expected 2)" } }
        return @{ Pass=$true; Reason="HTTP 200  job_id=$($r.Data.id)  total=$($r.Data.total)  status=$($r.Data.status)" }
    } `
    -Notes "Job starts async — status will be queued/processing, not completed immediately"

Run-Test -Id "TC-REG-09" -Title "Phase 2 stub: target_element returns clean 400/403 (not 500)" `
    -Skip:($null -eq $BIZ_TOKEN) -SkipWhy "BIZ_USER not configured — Business+ needed to pass tier gate" `
    -TestBlock { Shot $BIZ_HDR @{ url=$TEST_URL; target_element=".hero" } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -eq 500) { return @{ Pass=$false; Reason="CRITICAL: got 500 — stub crashed instead of clean error" } }
        if ($r.StatusCode -in 400,403) {
            return @{ Pass=$true; Reason="HTTP $($r.StatusCode) — clean error: $($r.Data.detail)" }
        }
        return @{ Pass=$false; Reason="Unexpected HTTP $($r.StatusCode)" }
    }

Run-Test -Id "TC-REG-10" -Title "Free user basic screenshot still works (no regression)" `
    -Skip:($null -eq $FREE_TOKEN) -SkipWhy "FREE_USER not configured" `
    -TestBlock { Shot $FREE_HDR @{ url=$TEST_URL } } `
    -AssertBlock {
        param($r)
        if ($r.StatusCode -ne 200) { return @{ Pass=$false; Reason="Expected 200, got HTTP $($r.StatusCode)" } }
        if (-not $r.Data.screenshot_url) { return @{ Pass=$false; Reason="screenshot_url missing" } }
        return @{ Pass=$true; Reason="HTTP 200  free user basic capture working" }
    } `
    -Notes "Phase 1 must not break the free tier basic flow"

# =============================================================================
# RESULTS SUMMARY
# =============================================================================

Write-Log ""
Write-Log ("=" * 68) "Cyan"
Write-Log "  PHASE 1 TEST RESULTS SUMMARY" "Cyan"
Write-Log ("=" * 68) "Cyan"
Write-Log ""

$total = $script:PASS + $script:FAIL + $script:SKIP
Write-Log ("  Total: {0}   Pass: {1}   Fail: {2}   Skip: {3}" -f $total, $script:PASS, $script:FAIL, $script:SKIP) "White"
Write-Log ""

$sections = @(
    @{ Tag="P";       Name="Prerequisites" }
    @{ Tag="TC-TIER"; Name="Tier Gate" }
    @{ Tag="TC-JS";   Name="Custom JavaScript" }
    @{ Tag="TC-DE";   Name="Device Emulation" }
    @{ Tag="TC-WFS";  Name="wait_for_selector" }
    @{ Tag="TC-REG";  Name="Regression" }
)
foreach ($sec in $sections) {
    $rows  = $script:RESULTS | Where-Object { $_.Id -like "$($sec.Tag)*" }
    $p = ($rows | Where-Object { $_.Result -eq "PASS" }).Count
    $f = ($rows | Where-Object { $_.Result -eq "FAIL" }).Count
    $s = ($rows | Where-Object { $_.Result -eq "SKIP" }).Count
    $c = if ($f -gt 0) { "Red" } elseif ($s -gt 0 -and $p -eq 0) { "Yellow" } else { "Green" }
    Write-Log ("  {0,-26}  Pass:{1,3}  Fail:{2,3}  Skip:{3,3}" -f $sec.Name, $p, $f, $s) $c
}
Write-Log ""

$failures = $script:RESULTS | Where-Object { $_.Result -eq "FAIL" }
if ($failures.Count -gt 0) {
    Write-Log "  FAILURES:" "Red"
    foreach ($f in $failures) {
        Write-Log "    [$($f.Id)]  $($f.Title)" "Red"
        Write-Log "    => $($f.Reason)" "DarkRed"
    }
    Write-Log ""
}

$tierFails = ($script:RESULTS | Where-Object { $_.Id -like "TC-TIER*" -and $_.Result -eq "FAIL" }).Count
$jsFails   = ($script:RESULTS | Where-Object { $_.Id -like "TC-JS*"   -and $_.Result -eq "FAIL" }).Count
$deFails   = ($script:RESULTS | Where-Object { $_.Id -like "TC-DE*"   -and $_.Result -eq "FAIL" }).Count
$regFails  = ($script:RESULTS | Where-Object { $_.Id -like "TC-REG*"  -and $_.Result -eq "FAIL" }).Count

Write-Log "  RELEASE CRITERIA:" "White"

$tLabel = if ($tierFails -eq 0) { "PASS" } else { "FAIL" }
$tColor = if ($tierFails -eq 0) { "Green" } else { "Red" }
Write-Log ("  {0}  TC-TIER-*  Tier gates     : {1} failures" -f $tLabel, $tierFails) $tColor

$jLabel = if ($jsFails -eq 0) { "PASS" } else { "FAIL" }
$jColor = if ($jsFails -eq 0) { "Green" } else { "Red" }
Write-Log ("  {0}  TC-JS-*    Custom JS      : {1} failures" -f $jLabel, $jsFails) $jColor

$dLabel = if ($deFails -eq 0) { "PASS" } else { "FAIL" }
$dColor = if ($deFails -eq 0) { "Green" } else { "Red" }
Write-Log ("  {0}  TC-DE-*    Device Emul.   : {1} failures" -f $dLabel, $deFails) $dColor

$rLabel = if ($regFails -eq 0) { "PASS" } else { "FAIL" }
$rColor = if ($regFails -eq 0) { "Green" } else { "Red" }
Write-Log ("  {0}  TC-REG-*   Regressions    : {1} failures" -f $rLabel, $regFails) $rColor
Write-Log ""

$allClear = ($tierFails + $jsFails + $deFails + $regFails) -eq 0
if ($allClear) {
    Write-Log "  PHASE 1 RELEASE APPROVED — all critical tests pass" "Green"
    Write-Log "  Ready to proceed to Phase 2: Element Selection (Business+)" "Green"
} else {
    Write-Log "  PHASE 1 NOT READY — fix the failures above before releasing" "Red"
}

Write-Log ""
Write-Log ("=" * 68) "Cyan"
Write-Log "  Completed: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "Gray"
Write-Log ("=" * 68) "Cyan"

if (-not $NoLog) {
    $script:Log | Out-File -FilePath $LOG_FILE -Encoding UTF8
    Write-Log ""
    Write-Host "  Log saved: $LOG_FILE" -ForegroundColor Gray
}

if ($script:FAIL -gt 0) { exit 1 } else { exit 0 }
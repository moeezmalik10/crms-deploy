# Checks each deployment step.  Usage:  CHECK_DEPLOY.bat <step>
#   files | github | backend | ids | website | cors | nodes | all
param([string]$Step = "all")

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot "deploy_config.ps1")
$Repo = Split-Path -Parent $PSScriptRoot
$script:fails = 0
function Write-Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Write-Ok($t)   { Write-Host "    OK   $t" -ForegroundColor Green }
function Write-Warn($t) { Write-Host "    WARN $t" -ForegroundColor Yellow }
function Fail($t)       { Write-Host "    FAIL $t" -ForegroundColor Red; $script:fails++ }
function Need([string]$name) { if (-not $DEPLOY[$name]) { Fail "$name is empty in tools\deploy_config.ps1"; return $false }; return $true }
function U([string]$name) { $DEPLOY[$name].Trim().TrimEnd('/') }
# Free Render services sleep; the first request after a sleep can take about a minute.
function Get-Url([string]$url, [int]$timeout = 100) { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeout }
function Get-AdminToken {
    $body = '{"email":"' + $TEST_ADMIN.Email + '","password":"' + $TEST_ADMIN.Password + '"}'
    Invoke-RestMethod -Method Post -Uri ((U "BackendUrl") + "/auth/login") -ContentType "application/json" -Body $body -TimeoutSec 100
}

function Check-Files {
    Write-Step "Step 1 - package files ($Repo)"
    foreach ($f in "render.yaml", ".gitignore", ".python-version", "backend\run.py", "backend\requirements.txt", "ids\app.py",
                   "ids\requirements.txt", "frontend\package.json", "frontend\vercel.json", "frontend\src\config.js",
                   "node-agent\agent.py", "node-agent\backend_url.txt", "database\supabase_setup.sql") {
        if (Test-Path -LiteralPath (Join-Path $Repo $f)) { Write-Ok $f } else { Fail "missing $f - extract the whole zip again" }
    }
    $secret = Get-ChildItem -LiteralPath $Repo -Recurse -File -Include *.py, *.js, *.jsx -ErrorAction SilentlyContinue |
              Where-Object { $_.FullName -notmatch '[\\/](node_modules|venv)[\\/]' } |
              Select-String -Pattern 'eyJhbGciOi' -List
    if ($secret) { Fail ("a key is written in code: " + ($secret.Path -join ', ') + " - keys belong in Render/Vercel settings") } else { Write-Ok "no keys written in the code" }
}

function Check-Github {
    Write-Step "Step 2 - GitHub"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git is not installed (git-scm.com)"; return }
    if (-not (Test-Path -LiteralPath (Join-Path $Repo ".git"))) { Fail "this folder is not a git repository yet (step 2 commands)"; return }
    $remote = (& git -C $Repo remote get-url origin 2>$null)
    if (-not $remote) { Fail "no 'origin' remote - run: git remote add origin <your repo URL>"; return }
    Write-Ok "remote: $remote"
    $tracked = (& git -C $Repo ls-files) -join "`n"
    if ($tracked -match '(^|/)(venv|node_modules)/') { Fail "venv or node_modules were committed" } else { Write-Ok "no venv / node_modules committed" }
    if ($tracked -notmatch '(^|\n)render\.yaml') { Fail "render.yaml is not committed" } else { Write-Ok "render.yaml committed" }
    & git -C $Repo fetch origin --quiet 2>$null
    $status = (& git -C $Repo status -sb 2>$null | Select-Object -First 1)
    if ($status -match 'ahead') { Fail "not pushed yet ($status) - run: git push" } elseif (-not $status) { Fail "could not read git status" } else { Write-Ok "pushed ($status)" }
    if (& git -C $Repo status --porcelain) { Write-Warn "uncommitted changes - run: git add . ; git commit -m update ; git push" }
    Write-Host "    On github.com the repository should show the word 'Private'." -ForegroundColor Yellow
}

function Check-Backend {
    Write-Step "Step 3a - backend on Render"
    if (-not (Need "BackendUrl")) { return }
    try { $r = Get-Url ((U "BackendUrl") + "/health"); if ($r.Content -match '"status"\s*:\s*"ok"') { Write-Ok "/health -> ok" } else { Fail "unexpected /health reply" } }
    catch { Fail "cannot open $(U 'BackendUrl')/health : $($_.Exception.Message) -> see Render -> crms-backend -> Logs"; return }
    try { $t = Get-AdminToken; if ($t.access_token) { Write-Ok "admin login works (Render reaches Supabase)" } }
    catch { Fail "admin login failed: $($_.Exception.Message) -> check DATABASE_URL / JWT_SECRET_KEY on Render; run database\supabase_setup.sql" }
}

function Check-Ids {
    Write-Step "Step 3b - IDS on Render"
    if (-not (Need "IdsUrl")) { return }
    try { $null = Get-Url ((U "IdsUrl") + "/health"); Write-Ok "/health -> ok" }
    catch { Fail "cannot open $(U 'IdsUrl')/health : $($_.Exception.Message) -> see Render -> crms-ids -> Logs"; return }
    $readOk = $false
    try { $r = Get-Url ((U "IdsUrl") + "/blacklisted"); if ($r.Content.Trim().StartsWith('[')) { Write-Ok "/blacklisted -> list (IDS reaches Supabase)"; $readOk = $true } else { Fail "IDS cannot read Supabase: $($r.Content)" } }
    catch { Fail "/blacklisted failed: $($_.Exception.Message)" }
    if (-not $readOk) {
        # Ask the IDS what it sees (URL, kind of key, Supabase's answer). The key itself is never shown.
        try {
            $d = Invoke-RestMethod -Uri ((U "IdsUrl") + "/health/supabase") -TimeoutSec 60
            Write-Host "    What crms-ids sees:" -ForegroundColor Yellow
            Write-Host "      SUPABASE_URL : $($d.supabase_url)"
            Write-Host "      SUPABASE_KEY : $($d.supabase_key)  (length $($d.supabase_key_length))"
            if ($d.rest_status) { Write-Host "      Supabase said: $($d.rest_status) $($d.rest_reply)" }
            if ($d.error)   { Write-Host "      Error        : $($d.error)" }
            if ($d.warning) { Write-Host "      Warning      : $($d.warning)" -ForegroundColor Yellow }
            Write-Host "      Result       : $($d.result)" -ForegroundColor Yellow
        } catch {
            Write-Warn "this IDS has no /health/supabase yet - push the updated ids folder (git add . ; git commit -m 'IDS fix' ; git push), wait for Live, check again"
        }
    }
    try {
        $d = Invoke-RestMethod -Method Post -Uri ((U "IdsUrl") + "/detect") -ContentType "application/json" -Body '{"email":"health-check@uog.edu.pk","Destination Port":80,"Flow Duration":100}' -TimeoutSec 100
        Write-Ok "/detect works (prediction: $($d.prediction))"
    } catch { Fail "/detect failed: $($_.Exception.Message)" }
}

function Check-Website {
    Write-Step "Step 4 - website on Vercel"
    if (-not (Need "FrontendUrl")) { return }
    $u = U "FrontendUrl"
    try { $r = Get-Url $u 40 } catch { Fail "cannot open $u : $($_.Exception.Message)"; return }
    if ($r.Content -match 'id="root"') { Write-Ok "$u loads" } else { Fail "$u did not return the CRMS page" }
    try { $null = Get-Url "$u/adminpage/machines" 40; Write-Ok "page refresh works" } catch { Fail "inner pages give an error - Root Directory must be 'frontend'" }
    $js = [regex]::Match($r.Content, 'src="(/assets/[^"]+\.js)"').Groups[1].Value
    if (-not $js) { Fail "could not find the website's script"; return }
    $bundle = (Get-Url "$u$js" 40).Content
    if ($DEPLOY.BackendUrl -and $bundle.Contains((U "BackendUrl"))) { Write-Ok "website calls $(U 'BackendUrl')" } else { Fail "website does not call BackendUrl -> set VITE_API_BASE in Vercel, then Redeploy" }
    if ($DEPLOY.IdsUrl -and $bundle.Contains((U "IdsUrl"))) { Write-Ok "website calls $(U 'IdsUrl')" } else { Fail "website does not call IdsUrl -> set VITE_IDS_BASE in Vercel, then Redeploy" }
    if ($bundle -match 'supabase\.co') { Write-Ok "Supabase address is set" } else { Fail "VITE_SUPABASE_URL missing in Vercel -> add it, then Redeploy" }
}

function Check-Cors {
    Write-Step "Step 5 - backend accepts the website (CORS)"
    if (-not (Need "BackendUrl") -or -not (Need "FrontendUrl")) { return }
    $origin = U "FrontendUrl"
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Method Options -Uri ((U "BackendUrl") + "/auth/login") -TimeoutSec 100 -Headers @{
            "Origin" = $origin; "Access-Control-Request-Method" = "POST"; "Access-Control-Request-Headers" = "content-type" }
        $allow = "$($r.Headers['Access-Control-Allow-Origin'])"
        if ($allow -eq $origin) { Write-Ok "backend allows $origin" } else { Fail "backend does not allow $origin (got '$allow') -> set CORS_ALLOWED_ORIGINS=$origin on crms-backend" }
    } catch { Fail "CORS check failed: $($_.Exception.Message)" }
}

function Check-Nodes {
    Write-Step "Steps 6-7 - PCs in the pool"
    if (-not (Need "BackendUrl")) { return }
    try {
        $t = Get-AdminToken
        $nodes = Invoke-RestMethod -Uri ((U "BackendUrl") + "/admin/nodes") -Headers @{ Authorization = "Bearer $($t.access_token)" } -TimeoutSec 60
        if (-not $nodes) { Fail "no PCs registered yet - run start_agent.bat (step 6)"; return }
        foreach ($n in $nodes) {
            $line = "{0,-22} {1,-8} RAM free {2,6:N0} MB   last seen {3}" -f $n.name, $n.status, $n.ram_free, $n.last_seen
            if ($n.status -eq "online") { Write-Ok $line } else { Write-Warn $line }
        }
        $online = @($nodes | Where-Object { $_.status -eq "online" }).Count
        Write-Host "    $online PC(s) online"
        if ($online -eq 0) { Fail "no PC online - start start_agent.bat" }
        elseif ($online -lt 2) { Write-Warn "pooling needs 2 PCs online - start the agent on the second PC (step 7)" }
    } catch { Fail "could not list PCs: $($_.Exception.Message)" }
}

switch ($Step.ToLower()) {
    "files"   { Check-Files }
    "github"  { Check-Github }
    "backend" { Check-Backend }
    "ids"     { Check-Ids }
    "website" { Check-Website }
    "cors"    { Check-Cors }
    "nodes"   { Check-Nodes }
    "all"     { Check-Files; Check-Github; Check-Backend; Check-Ids; Check-Website; Check-Cors; Check-Nodes }
    default   { Write-Host "Use one of: files, github, backend, ids, website, cors, nodes, all" }
}
Write-Host ""
if ($script:fails -eq 0) { Write-Host "PASSED" -ForegroundColor Green } else { Write-Host "$($script:fails) problem(s) - fix the FAIL lines above, then run the check again" -ForegroundColor Red }

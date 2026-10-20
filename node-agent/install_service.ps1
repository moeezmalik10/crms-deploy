<#
  CRMS - run the agent as a Windows service with a watchdog (R1: "the agent runs as a Windows
  service with a watchdog and log rotation")

  Run install_service.bat once on a contributor PC. It asks for administrator permission and:
    1. finds NSSM (nssm.exe) - in PATH, or in a "tools" folder next to this script
    2. registers agent.py (through this folder's venv) as a Windows service that starts
       automatically on boot
    3. configures NSSM's own restart-on-exit as the watchdog: if the agent crashes, NSSM starts
       it again, with a short delay and throttling so a genuine crash-loop backs off instead of
       spinning the CPU
    4. points NSSM's own stdout/stderr capture at rotated log files, as a safety net below the
       agent's own RotatingFileHandler (agent.py::setup_logging) - if the agent crashes before
       its own logging is set up, there's still something to read
  uninstall_service.bat removes the service; nothing else about the PC is changed.

  NSSM itself is not downloaded by this script - it's a small, well-known third-party tool
  (https://nssm.cc), and fetching and running an unreviewed binary isn't something this script
  does on your behalf. Download it yourself, then run this again.
#>
param(
  [switch]$Remove,
  [switch]$Elevated
)
$ErrorActionPreference = "Stop"
$AgentDir    = Split-Path -Parent $PSCommandPath
$ServiceName = "CRMSNodeAgent"
$LogFile     = Join-Path $AgentDir "install_service.log"

function Say([string]$Text, [string]$Color = "Gray") { Write-Host $Text -ForegroundColor $Color }

# Runs a Windows command; returns $true when it worked, prints its message when it did not -
# same helper pattern as setup_sandbox.ps1's Run-Native.
function Run-Native([string]$Exe, [string[]]$Argv) {
  $ErrorActionPreference = "Continue"
  $out = & $Exe @Argv 2>&1
  if ($LASTEXITCODE -ne 0) {
    Say ("   " + $Exe + ": " + (($out | Out-String).Trim())) "Yellow"
    return $false
  }
  return $true
}

function Find-Nssm {
  $cmd = Get-Command "nssm.exe" -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $local = Join-Path $AgentDir "tools\nssm.exe"
  if (Test-Path -LiteralPath $local) { return $local }
  return $null
}

# ------------------------------------------------------------------------------ install
function Install-AgentService {
  Say "CRMS node agent - install as a Windows service" "Cyan"

  $py = Join-Path $AgentDir "venv\Scripts\python.exe"
  if (-not (Test-Path -LiteralPath $py)) {
    throw "The agent is not installed in this folder yet - run setup_agent.bat first."
  }

  $nssm = Find-Nssm
  if (-not $nssm) {
    Say "`nNSSM was not found." "Red"
    Say "Download it from https://nssm.cc/download (the 'win64' build for a 64-bit PC)," "Yellow"
    Say "put nssm.exe at:" "Yellow"
    Say "   $AgentDir\tools\nssm.exe" "Yellow"
    Say "then run install_service.bat again." "Yellow"
    throw "nssm.exe not found"
  }
  Say "   NSSM: $nssm"
  Say "   Agent: $py agent.py"
  Say "   Folder: $AgentDir"

  if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Say "`n   Service '$ServiceName' already exists - reconfiguring it, not creating a duplicate." "Yellow"
    if (-not (Run-Native $nssm @("stop", $ServiceName))) { Say "   (already stopped)" }
  } else {
    if (-not (Run-Native $nssm @("install", $ServiceName, $py, "agent.py"))) {
      throw "nssm install failed"
    }
  }

  # Where and how it runs
  Run-Native $nssm @("set", $ServiceName, "AppDirectory", $AgentDir) | Out-Null
  Run-Native $nssm @("set", $ServiceName, "DisplayName", "CRMS Node Agent") | Out-Null
  Run-Native $nssm @("set", $ServiceName, "Description",
    "CRMS resource-pool node agent - reports this PC's capacity and runs pooled work. Restarted automatically by NSSM if it stops.") | Out-Null
  Run-Native $nssm @("set", $ServiceName, "Start", "SERVICE_AUTO_START") | Out-Null

  # Watchdog: restart on any exit, with a short delay and a throttle so a genuine crash-loop
  # backs off instead of spinning. AppThrottle (ms) is how long the app must stay up to NOT
  # count as a failed start - 10s is generous for Flask + the pool's own startup network calls,
  # short enough that a real crash-loop is still caught and throttled quickly.
  Run-Native $nssm @("set", $ServiceName, "AppExit", "Default", "Restart") | Out-Null
  Run-Native $nssm @("set", $ServiceName, "AppRestartDelay", "5000") | Out-Null
  Run-Native $nssm @("set", $ServiceName, "AppThrottle", "10000") | Out-Null

  # A safety net below agent.py's own RotatingFileHandler: if the agent dies before its own
  # logging is even set up, NSSM's own captured output still lands somewhere, also rotated so
  # it can't grow forever either.
  $stdoutLog = Join-Path $AgentDir "service_stdout.log"
  $stderrLog = Join-Path $AgentDir "service_stderr.log"
  Run-Native $nssm @("set", $ServiceName, "AppStdout", $stdoutLog) | Out-Null
  Run-Native $nssm @("set", $ServiceName, "AppStderr", $stderrLog) | Out-Null
  Run-Native $nssm @("set", $ServiceName, "AppRotateFiles", "1") | Out-Null
  Run-Native $nssm @("set", $ServiceName, "AppRotateOnline", "1") | Out-Null
  Run-Native $nssm @("set", $ServiceName, "AppRotateBytes", "5242880") | Out-Null   # 5MB

  if (-not (Run-Native $nssm @("start", $ServiceName))) {
    throw "the service was installed but failed to start - check Windows Event Viewer (Application log) and $stderrLog"
  }
  Start-Sleep -Seconds 2
  $svc = Get-Service -Name $ServiceName
  Say "`n   Service status: $($svc.Status)" "Green"
  Say "Done. The agent now starts automatically on boot and restarts itself if it crashes." "Green"
  Say "Logs: agent.log (the agent's own) and service_stdout.log/service_stderr.log (NSSM's)." "Gray"
}

# ------------------------------------------------------------------------------ remove
function Remove-AgentService {
  Say "CRMS node agent - remove the Windows service" "Cyan"
  $nssm = Find-Nssm
  $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if (-not $svc) {
    Say "   Service '$ServiceName' is not installed - nothing to do." "Yellow"
    return
  }
  if ($nssm) {
    Run-Native $nssm @("stop", $ServiceName) | Out-Null
    Run-Native $nssm @("remove", $ServiceName, "confirm") | Out-Null
  } else {
    # NSSM isn't on this PC any more, but the service is - sc.exe can still remove it.
    Run-Native "sc.exe" @("stop", $ServiceName) | Out-Null
    Run-Native "sc.exe" @("delete", $ServiceName) | Out-Null
  }
  Say "Done. Run start_agent.bat if you want the agent running manually instead." "Green"
}

# ------------------------------------------------------------------------------ start
if (-not $Elevated) {
  if (Test-Path -LiteralPath $LogFile) { Remove-Item -LiteralPath $LogFile -Force -ErrorAction SilentlyContinue }
  $started = Get-Date
  $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
  $line = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated"
  if ($Remove) { $line += " -Remove" }
  Say "Windows will ask for administrator permission; this continues in a new window..." "Cyan"
  try {
    Start-Process -FilePath "cmd.exe" -Verb RunAs -Wait -ArgumentList "/c `"$line || pause`""
  } catch {
    Say "Administrator permission was not given - nothing was changed." "Red"; exit 1
  }
  if (-not (Test-Path -LiteralPath $LogFile) -or (Get-Item -LiteralPath $LogFile).LastWriteTime -lt $started) {
    Say "`nThe administrator part did not finish - see the window that just opened for the error." "Red"
    exit 1
  }
  exit 0
}

try { Start-Transcript -LiteralPath $LogFile -Force | Out-Null } catch { }
try {
  if ($Remove) { Remove-AgentService } else { Install-AgentService }
} catch {
  Say ""
  Say ("ERROR: " + $_.Exception.Message) "Red"
  Say $_.InvocationInfo.PositionMessage "DarkGray"
}
try { Stop-Transcript | Out-Null } catch { }
Say ""
Read-Host "Press Enter to close this window" | Out-Null
exit 0

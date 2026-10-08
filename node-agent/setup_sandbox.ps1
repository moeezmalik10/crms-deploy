<#
  CRMS - Windows sandbox for pool jobs (no Docker needed)

  Run setup_sandbox.bat once on a contributor PC. It asks for administrator permission and:
    1. creates a few hidden, low-privilege Windows accounts (crms_sb1 ... crms_sb4) in the group
       "CRMS Sandbox" - pool jobs run as one of these accounts, never as you
    2. makes a work folder (C:\ProgramData\CRMS\sandbox) that only you and, per job, one sandbox
       account can use
    3. lets the accounts start Python (and g++ if installed) - nothing else is added
    4. blocks the accounts from this agent folder (device key) and, if you agree, from other
       drives and folders; your user folder (Desktop, Documents...) is private already
    5. blocks their network access with a Windows Firewall rule
    6. limits how much disk they can fill (Windows disk quotas)
    7. saves the setup for the agent (passwords encrypted with Windows DPAPI)
  The agent then runs each job as one of these accounts inside a Windows Job Object that caps
  CPU, memory and processes. remove_sandbox.bat undoes everything.
#>
param(
  [string]$AgentSid = "",
  [string]$PythonDir = "",
  [string]$GppPath = "",
  [int]$Accounts = 4,
  [int]$QuotaGB = 10,
  [switch]$Remove,
  [switch]$Elevated
)
$ErrorActionPreference = "Stop"
$AgentDir    = Split-Path -Parent $PSCommandPath
$GroupName   = "CRMS Sandbox"
$Prefix      = "crms_sb"
$WorkRoot    = Join-Path $env:ProgramData "CRMS\sandbox"
$ConfigFile  = Join-Path $AgentDir "sandbox_users.json"
$RulePrefix  = "CRMS-Sandbox"
$LogFile     = Join-Path $AgentDir "sandbox_setup.log"
$UserListKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"

function Say([string]$Text, [string]$Color = "Gray") { Write-Host $Text -ForegroundColor $Color }

function Ask([string]$Question) {
  $a = Read-Host "$Question [Y/n]"
  return ($a -eq "" -or $a -match '^(y|yes)$')
}

# Runs a Windows command; returns $true when it worked, prints its message when it did not
function Run-Native([string]$Exe, [string[]]$Argv) {
  $ErrorActionPreference = "Continue"
  $out = & $Exe @Argv 2>&1
  if ($LASTEXITCODE -ne 0) {
    Say ("   " + $Exe + ": " + (($out | Out-String).Trim())) "Yellow"
    return $false
  }
  return $true
}

function Add-GroupMemberSafe([string]$Name = "", [string]$Sid = "", [string]$Member) {
  try {
    if ($Sid) { Add-LocalGroupMember -SID $Sid -Member $Member -ErrorAction Stop }
    else { Add-LocalGroupMember -Name $Name -Member $Member -ErrorAction Stop }
  } catch {
    if ($_.FullyQualifiedErrorId -notlike "MemberExists*") { throw }
  }
}

function New-Password {
  $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!#%+-=?@_'
  $bytes = New-Object byte[] 28
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return "Aa1!" + (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] }))
}

function Get-QuotaState([string]$Volume) {
  $q = Get-CimInstance Win32_QuotaSetting -ErrorAction SilentlyContinue | Where-Object { $_.VolumePath -eq "$Volume\" }
  if ($q) { return [int]$q.State }
  return -1
}

# ------------------------------------------------------------------------------ install
function Install-Sandbox {
  Say "CRMS Windows sandbox - setup" "Cyan"
  Say "Agent folder : $AgentDir"
  Say "Python       : $PythonDir"
  if ([Environment]::OSVersion.Version.Major -lt 10) { throw "Windows 10 or 11 is needed." }
  $pyExe = Join-Path $PythonDir "python.exe"
  if (-not (Test-Path -LiteralPath $pyExe)) { throw "Python was not found at $pyExe" }
  if ($PythonDir -like "*\WindowsApps\*") {
    throw "This PC uses the Microsoft Store Python, which other accounts cannot start. Install Python from python.org, run setup_agent.bat again, then this setup."
  }
  Add-Type -AssemblyName System.Security

  # 1. accounts ---------------------------------------------------------------
  Say "`n[1/7] Sandbox accounts" "Cyan"
  if (-not (Get-LocalGroup -Name $GroupName -ErrorAction SilentlyContinue)) {
    New-LocalGroup -Name $GroupName -Description "CRMS pool jobs run as these accounts" | Out-Null
  }
  $gsid = (Get-LocalGroup -Name $GroupName).SID.Value
  if (-not (Test-Path $UserListKey)) { New-Item -Path $UserListKey -Force | Out-Null }
  $users = @()
  for ($i = 1; $i -le $Accounts; $i++) {
    $name = "$Prefix$i"
    $pw = New-Password
    $sec = ConvertTo-SecureString $pw -AsPlainText -Force
    if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) {
      Set-LocalUser -Name $name -Password $sec -PasswordNeverExpires $true -AccountNeverExpires
      Enable-LocalUser -Name $name
    } else {
      New-LocalUser -Name $name -Password $sec -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires `
        -Description "CRMS pool sandbox account (no admin)" | Out-Null
    }
    Add-GroupMemberSafe -Name $GroupName -Member $name
    Add-GroupMemberSafe -Sid "S-1-5-32-545" -Member $name          # Users: may sign in locally, nothing more
    # never an administrator - check membership first so there is nothing to swallow; if a
    # removal that IS needed fails, that is a real problem and must stop setup, not be hidden
    if (Get-LocalGroupMember -SID "S-1-5-32-544" -Member $name -ErrorAction SilentlyContinue) {
      Remove-LocalGroupMember -SID "S-1-5-32-544" -Member $name -ErrorAction Stop
    }
    New-ItemProperty -Path $UserListKey -Name $name -Value 0 -PropertyType DWord -Force | Out-Null  # hidden from the sign-in screen
    $blob = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($pw),
              [Text.Encoding]::UTF8.GetBytes("crms-sandbox"), [Security.Cryptography.DataProtectionScope]::LocalMachine)
    $users += [ordered]@{ name = $name; sid = (Get-LocalUser -Name $name).SID.Value; secret = [Convert]::ToBase64String($blob) }
    Say "   $name ready (standard user, hidden, random password nobody sees)"
  }

  # 2. work folder ------------------------------------------------------------
  Say "`n[2/7] Work folder $WorkRoot" "Cyan"
  if (Test-Path -LiteralPath $WorkRoot) {
    Get-ChildItem -LiteralPath $WorkRoot -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  }
  New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
  if (-not (Run-Native "icacls.exe" @($WorkRoot, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
                                     "*S-1-5-32-544:(OI)(CI)F", "*${AgentSid}:(OI)(CI)F", "/Q"))) {
    throw "could not set the permissions of $WorkRoot"
  }
  Say "   only you and administrators; each job's folder is opened to one sandbox account"

  # 3. Python / g++ -----------------------------------------------------------
  Say "`n[3/7] Programs the sandbox may start" "Cyan"
  if (Run-Native "icacls.exe" @($PythonDir, "/grant", "*${gsid}:(OI)(CI)RX", "/Q")) { Say "   Python: $PythonDir" }
  $gppRoot = ""
  if ($GppPath -and (Test-Path -LiteralPath $GppPath)) {
    $gppRoot = Split-Path -Parent (Split-Path -Parent $GppPath)
    if (Run-Native "icacls.exe" @($gppRoot, "/grant", "*${gsid}:(OI)(CI)RX", "/Q")) { Say "   g++   : $GppPath" }
  } else {
    $GppPath = ""
    Say "   no g++ found - C++ jobs need MinGW g++ on this PC (Python jobs work)" "Yellow"
  }

  # 4. keep the sandbox out of the owner's files --------------------------------
  Say "`n[4/7] Keep the sandbox out of your files" "Cyan"
  $protected = @()
  if (-not (Run-Native "icacls.exe" @($AgentDir, "/deny", "*${gsid}:(OI)(CI)F", "/Q"))) {
    throw "could not block the sandbox from $AgentDir (device key, sandbox passwords) - setup stopped, nothing is marked configured"
  }
  $protected += $AgentDir
  Say "   blocked: $AgentDir (agent, device key, sandbox passwords)"
  Say "   your user folder (Desktop, Documents, Downloads, ...) is private to you already"
  if ($env:PUBLIC -and (Test-Path -LiteralPath $env:PUBLIC)) {
    if (Run-Native "icacls.exe" @($env:PUBLIC, "/deny", "*${gsid}:(OI)(CI)F", "/C", "/Q")) {
      $protected += $env:PUBLIC; Say "   blocked: $env:PUBLIC (the shared Public folder)"
    }
  }
  $sysRoot = $env:SystemDrive + "\"
  $skip = @("Windows", "Program Files", "Program Files (x86)", "ProgramData", "Users", "PerfLogs", "Recovery",
            "System Volume Information", '$Recycle.Bin', '$WinREAgent', "Config.Msi", "Documents and Settings",
            "OneDriveTemp", "Boot")
  $needed = @($PythonDir, $gppRoot) | Where-Object { $_ }
  $rootDirs = @(Get-ChildItem -LiteralPath $sysRoot -Directory -Force -ErrorAction SilentlyContinue | Where-Object {
      $d = $_.FullName
      ($skip -notcontains $_.Name) -and
      -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
      -not ($needed | Where-Object { $_ -eq $d -or $_.StartsWith($d + "\", [StringComparison]::OrdinalIgnoreCase) })
  })
  if ($rootDirs.Count) {
    Say ("   folders on $sysRoot that every account can normally open: " + (($rootDirs | ForEach-Object { $_.Name }) -join ", "))
    if (Ask "   Block the sandbox from these folders?") {
      foreach ($d in $rootDirs) {
        if (Run-Native "icacls.exe" @($d.FullName, "/deny", "*${gsid}:(OI)(CI)F", "/C", "/Q")) {
          $protected += $d.FullName; Say "   blocked: $($d.FullName)"
        }
      }
    }
  }
  $drives = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Where-Object { $_.DeviceID -ne $env:SystemDrive })
  foreach ($dr in $drives) {
    $root = $dr.DeviceID + "\"
    if ($dr.FileSystem -ne "NTFS") {
      Say "   $root uses $($dr.FileSystem), which has no permissions - keep private files off it while sharing" "Yellow"
      continue
    }
    if (Ask "   Block the sandbox from drive $root ? (can take a few minutes on a full drive)") {
      Say "   working on $root ..."
      if (Run-Native "icacls.exe" @($root, "/deny", "*${gsid}:(OI)(CI)F", "/C", "/Q")) {
        $protected += $root; Say "   blocked: $root"
      }
    }
  }

  # 5. network ----------------------------------------------------------------
  Say "`n[5/7] No network for the sandbox (Windows Firewall)" "Cyan"
  Get-NetFirewallRule -Name "$RulePrefix-*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  $sddl = "D:" + (($users | ForEach-Object { "(A;;CC;;;" + $_.sid + ")" }) -join "")
  New-NetFirewallRule -Name "$RulePrefix-Out" -DisplayName "CRMS sandbox - block network (outbound)" `
    -Direction Outbound -Action Block -LocalUser $sddl -Profile Any | Out-Null
  try {
    New-NetFirewallRule -Name "$RulePrefix-In" -DisplayName "CRMS sandbox - block network (inbound)" `
      -Direction Inbound -Action Block -LocalUser $sddl -Profile Any | Out-Null
  } catch { Say "   inbound rule not supported here (outbound block is what matters)" "Yellow" }
  Say "   rule added: the sandbox accounts cannot reach the internet or your network"
  $off = @(Get-NetFirewallProfile | Where-Object { -not $_.Enabled })
  if ($off.Count) {
    throw ("Windows Firewall is OFF for: " + (($off | ForEach-Object { $_.Name }) -join ", ") +
           " - the network block rule only works while Firewall is on. Turn Firewall on for " +
           "that profile and run setup_sandbox.bat again; nothing is marked configured.")
  }

  # 6. disk quota -------------------------------------------------------------
  Say "`n[6/7] Disk limit: $QuotaGB GB per sandbox account (Windows disk quotas)" "Cyan"
  $quota = @()
  $limit = [int64]$QuotaGB * 1GB
  $warn = [int64]($limit * 0.9)
  $vols = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Where-Object { $_.FileSystem -eq "NTFS" })
  foreach ($v in $vols) {
    $before = Get-QuotaState $v.DeviceID
    if ($before -ne 2) {
      if (-not (Run-Native "fsutil.exe" @("quota", "enforce", $v.DeviceID))) {
        Say "   $($v.DeviceID): could not turn on disk quotas - sandbox jobs have NO disk limit on this volume" "Yellow"
        continue
      }
    }
    $ok = $true
    foreach ($u in $users) {
      if (-not (Run-Native "fsutil.exe" @("quota", "modify", $v.DeviceID, "$warn", "$limit", "$env:COMPUTERNAME\$($u.name)"))) { $ok = $false }
    }
    $quota += [ordered]@{ volume = $v.DeviceID; previous_state = $before }
    if ($ok) { Say "   $($v.DeviceID) limited" } else { Say "   $($v.DeviceID): quota only partly applied - some sandbox accounts have NO disk limit on this volume" "Yellow" }
  }

  # 7. save -------------------------------------------------------------------
  Say "`n[7/7] Save" "Cyan"
  $svc = Get-Service seclogon -ErrorAction SilentlyContinue
  if ($svc -and $svc.StartType -eq "Disabled") {
    Set-Service seclogon -StartupType Manual
    Say "   enabled the Windows service 'Secondary Logon' (starts programs as another account)"
  }
  $cfg = [ordered]@{
    version = 1; created = (Get-Date).ToString("s"); work_root = $WorkRoot; python = $pyExe; gpp = $GppPath
    gpp_root = $gppRoot; python_dir = $PythonDir; group_sid = $gsid; agent_sid = $AgentSid
    users = $users; protected = $protected; quota = $quota; quota_gb = $QuotaGB
  }
  ($cfg | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $ConfigFile -Encoding UTF8
  Run-Native "icacls.exe" @($ConfigFile, "/inheritance:r", "/grant:r", "*S-1-5-18:F", "*S-1-5-32-544:F", "*${AgentSid}:F", "/Q") | Out-Null
  Say "   $ConfigFile (only you can read it)"
  Say "`nDone. This window closes and the sandbox is tested next." "Green"
}

# ------------------------------------------------------------------------------ remove
function Remove-Sandbox {
  Say "CRMS Windows sandbox - remove" "Cyan"
  $cfg = $null
  if (Test-Path -LiteralPath $ConfigFile) { try { $cfg = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json } catch { } }

  Get-NetFirewallRule -Name "$RulePrefix-*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Say "   firewall rules removed"

  $grp = Get-LocalGroup -Name $GroupName -ErrorAction SilentlyContinue
  if ($grp) {
    $gsid = $grp.SID.Value
    $paths = @()
    if ($cfg) { $paths = @($cfg.protected) + @($cfg.python_dir, $cfg.gpp_root) }
    $paths += $AgentDir
    foreach ($p in ($paths | Where-Object { $_ } | Select-Object -Unique)) {
      if (Test-Path -LiteralPath $p) {
        Say "   removing sandbox permissions from $p ..."
        Run-Native "icacls.exe" @($p, "/remove", "*$gsid", "/C", "/Q") | Out-Null
      }
    }
  }

  foreach ($u in @(Get-LocalUser -Name "$Prefix*" -ErrorAction SilentlyContinue)) {
    $sid = $u.SID.Value
    try {
      Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid } | Remove-CimInstance -ErrorAction Stop
    } catch { Say "   could not delete the profile of $($u.Name) (stop the agent first): $($_.Exception.Message)" "Yellow" }
    Remove-LocalUser -SID $u.SID
    Remove-ItemProperty -Path $UserListKey -Name $u.Name -ErrorAction SilentlyContinue
    Say "   account $($u.Name) removed"
  }
  if ($grp) { Remove-LocalGroup -SID $grp.SID; Say "   group '$GroupName' removed" }

  if ($cfg) {
    foreach ($q in @($cfg.quota)) {
      if (-not $q.volume) { continue }
      if ($q.previous_state -eq 0) { Run-Native "fsutil.exe" @("quota", "disable", $q.volume) | Out-Null; Say "   disk quotas switched off again on $($q.volume)" }
      elseif ($q.previous_state -eq 1) { Run-Native "fsutil.exe" @("quota", "track", $q.volume) | Out-Null }
    }
  }
  if (Test-Path -LiteralPath $WorkRoot) { Remove-Item -LiteralPath $WorkRoot -Recurse -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $ConfigFile) { Remove-Item -LiteralPath $ConfigFile -Force }
  Say "`nDone. Pool jobs on this PC need Docker (or the light sandbox) again." "Green"
}

# ------------------------------------------------------------------------------ start
if (-not $Elevated) {
  # Runs as you: note your account and the Python jobs will use, then restart as administrator
  if (-not $AgentSid) { $AgentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
  $py = Join-Path $AgentDir "venv\Scripts\python.exe"
  if (-not $Remove) {
    if (-not (Test-Path -LiteralPath $py)) { Say "The agent is not installed in this folder yet - run setup_agent.bat first." "Red"; exit 1 }
    $PythonDir = (& $py -c "import sys; print(sys.base_prefix)" | Out-String).Trim().TrimEnd('\')
    $g = Get-Command g++.exe -ErrorAction SilentlyContinue
    if ($g) { $GppPath = $g.Source }
  }
  $AgentSid = $AgentSid.Trim()
  # Files downloaded from the internet are marked as such; clear that so Windows lets them run
  Get-ChildItem -LiteralPath $AgentDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in ".ps1", ".bat", ".py" } | Unblock-File -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $LogFile) { Remove-Item -LiteralPath $LogFile -Force -ErrorAction SilentlyContinue }
  $started = Get-Date

  # The administrator part runs in its own window through cmd.exe, so that even if PowerShell
  # fails before the script starts, the window stays open ("pause") and the message can be read.
  $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
  $line = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated -AgentSid $AgentSid -Accounts $Accounts -QuotaGB $QuotaGB"
  if ($PythonDir) { $line += " -PythonDir `"$PythonDir`"" }
  if ($GppPath)   { $line += " -GppPath `"$GppPath`"" }
  if ($Remove)    { $line += " -Remove" }
  Say "Windows will ask for administrator permission; the setup continues in a new window..." "Cyan"
  try {
    Start-Process -FilePath "cmd.exe" -Verb RunAs -Wait -ArgumentList "/c `"$line || pause`""
  } catch {
    Say "Administrator permission was not given - nothing was changed." "Red"; exit 1
  }
  $finished = if ($Remove) { -not (Test-Path -LiteralPath $ConfigFile) }
              else { (Test-Path -LiteralPath $ConfigFile) -and ((Get-Item -LiteralPath $ConfigFile).LastWriteTime -ge $started) }
  if (-not $finished) {
    Say "`nThe administrator part did not finish." "Red"
    if (Test-Path -LiteralPath $LogFile) {
      Say "Last lines of $LogFile :" "Yellow"
      Get-Content -LiteralPath $LogFile -Tail 25 | ForEach-Object { Say "   $_" }
    } else {
      Say "It stopped before it could write $LogFile - send a photo of the administrator window." "Yellow"
    }
    exit 1
  }
  if ($Remove) { Say "The Windows sandbox was removed. Restart the agent." "Green"; exit 0 }
  Say "`nTesting the sandbox (about 30 seconds)..." "Cyan"
  & $py (Join-Path $AgentDir "win_sandbox.py") --test
  exit $LASTEXITCODE
}

try { Start-Transcript -LiteralPath $LogFile -Force | Out-Null } catch { }
try {
  if ($Remove) { Remove-Sandbox } else { Install-Sandbox }
} catch {
  Say ""
  Say ("ERROR: " + $_.Exception.Message) "Red"
  Say $_.InvocationInfo.PositionMessage "DarkGray"
}
try { Stop-Transcript | Out-Null } catch { }
Say ""
Read-Host "Press Enter to close this window" | Out-Null
exit 0     # the first window checks the result itself

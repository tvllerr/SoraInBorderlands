<#
Sora in Borderlands - one-click installer. Started by INSTALL.bat (and UNINSTALL.bat with -Remove).

What it does, in order:
  1. finds Borderlands GOTY Enhanced and KINGDOM HEARTS HD 1.5+2.5 ReMIX through Steam (or asks for the folders)
  2. installs the Borderlands mod loader (willow1 PythonSDK v2.3, from github.com/bl-sdk) if it isn't there yet,
     plus the Microsoft Visual C++ runtime if Windows doesn't have it
  3. unpacks the KH1 data files it needs from YOUR KH1 install (KHPCPatchManager, from github.com/AntonioDePau)
  4. builds Sora, Donald, Goofy, the Heartless, summons, weapons, menus, effects and sounds from them (uv + Python,
     from astral.sh, downloaded into the temporary folder)
  5. copies the mod into Borderlands and deletes the temporary folder
It never changes Kingdom Hearts or your saves, and nothing from Kingdom Hearts is shipped in the zip.
#>
param([switch]$Remove, [switch]$FixLauncher, [switch]$Update, [switch]$KeepTemp, [string]$BorderlandsDir = "", [string]$KingdomHeartsDir = "",
      [switch]$Melty, [string]$DoneFile = "")
# -Melty: Melty (melty.gg) already placed the mod loader and the mod code into Borderlands; this only builds the KH1
# files from the player's own install into sdk_mods\sora\data, asks nothing, and writes $DoneFile when finished.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is very slow with its progress bar on
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
$ModSrc = Join-Path $Root "mod"
if ($Melty) { try { Start-Transcript -Path (Join-Path $Root "setup.log") -Force | Out-Null } catch { } }

$SDK_URL = "https://github.com/bl-sdk/willow1-mod-manager/releases/download/v2.3/bl1-enhanced-sdk.zip"
$KHPC_URL = "https://github.com/AntonioDePau/KHPCPatchManager/releases/download/v1.2.1/KHPCPatchManager.exe"
$UV_URL = "https://github.com/astral-sh/uv/releases/download/0.12.19/uv-x86_64-pc-windows-msvc.zip"
$VCRT_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$KH1_SHA1 = "E01FA57BC8DA71222E17E35926394C832BCE8107"   # Steam KINGDOM HEARTS FINAL MIX.exe the mod was made for
$KH1_PARTS = @("kh1_first", "kh1_third", "kh1_fourth", "kh1_fifth")
$REPO = "tvllerr/SoraInBorderlands"                         # GitHub repo UPDATE.bat checks (releases/latest)
$NEED_GB = 25

function Say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }
function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Fail($t) {
    Write-Host ""; Write-Host "INSTALL STOPPED: $t" -ForegroundColor Red
    Write-Host "Nothing is broken - fix the problem above and run INSTALL.bat again (it picks up where it stopped)." -ForegroundColor Yellow
    exit 1
}
function Ask-YesNo($q) {
    if ($Melty) { Say "$q -> yes (Melty install)"; return $true }
    while ($true) {
        $a = (Read-Host "$q [Y/N]").Trim().ToUpper()
        if ($a -eq "Y" -or $a -eq "YES") { return $true }
        if ($a -eq "N" -or $a -eq "NO") { return $false }
    }
}
function Download($url, $dest) {
    if (Test-Path $dest) { return }
    Say "   downloading $(Split-Path -Leaf $dest) ..."
    try { Invoke-WebRequest $url -OutFile "$dest.part" -UseBasicParsing } catch { Fail "could not download $url ($($_.Exception.Message)). Check your internet connection." }
    Move-Item "$dest.part" $dest -Force
}

function Steam-Libraries {
    $libs = @()
    foreach ($k in @("HKCU:\Software\Valve\Steam", "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam")) {
        $p = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
        foreach ($v in @($p.SteamPath, $p.InstallPath)) { if ($v) { $libs += ($v -replace "/", "\") } }
    }
    foreach ($s in @($libs)) {
        $vdf = Join-Path $s "steamapps\libraryfolders.vdf"
        if (Test-Path $vdf) {
            foreach ($m in [regex]::Matches((Get-Content $vdf -Raw), '"path"\s+"([^"]+)"')) { $libs += ($m.Groups[1].Value -replace '\\\\', '\') }
        }
    }
    return $libs | Select-Object -Unique
}
function Find-Game($folder, $marker) {
    foreach ($l in Steam-Libraries) {
        $d = Join-Path $l "steamapps\common\$folder"
        if (Test-Path (Join-Path $d $marker)) { return $d }
    }
    return $null
}
function Pick-Folder($what, $marker) {
    Say "   Couldn't find $what automatically. A folder window will open - pick the game's install folder." "Yellow"
    Say "   (Steam: right-click the game > Manage > Browse local files shows where it is.)" "Yellow"
    Add-Type -AssemblyName System.Windows.Forms
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "Pick the $what install folder"
    if ($dlg.ShowDialog() -ne "OK") { Fail "no folder picked for $what." }
    if (-not (Test-Path (Join-Path $dlg.SelectedPath $marker))) { Fail "that folder doesn't look like $what (no $marker in it)." }
    return $dlg.SelectedPath
}

# Borderlands' 2K launcher (the small .NET Launcher.exe) crashes before the game starts on PCs with a virtual display
# adapter listed first (MuMu / Meta Virtual Monitor / Parsec / spacedesk...: PNPDeviceID ROOT\DISPLAY\0000 ->
# "Unable to parse PNPDeviceID" -> "Initialization error"). Not caused by the mod, but we skip the launcher so it can't
# happen: Launcher.exe becomes a copy of the game exe (PCGamingWiki's skip-launcher fix), original kept as
# Launcher_Backup.exe. Steam's "Verify integrity" puts the launcher back -> FIX_LAUNCHER.bat / INSTALL.bat redo this.
function Skip-Launcher($win64) {
    $launcher = Join-Path $win64 "Launcher.exe"
    $game = Join-Path $win64 "BorderlandsGOTY.exe"
    $backup = Join-Path $win64 "Launcher_Backup.exe"
    if ((Get-Item $launcher).Length -gt 10MB) { Say "   the 2K launcher is already skipped (Launcher.exe starts the game directly)"; return }
    if (-not (Test-Path $game)) { Say "   WARNING: BorderlandsGOTY.exe not found - leaving the 2K launcher as it is" "Yellow"; return }
    Copy-Item $launcher $backup -Force
    Copy-Item $game $launcher -Force
    Set-Content (Join-Path $win64 "sora_skip_launcher.txt") "Sora in Borderlands: Launcher.exe = copy of BorderlandsGOTY.exe, the 2K launcher is Launcher_Backup.exe. UNINSTALL.bat puts it back."
    Say "   skipped the 2K launcher (it crashes on PCs with virtual display adapters; original kept as Launcher_Backup.exe)"
}

# Fingerprint of everything that builds the KH1 files on the player's PC. An update with the same fingerprint only
# needs the new mod code; a different one means the KH1 files must be rebuilt (full install).
function KH-Build-Stamp($installerDir) {
    $files = @(Get-Item (Join-Path $installerDir "build_kh1_files.py"), (Join-Path $installerDir "kh1_sounds.json")) +
             @(Get-ChildItem (Join-Path $installerDir "buildtools") -Recurse -File | Sort-Object FullName)
    $sb = New-Object Text.StringBuilder
    foreach ($f in $files) { [void]$sb.Append($f.FullName.Substring($installerDir.Length)).Append((Get-FileHash $f.FullName -Algorithm SHA1).Hash) }
    $sha = [Security.Cryptography.SHA1]::Create()
    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString().ToLower())))).Replace("-", "")
}
# The player's config.json values on top of the new one (new settings arrive, their own choices stay).
function Merge-Config($cfgPath, $keepCfg) {
    try {
        $mine = $keepCfg | ConvertFrom-Json
        $merged = Get-Content $cfgPath -Raw | ConvertFrom-Json
        foreach ($prop in $mine.PSObject.Properties) { $merged | Add-Member -NotePropertyName $prop.Name -NotePropertyValue $prop.Value -Force }
        [IO.File]::WriteAllText($cfgPath, ($merged | ConvertTo-Json -Depth 20))
        Say "   kept your config.json settings (new settings added)"
    } catch {
        [IO.File]::WriteAllText($cfgPath, $keepCfg)
        Say "   kept your config.json settings"
    }
}

Write-Host ""
Write-Host "  SORA IN BORDERLANDS - installer" -ForegroundColor Green
Write-Host "  (Kingdom Hearts Final Mix x Borderlands GOTY Enhanced passthrough mod)" -ForegroundColor Green

if ($Here -match "\\AppData\\Local\\Temp\\") {
    Fail "you're running this from inside the zip. Right-click the zip > Extract All..., then open the extracted folder and double-click INSTALL.bat."
}

# ---------------------------------------------------------------------------------------------- 1. find the games
Step "Finding your games"
$BL = if ($BorderlandsDir) { $BorderlandsDir } else { Find-Game "BorderlandsGOTYEnhanced" "Binaries\Win64\Launcher.exe" }
if (-not $BL) { $BL = Pick-Folder "Borderlands Game of the Year Enhanced" "Binaries\Win64\Launcher.exe" }
Say "   Borderlands:     $BL"
$Plugins = Join-Path $BL "Binaries\Win64\Plugins"
$ModDir = Join-Path $BL "sdk_mods\sora"
$Toml = Join-Path $Plugins "unrealsdk.toml"
$Win64 = Join-Path $BL "Binaries\Win64"

if ($FixLauncher) {
    Step "Skipping the Borderlands 2K launcher"
    if (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($BL, "OrdinalIgnoreCase") }) { Fail "Borderlands is running - close it first." }
    Skip-Launcher $Win64
    Say ""
    Say "Done. Start Borderlands from Steam as usual." "Green"
    exit 0
}

# ---------------------------------------------------------------------------------------------- update (UPDATE.bat)
# Latest GitHub release -> if only the mod code changed: copy it over, keep the KH1 files built on this PC (seconds);
# if the KH1 build steps changed: run the full install from the new package. config.json settings are kept either way.
if ($Update) {
    Step "Checking for an update"
    $marker = Join-Path $ModDir "installed_by_setup.txt"
    if (-not (Test-Path $marker)) { Fail "Sora in Borderlands isn't installed in $BL yet - run INSTALL.bat first." }
    $verFile = Join-Path $ModDir "VERSION.txt"
    $have = if (Test-Path $verFile) { (Get-Content $verFile -Raw).Trim() } else { "unknown" }
    $api = if ($env:SORA_UPDATE_API) { $env:SORA_UPDATE_API } else { "https://api.github.com/repos/$REPO/releases/latest" }   # env = local test server
    try { $rel = Invoke-RestMethod $api -Headers @{ "User-Agent" = "SoraInBorderlands-updater" } }
    catch { Fail "couldn't reach GitHub ($($_.Exception.Message)). Check your internet connection." }
    $want = ($rel.tag_name -replace '^v', '').Trim()
    Say "   installed: $have    latest: $want"
    if ($want -eq $have) { Say ""; Say "You already have the latest version." "Green"; exit 0 }
    $asset = $rel.assets | Where-Object { $_.name -like "*.zip" } | Select-Object -First 1
    if (-not $asset) { Fail "the latest release ($($rel.tag_name)) has no zip attached." }
    # KH1 too: it has the mod's kh1_fx.dll loaded from sdk_mods\sora\data (a local test server run skips that check)
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object { ($_.Path -and $_.Path.StartsWith($BL, "OrdinalIgnoreCase")) -or ($_.ProcessName -like "KINGDOM HEARTS*" -and -not $env:SORA_UPDATE_API) }
    if ($running -and -not $Melty) { Fail "close Borderlands and Kingdom Hearts first ($(($running | ForEach-Object Name | Select-Object -Unique) -join ', ') still running)." }
    $upd = Join-Path $Root "_update"
    if (Test-Path $upd) { Remove-Item $upd -Recurse -Force }
    New-Item -ItemType Directory -Force $upd | Out-Null
    Download $asset.browser_download_url (Join-Path $upd "package.zip")
    Expand-Archive (Join-Path $upd "package.zip") -DestinationPath (Join-Path $upd "pkg") -Force
    $pkg = Get-ChildItem (Join-Path $upd "pkg") -Recurse -Filter "INSTALL.bat" | Select-Object -First 1
    if (-not $pkg) { Fail "the downloaded zip doesn't look like a Sora in Borderlands package (no INSTALL.bat)." }
    $pkgRoot = $pkg.DirectoryName
    # this folder becomes the new package (new INSTALL / UNINSTALL / UPDATE and installer for next time)
    robocopy $pkgRoot $Root /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "couldn't copy the new package into $Root (robocopy code $LASTEXITCODE)." }
    $stampFile = Join-Path $ModDir "kh_build_stamp.txt"
    $oldStamp = if (Test-Path $stampFile) { (Get-Content $stampFile -Raw).Trim() } else { "" }
    $newStamp = KH-Build-Stamp $Here
    if ($oldStamp -and $oldStamp -eq $newStamp) {
        Step "Updating the mod to $want (your KH1 files stay as they are)"
        $keepCfg = $null
        if (Test-Path (Join-Path $ModDir "config.json")) { $keepCfg = Get-Content (Join-Path $ModDir "config.json") -Raw }
        robocopy (Join-Path $ModSrc "sdk_mods\sora") $ModDir /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { Fail "couldn't copy the new mod files (robocopy code $LASTEXITCODE)." }
        Copy-Item (Join-Path $ModSrc "Binaries\Win64\Plugins\bl1e_exe_fix.py") $Plugins -Force
        if ($keepCfg) { Merge-Config (Join-Path $ModDir "config.json") $keepCfg }
        Set-Content $marker "Sora in Borderlands, updated to $want $(Get-Date -Format s). UNINSTALL.bat removes this whole folder."
        Remove-Item $upd -Recurse -Force -ErrorAction SilentlyContinue
        Say ""; Say "Updated to $want." "Green"
        exit 0
    }
    Say "   this update changes how the KH1 files are built - running the full install (5-10 minutes)..." "Yellow"
    Remove-Item $upd -Recurse -Force -ErrorAction SilentlyContinue
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Here "setup.ps1") -BorderlandsDir $BL
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------------------------- uninstall
if ($Remove) {
    Step "Removing Sora in Borderlands"
    if (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($BL, "OrdinalIgnoreCase") }) { Fail "Borderlands is running - close it first." }
    if (Test-Path $ModDir) { Remove-Item $ModDir -Recurse -Force; Say "   removed sdk_mods\sora" }
    $fix = Join-Path $Plugins "bl1e_exe_fix.py"
    if (Test-Path $fix) { Remove-Item $fix -Force; Say "   removed bl1e_exe_fix.py" }
    if (Test-Path "$Toml.orig") { Copy-Item "$Toml.orig" $Toml -Force; Remove-Item "$Toml.orig" -Force; Say "   restored unrealsdk.toml" }
    $skipMark = Join-Path $Win64 "sora_skip_launcher.txt"
    if ((Test-Path $skipMark) -and (Test-Path (Join-Path $Win64 "Launcher_Backup.exe"))) {
        Copy-Item (Join-Path $Win64 "Launcher_Backup.exe") (Join-Path $Win64 "Launcher.exe") -Force
        Remove-Item (Join-Path $Win64 "Launcher_Backup.exe"), $skipMark -Force
        Say "   put the 2K launcher back (Launcher.exe)"
    }
    Say ""
    Say "Done. The Borderlands mod loader (Mods menu) is still installed - it's harmless on its own." "Green"
    Say "To remove that too: Steam > Borderlands GOTY Enhanced > Properties > Installed Files > Verify integrity, then delete" "Green"
    Say "Binaries\Win64\dinput8.dll and Binaries\Win64\Plugins from the game folder." "Green"
    exit 0
}

$KH = if ($KingdomHeartsDir) { $KingdomHeartsDir } else { Find-Game "KINGDOM HEARTS -HD 1.5+2.5 ReMIX-" "Image\dt\kh1_first.hed" }
if (-not $KH) { $KH = Pick-Folder "KINGDOM HEARTS HD 1.5+2.5 ReMIX" "Image\dt\kh1_first.hed" }
Say "   Kingdom Hearts:  $KH"

$running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path.StartsWith($BL, "OrdinalIgnoreCase") -or $_.Path.StartsWith($KH, "OrdinalIgnoreCase")) }
if ($running -and -not $Melty) { Fail "close Borderlands and Kingdom Hearts first ($(($running | ForEach-Object Name | Select-Object -Unique) -join ', ') still running)." }

$kh1exe = Join-Path $KH "KINGDOM HEARTS FINAL MIX.exe"
if (-not (Test-Path $kh1exe)) { Fail "KINGDOM HEARTS FINAL MIX.exe is missing from $KH." }
if ((Get-FileHash $kh1exe -Algorithm SHA1).Hash -ne $KH1_SHA1) {
    Say "   WARNING: your KINGDOM HEARTS FINAL MIX.exe is a different version than the one this mod was made for" "Yellow"
    Say "   (Steam version 1.0.0.2). The install will work, but Sora may not appear or KH1 may crash in game." "Yellow"
    if (-not (Ask-YesNo "   Install anyway?")) { exit 1 }
}

# ---------------------------------------------------------------------------------------------- space + temp folder
$drive = [IO.Path]::GetPathRoot($KH)
$Tmp = Join-Path $drive "SoraInBorderlands_setup"
$freeGB = [math]::Floor((New-Object IO.DriveInfo $drive).AvailableFreeSpace / 1GB)
$haveExtract = @($KH1_PARTS | Where-Object { Test-Path (Join-Path $Tmp "kh1_extract\$_\remastered") }).Count
if ($freeGB -lt $NEED_GB -and $haveExtract -lt $KH1_PARTS.Count) {
    Fail "drive $drive has $freeGB GB free; the install needs about $NEED_GB GB there for a while (it's all freed again at the end)."
}
New-Item -ItemType Directory -Force $Tmp | Out-Null
Say "   temporary folder: $Tmp ($freeGB GB free on $drive)"

# ---------------------------------------------------------------------------------------------- 2. mod loader + VC++ runtime
Step "Borderlands mod loader"
$vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
if (-not $vc -or $vc.Installed -ne 1) {
    Say "   installing the Microsoft Visual C++ runtime (Windows will ask for permission)..."
    $vcexe = Join-Path $Tmp "vc_redist.x64.exe"
    Download $VCRT_URL $vcexe
    $p = Start-Process $vcexe -ArgumentList "/install", "/passive", "/norestart" -Verb RunAs -Wait -PassThru
    if ($p.ExitCode -notin 0, 1638, 3010) { Fail "the Visual C++ runtime install failed (code $($p.ExitCode))." }
}
if ($Melty) {
    Say "   installed by Melty"
} elseif ((Test-Path (Join-Path $Plugins "pyunrealsdk.dll")) -and (Test-Path (Join-Path $BL "sdk_mods\mods_base.sdkmod"))) {
    Say "   already installed"
} else {
    $sdkzip = Join-Path $Tmp "bl1-enhanced-sdk.zip"
    Download $SDK_URL $sdkzip
    Expand-Archive $sdkzip -DestinationPath $BL -Force
    Say "   installed willow1 PythonSDK v2.3 (adds the Mods menu)"
}
# The Steam GOTY Enhanced exe is Launcher.exe, which the SDK doesn't recognise -> exe_override + our init script.
if (-not $Melty) { Copy-Item (Join-Path $ModSrc "Binaries\Win64\Plugins\bl1e_exe_fix.py") $Plugins -Force }
$tomlText = if ($Melty) { "exe_override bl1e_exe_fix" } else { Get-Content $Toml -Raw }   # Melty ships the set-up toml
if ($tomlText -notmatch 'exe_override' -or $tomlText -notmatch 'bl1e_exe_fix') {
    if (-not (Test-Path "$Toml.orig")) { Copy-Item $Toml "$Toml.orig" }
    $want = @{ "unrealsdk" = 'exe_override = "BorderlandsGOTY.exe"'; "pyunrealsdk" = 'init_script = "bl1e_exe_fix.py"' }
    $key = @{ "unrealsdk" = 'exe_override'; "pyunrealsdk" = 'init_script' }
    $out = @(); $sec = ""; $placed = @{}
    foreach ($l in (Get-Content $Toml)) {
        if ($l -match '^\s*\[([^\]]+)\]\s*$') {
            $sec = $Matches[1]; $out += $l
            if ($want.ContainsKey($sec)) { $out += $want[$sec]; $placed[$sec] = $true }
            continue
        }
        if ($key.ContainsKey($sec) -and $l -match ('^\s*' + $key[$sec] + '\s*=')) { continue }
        $out += $l
    }
    foreach ($s in @("unrealsdk", "pyunrealsdk")) { if (-not $placed[$s]) { $out += ""; $out += "[$s]"; $out += $want[$s] } }
    [IO.File]::WriteAllLines($Toml, [string[]]$out)    # UTF-8 without BOM
    Say "   set the loader up for the Steam GOTY Enhanced exe (unrealsdk.toml; original kept as unrealsdk.toml.orig)"
}
if (-not $Melty) { Skip-Launcher $Win64 }    # Melty starts the game through Steam and never changes its files

# ---------------------------------------------------------------------------------------------- KH1 save backup
# A permanent copy of the player's KH saves from BEFORE the mod ever ran (never rotated out; the mod itself also backs
# them up on every Borderlands start, keeping the last 10). Only made once: a reinstall/update keeps the first one.
Step "Backing up your Kingdom Hearts saves"
$docs = [Environment]::GetFolderPath("MyDocuments")
$khSaves = Join-Path $docs "My Games\KINGDOM HEARTS HD 1.5+2.5 ReMIX"
$origBak = Join-Path $docs "SoraInBorderlands\kh1_save_backup_before_mod"
if (Test-Path $origBak) {
    Say "   already backed up earlier: $origBak"
} elseif (Test-Path $khSaves) {
    New-Item -ItemType Directory -Force $origBak | Out-Null
    Copy-Item (Join-Path $khSaves "*") $origBak -Recurse -Force
    Set-Content (Join-Path $origBak "README.txt") ("Your Kingdom Hearts HD 1.5+2.5 saves from before Sora in Borderlands was installed " +
        "($(Get-Date -Format s)). To put them back: close Kingdom Hearts, then copy everything in this folder (except this file) into`r`n$khSaves")
    Say "   saved a copy to $origBak"
} else {
    Say "   no Kingdom Hearts saves found yet ($khSaves)" "Yellow"
}

# ---------------------------------------------------------------------------------------------- 3. unpack KH1 files
Step "Unpacking Kingdom Hearts files from your install (takes a while, about 18 GB)"
$khpc = Join-Path $Tmp "KHPCPatchManager.exe"
$X = Join-Path $Tmp "kh1_extract"
New-Item -ItemType Directory -Force $X | Out-Null
foreach ($part in $KH1_PARTS) {
    $dst = Join-Path $X $part
    if ((Test-Path (Join-Path $dst "original")) -and (Test-Path (Join-Path $dst "done.txt"))) { Say "   $part - already unpacked"; continue }
    Download $KHPC_URL $khpc
    $hed = Join-Path $KH "Image\dt\$part.hed"
    $hedOut = "$hed" + "_out"                           # KHPCPatchManager always writes next to the .hed
    Say "   $part ..."
    $t0 = Get-Date
    Push-Location $Tmp                                  # it unpacks its name lists to .\resources but reads them next to the exe
    try { "`n" | & $khpc $hed 2>&1 | Out-Null }         # the newline answers its "press Enter" at the end
    catch { }
    finally { Pop-Location }
    if (-not (Test-Path (Join-Path $hedOut "remastered"))) { Fail "unpacking $part.hed didn't work (no $hedOut\remastered)." }
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Move-Item $hedOut $dst                              # same drive -> instant
    Set-Content (Join-Path $dst "done.txt") "ok"
    Say ("      done ({0:N0} s)" -f ((Get-Date) - $t0).TotalSeconds)
}

# ---------------------------------------------------------------------------------------------- 4. build the KH files
Step "Building Sora, Donald, Goofy, Heartless, weapons and sounds from those files (5-10 minutes)"
$uvzip = Join-Path $Tmp "uv.zip"
$uv = Join-Path $Tmp "uv\uv.exe"
if (-not (Test-Path $uv)) {
    Download $UV_URL $uvzip
    Expand-Archive $uvzip -DestinationPath (Join-Path $Tmp "uv") -Force
}
$env:UV_CACHE_DIR = Join-Path $Tmp "uv-cache"              # keep everything uv downloads inside the temp folder
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tmp "python"
$env:UV_NO_CONFIG = "1"
$Staging = Join-Path $Tmp "mod_data"
if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
& $uv run --no-project --python 3.12 --with numpy --with pillow python (Join-Path $Here "build_kh1_files.py") `
    --extract $X --work (Join-Path $Tmp "build") --vgmstream (Join-Path $Here "vgmstream\vgmstream-cli.exe") --out $Staging
if ($LASTEXITCODE -ne 0) { Fail "building the KH1 files failed (see the messages above)." }

# ---------------------------------------------------------------------------------------------- 5. install the mod
Step "Installing the mod into Borderlands"
if ($Melty) {
    # the mod code is Melty's; only the KH1 files built on this PC go next to it
    New-Item -ItemType Directory -Force (Join-Path $ModDir "data") | Out-Null
    Copy-Item (Join-Path $Staging "*") (Join-Path $ModDir "data") -Recurse -Force
    Set-Content (Join-Path $ModDir "kh_build_stamp.txt") (KH-Build-Stamp $Here)
    if (-not $KeepTemp) { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }
    if ($DoneFile) { New-Item -ItemType Directory -Force (Split-Path -Parent $DoneFile) | Out-Null; Set-Content $DoneFile "built $(Get-Date -Format s)" }
    Say "  ALL DONE - KH1 files built from your install." "Green"
    exit 0
}
$marker = Join-Path $ModDir "installed_by_setup.txt"
if ((Test-Path $ModDir) -and -not (Test-Path $marker)) {
    $bak = "$ModDir" + "_backup_" + (Get-Date -Format "yyyyMMdd_HHmmss")
    Move-Item $ModDir $bak
    Say "   an older sdk_mods\sora was there - moved it to $(Split-Path -Leaf $bak)" "Yellow"
}
$keepCfg = $null
if (Test-Path (Join-Path $ModDir "config.json")) { $keepCfg = Get-Content (Join-Path $ModDir "config.json") -Raw }
if (Test-Path $ModDir) { Remove-Item $ModDir -Recurse -Force }
Copy-Item (Join-Path $ModSrc "sdk_mods\sora") $ModDir -Recurse
Copy-Item (Join-Path $Staging "*") (Join-Path $ModDir "data") -Recurse -Force
# update: the NEW config.json (new settings arrive) with the player's own values put back on top
if ($keepCfg) { Merge-Config (Join-Path $ModDir "config.json") $keepCfg }
Set-Content $marker "Sora in Borderlands, installed $(Get-Date -Format s). UNINSTALL.bat removes this whole folder."
Set-Content (Join-Path $ModDir "kh_build_stamp.txt") (KH-Build-Stamp $Here)     # UPDATE.bat: same stamp = quick update
$n = (Get-ChildItem $ModDir -Recurse -File).Count
Say "   $n files in $ModDir"

if (-not $KeepTemp) {
    Say "   deleting the temporary folder (frees about 20 GB)..."
    Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "  ALL DONE - Sora in Borderlands is installed." -ForegroundColor Green
Write-Host ""
Write-Host "  To play (every time):" -ForegroundColor Green
Write-Host "   1. Start KINGDOM HEARTS HD 1.5+2.5 ReMIX from Steam, pick KINGDOM HEARTS FINAL MIX, load a save" -ForegroundColor Green
Write-Host "      where Donald and Goofy are with you, and stand in the field (not in a menu or cutscene)." -ForegroundColor Green
Write-Host "   2. Start Borderlands GOTY Enhanced from Steam and load your character." -ForegroundColor Green
Write-Host "   3. Click back into Borderlands. Leave KH1 open behind it (don't minimize it)." -ForegroundColor Green
Write-Host "  Controls are in HOW TO INSTALL.txt and in sdk_mods\sora\CONTROLS.txt." -ForegroundColor Green

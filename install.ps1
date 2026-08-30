# skillmem one-command installer for Windows (PowerShell 5.1+ / pwsh 7).
#
# Usage:
#   irm https://<your-host>/skillmem/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-NoClaudeCode] [-NoSemantic] [-Url <tar.gz>] [-From <file>] [-NoSchedule]
#
# Что делает (зеркало install.sh):
#   1. находит Python >=3.12 (py launcher / python) или ставит через winget
#   2. скачивает тарбол (или берёт -From файл) + проверяет SHA256
#   3. разворачивает в %LOCALAPPDATA%\skillmem\current
#   4. venv + pip install -e .[semantic]  (-NoSemantic = только BM25)
#   5. кладёт .cmd-шимы в %USERPROFILE%\.local\bin и добавляет его в user PATH
#   6. skillmem init --claude-code  +  skillmem schedule install
#
# Откат: skillmem uninstall; rmdir /s %LOCALAPPDATA%\skillmem

[CmdletBinding()]
param(
    [switch]$NoClaudeCode,
    [switch]$NoSemantic,
    [switch]$NoSchedule,
    [string]$Url = $(if ($env:SKILLMEM_URL) { $env:SKILLMEM_URL } else { "https://<your-host>/skillmem/skillmem-latest.tar.gz" }),
    [string]$From = "",
    [string]$Token = ""   # read-only GitHub token для `skillmem upgrade` (приватные релизы)
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

function Say($msg)  { Write-Host "> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "! $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "x $msg" -ForegroundColor Red; exit 1 }

$InstallRoot = if ($env:SKILLMEM_INSTALL_ROOT) { $env:SKILLMEM_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "skillmem" }
$BinDir      = if ($env:SKILLMEM_BIN_DIR)      { $env:SKILLMEM_BIN_DIR }      else { Join-Path $env:USERPROFILE ".local\bin" }
$PkgDir      = Join-Path $InstallRoot "current"

# --- step 1: Python >= 3.12 ---
function Find-Python {
    # py launcher — самый надёжный способ выбрать версию на Windows.
    foreach ($ver in @("3.13", "3.12")) {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & py "-$ver" -c "pass" 2>$null
            if ($LASTEXITCODE -eq 0) { return @{ Exe = "py"; Args = @("-$ver") } }
        }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $v = & python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
        if ($v -and [version]$v -ge [version]"3.12") { return @{ Exe = "python"; Args = @() } }
    }
    return $null
}

$PyCmd = Find-Python
if (-not $PyCmd) {
    Say "Python >=3.12 не найден — ставлю через winget (Python.Python.3.12)"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Fail "winget недоступен. Поставь Python 3.12+ вручную с python.org и перезапусти установщик."
    }
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "winget install Python.Python.3.12 не удался" }
    # winget правит PATH для новых процессов — подтягиваем в текущий.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $PyCmd = Find-Python
    if (-not $PyCmd) { Fail "Python поставился, но не найден в PATH — открой новый терминал и перезапусти install.ps1" }
}
$PyExe = $PyCmd.Exe
$PyArgs = $PyCmd.Args
Say "Python: $(& $PyExe @PyArgs --version 2>&1)"

# --- step 2: obtain tarball + verify SHA256 ---
$TmpDir = Join-Path ([IO.Path]::GetTempPath()) ("skillmem-install-" + [Guid]::NewGuid().ToString("n").Substring(0, 8))
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
$Pkg = Join-Path $TmpDir "pkg.tgz"
$PkgSha = "$Pkg.sha256"

try {
    if ($From) {
        if (-not (Test-Path $From)) { Fail "no such file: $From" }
        Say "Installing skillmem from local file $From"
        Copy-Item $From $Pkg
        if (Test-Path "$From.sha256") { Copy-Item "$From.sha256" $PkgSha }
    } else {
        Say "Fetching skillmem from $Url"
        try { Invoke-WebRequest -Uri $Url -OutFile $Pkg -UseBasicParsing }
        catch { Fail "could not download $Url — check URL or network ($_)" }
        try { Invoke-WebRequest -Uri "$Url.sha256" -OutFile $PkgSha -UseBasicParsing } catch {}
    }

    if (Test-Path $PkgSha) {
        $expected = (Get-Content $PkgSha -Raw).Trim().Split()[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 $Pkg).Hash.ToLower()
        if ($expected -ne $actual) { Fail "SHA256 mismatch! expected=$expected actual=$actual — aborting." }
        Say "Checksum verified ($expected)"
    } else {
        Warn "no checksum file — proceeding without verification"
    }

    # --- step 3: unpack ---
    if (Test-Path $PkgDir) { Remove-Item -Recurse -Force $PkgDir }
    New-Item -ItemType Directory -Path $PkgDir -Force | Out-Null
    # tar.exe встроен в Windows 10 1803+.
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if (-not $tar) { Fail "tar.exe не найден (нужен Windows 10 1803+)" }
    tar -xzf $Pkg -C $PkgDir --strip-components=1
    if ($LASTEXITCODE -ne 0) { Fail "tar extraction failed" }
    Say "Unpacked to $PkgDir"
} finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}

# --- step 4: venv + install ---
Say "Creating venv + installing package"
Push-Location $PkgDir
try {
    & $PyExe @PyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed" }
    $VPython = Join-Path $PkgDir ".venv\Scripts\python.exe"
    & $VPython -m pip install --quiet --upgrade pip
    if (-not $NoSemantic) {
        Say "Including semantic recall (fastembed, ~200 MB; -NoSemantic to skip)"
        & $VPython -m pip install --quiet -e ".[semantic]"
        if ($LASTEXITCODE -ne 0) {
            Warn "semantic extra failed — falling back to base (recall stays BM25-only)"
            & $VPython -m pip install --quiet -e "."
            if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
        }
    } else {
        & $VPython -m pip install --quiet -e "."
        if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
    }
} finally { Pop-Location }

# --- step 5: shims + PATH ---
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
foreach ($cmd in @("skillmem", "skillmem-mcp", "skillmem-server")) {
    $exe = Join-Path $PkgDir ".venv\Scripts\$cmd.exe"
    Set-Content -Path (Join-Path $BinDir "$cmd.cmd") -Encoding Ascii -Value "@echo off`r`n`"$exe`" %*"
}
Say "Shims -> $BinDir\skillmem*.cmd"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$BinDir;$userPath", "User")
    $env:Path = "$BinDir;$env:Path"
    Say "Added $BinDir to user PATH (новые терминалы подхватят автоматически)"
}

# --- step 5b: pre-download embedding model ---
$VSkillMem = Join-Path $PkgDir ".venv\Scripts\skillmem.exe"
if (-not $NoSemantic) {
    Say "Downloading embedding model (~220 MB, one time — иначе первый поиск «зависнет»)"
    & $VPython -c "import sys; from skillmem import embed; sys.exit(0 if embed.available() else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { Say "Semantic recall ready" }
    else { Warn "model download failed — recall will use BM25 until it succeeds" }
}

# --- step 6: init Claude Code + schedule ---
if (-not $NoClaudeCode) {
    Say "Configuring Claude Code (-NoClaudeCode to skip)"
    & $VSkillMem init --claude-code
    if ($LASTEXITCODE -ne 0) { Warn "init failed — run manually: skillmem init --claude-code" }
} else {
    Say "Skipped Claude Code setup. Run later: skillmem init --claude-code"
}

if ($Token) {
    & $VSkillMem token set $Token | Out-Null
    Say "GitHub token сохранён — `skillmem upgrade` будет видеть новые релизы"
}

if (-not $NoSchedule) {
    & $VSkillMem schedule install
    if ($LASTEXITCODE -ne 0) { Warn "schedule install failed — run manually: skillmem schedule install" }
}

Write-Host ""
Write-Host "OK skillmem installed." -ForegroundColor Green
Write-Host @"

Try it now (в новом терминале, если PATH только что обновился):
    skillmem doctor
    skillmem search "..."
    skillmem ls --kind feedback

Open Claude Code ('claude') in any project — the mem_* tools are exposed.

Docs:   $PkgDir\OVERVIEW.md
Uninstall:
    skillmem uninstall
    Remove-Item -Recurse -Force '$InstallRoot'; Remove-Item '$BinDir\skillmem*.cmd'
"@

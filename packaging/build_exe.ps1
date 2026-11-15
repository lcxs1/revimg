#Requires -Version 5.1
<#
.SYNOPSIS
    Build revimg.exe (command line) or revimg-gui.exe (drag and drop) for Windows.

.DESCRIPTION
    Creates an isolated build environment (.venv-build), installs the pinned
    build requirements, runs the test suite, compiles the icon, packages the
    executable with PyInstaller and smoke-tests the result.

    The executable is written to dist\revimg.exe (one file) or, with -Gui, to
    dist\revimg-gui.exe.  Pass -OneDir for a folder build which starts faster.

.PARAMETER Gui
    Build the drag-and-drop GUI instead of the command-line program.

.PARAMETER OneDir
    Build a directory distribution instead of a single file.

.PARAMETER SkipTests
    Do not run the test suite before packaging.

.PARAMETER FullTests
    Also run the slow acceptance gate (2000x2000 attack matrix).

.PARAMETER NoInstall
    Reuse the existing .venv-build without installing requirements.

.PARAMETER NoZip
    Do not create the release zip in dist.

.PARAMETER PythonExe
    Python interpreter used to create .venv-build (default: py -3.12, then
    python on PATH).

.EXAMPLE
    pwsh -File packaging\build_exe.ps1

.EXAMPLE
    pwsh -File packaging\build_exe.ps1 -Gui

.EXAMPLE
    pwsh -File packaging\build_exe.ps1 -Gui -OneDir -SkipTests
#>
[CmdletBinding()]
param(
    [switch]$Gui,
    [switch]$OneDir,
    [switch]$SkipTests,
    [switch]$FullTests,
    [switch]$NoInstall,
    [switch]$NoZip,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvDir = Join-Path $Root ".venv-build"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$AppName = if ($Gui) { "revimg-gui" } else { "revimg" }
$SpecFile = Join-Path $Root ("packaging\" + $(if ($Gui) { "revimg_gui.spec" } else { "revimg.spec" }))

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)."
    }
}

# --- 1. build environment -------------------------------------------------
if (-not (Test-Path $VenvPy)) {
    Write-Step "Creating build environment .venv-build"
    if ($PythonExe) {
        $launcher = @{ Exe = $PythonExe; Args = @() }
    }
    elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $launcher = @{ Exe = "py"; Args = @("-3.12") }
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $launcher = @{ Exe = "python"; Args = @() }
    }
    else {
        throw "No Python found. Install Python 3.12 or pass -PythonExe <path>."
    }
    Invoke-Checked -Exe $launcher.Exe -Arguments (@($launcher.Args) + @("-m", "venv", $VenvDir)) -What "Creating the virtual environment"
}

Invoke-Checked -Exe $VenvPy -Arguments @(
    "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
) -What "Checking the Python version (3.10+ required)"

if (-not $NoInstall) {
    Write-Step "Installing build requirements"
    Invoke-Checked -Exe $VenvPy -Arguments @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "-r", (Join-Path $Root "packaging\requirements-build.txt")
    ) -What "Installing requirements"
}

# --- 2. icon and tests ----------------------------------------------------
Write-Step "Generating the application icon"
Invoke-Checked -Exe $VenvPy -Arguments @((Join-Path $Root "packaging\make_icon.py")) -What "Generating the icon"

if (-not $SkipTests) {
    if ($FullTests) {
        Write-Step "Running the full test suite (including the acceptance gate)"
        Invoke-Checked -Exe $VenvPy -Arguments @("-m", "unittest", "discover", "-s", "tests", "-v") -What "Tests"
    }
    else {
        Write-Step "Running the fast test suite"
        Invoke-Checked -Exe $VenvPy -Arguments @(
            "-m", "unittest", "discover", "-s", "tests", "-p", "test_revimg.py", "-v"
        ) -What "Tests"
    }
}

# --- 3. package -----------------------------------------------------------
$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root ("build\" + $AppName)
if (Test-Path $Build) { Remove-Item $Build -Recurse -Force }
if (Test-Path $Dist) {
    # Only clear this build's previous artefacts; the other flavour is kept.
    if ($OneDir) {
        Remove-Item (Join-Path $Dist $AppName) -Recurse -Force -ErrorAction SilentlyContinue
    }
    else {
        Remove-Item (Join-Path $Dist "$AppName.exe") -Force -ErrorAction SilentlyContinue
    }
}
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Write-Step "Packaging $AppName with PyInstaller"
$env:REVIMG_ONEDIR = if ($OneDir) { "1" } else { "0" }
Invoke-Checked -Exe $VenvPy -Arguments @(
    "-m", "PyInstaller", "--noconfirm", "--clean",
    "--distpath", $Dist, "--workpath", $Build,
    $SpecFile
) -What "PyInstaller"

$ExePath = if ($OneDir) { Join-Path $Dist "$AppName\$AppName.exe" } else { Join-Path $Dist "$AppName.exe" }
if (-not (Test-Path $ExePath)) { throw "PyInstaller finished but $ExePath is missing." }

$Payload = if ($OneDir) { Join-Path $Dist $AppName } else { $Dist }
Copy-Item (Join-Path $Root "LICENSE") (Join-Path $Payload "LICENSE") -Force
Copy-Item (Join-Path $Root "README.md") (Join-Path $Payload "README.md") -Force
# Keep the screenshot referenced by README.md next to the README.
New-Item -ItemType Directory -Force -Path (Join-Path $Payload "assets") | Out-Null
Copy-Item (Join-Path $Root "assets\gui.png") (Join-Path $Payload "assets\gui.png") -Force

# --- 4. smoke test --------------------------------------------------------
Write-Step "Smoke-testing $ExePath"
if ($Gui) {
    $report = Join-Path $Dist "gui-selftest.txt"
    # Use CreateProcess (UseShellExecute = $false) rather than Start-Process:
    # ShellExecute can pop a "Open File - Security Warning" dialog for a freshly
    # built, unsigned executable, which would block an unattended build forever.
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $ExePath
    $psi.Arguments = "--selftest `"$report`""
    $psi.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc.WaitForExit(180000)) {
        try { $proc.Kill() } catch { }
        throw "GUI self-test timed out."
    }
    if ($proc.ExitCode -ne 0) {
        if (Test-Path $report) { Get-Content $report }
        throw "GUI self-test failed (exit code $($proc.ExitCode))."
    }
    if (Test-Path $report) { Get-Content $report; Remove-Item $report -Force }
}
else {
    Invoke-Checked -Exe $ExePath -Arguments @("--version") -What "Version check"
    Invoke-Checked -Exe $ExePath -Arguments @("selftest") -What "Self-test"
}

# --- 5. report and zip ----------------------------------------------------
$version = (& $VenvPy -c "import revimg; print(revimg.__version__)").Trim()
$size = (Get-Item $ExePath).Length
Write-Step "Done"
Write-Host ("Executable : {0}" -f $ExePath)
Write-Host ("Version    : {0}" -f $version)
Write-Host ("Size       : {0:N1} MB" -f ($size / 1MB))

if (-not $NoZip) {
    $zipName = if ($OneDir) { "$AppName-{0}-win64-folder.zip" } else { "$AppName-{0}-win64.zip" }
    $zip = Join-Path $Dist ($zipName -f $version)
    if (Test-Path $zip) { Remove-Item $zip -Force }
    $items = if ($OneDir) {
        @((Join-Path $Dist $AppName))
    }
    else {
        @($ExePath, (Join-Path $Dist "LICENSE"), (Join-Path $Dist "README.md"),
          (Join-Path $Dist "assets"))
    }
    Compress-Archive -Path $items -DestinationPath $zip -CompressionLevel Optimal
    Write-Host ("Archive    : {0} ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
}

# Windows launcher: uv, Python, venv and packages all live in .\.runtime (nothing is installed system-wide).
#   paperlift.ps1 serve        start the web app (default)
#   paperlift.ps1 reset        delete .runtime
#   paperlift.ps1 update       reinstall dependencies
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$RT = Join-Path $Here '.runtime'
$Venv = Join-Path $RT 'venv'
$UV = Join-Path $RT 'bin\uv.exe'
$Py = Join-Path $Venv 'Scripts\python.exe'
$PyVer = if ($env:PAPERLIFT_PYTHON) { $env:PAPERLIFT_PYTHON } else { '3.12' }

$env:UV_PYTHON_INSTALL_DIR = Join-Path $RT 'python'
$env:UV_CACHE_DIR = Join-Path $RT 'cache'
$env:UV_TOOL_DIR = Join-Path $RT 'tools'
$env:UV_NO_CONFIG = '1'
$env:UV_PYTHON_PREFERENCE = 'only-managed'

$cmd = if ($args.Count) { $args[0] } else { 'serve' }
if ($cmd -eq 'reset') { Remove-Item -Recurse -Force $RT -ErrorAction SilentlyContinue; Write-Host 'Removed .runtime. It is rebuilt on the next start.'; exit 0 }

New-Item -ItemType Directory -Force -Path (Join-Path $RT 'bin') | Out-Null
if (-not (Test-Path $UV)) {
  Write-Host '> Downloading uv into .runtime\bin (one time)'
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
  $zip = Join-Path $RT 'uv.zip'
  Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-pc-windows-msvc.zip" -OutFile $zip
  Expand-Archive $zip -DestinationPath (Join-Path $RT 'bin') -Force
  Remove-Item $zip
  $found = Get-ChildItem (Join-Path $RT 'bin') -Recurse -Filter uv.exe | Select-Object -First 1
  if ($found.FullName -ne $UV) { Move-Item $found.FullName $UV -Force }
}
if (-not (Test-Path $Py)) {
  Write-Host "> Installing private Python $PyVer into .runtime"
  & $UV venv --quiet --python $PyVer $Venv
}
$stamp = Join-Path $RT '.req.sha'
if ($cmd -eq 'update') { Remove-Item $stamp -ErrorAction SilentlyContinue }
$want = (Get-FileHash (Join-Path $Here 'requirements.txt')).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $want) {
  Write-Host '> Installing dependencies into .runtime'
  & $UV pip install --quiet --python $Py -r (Join-Path $Here 'requirements.txt')
  Set-Content $stamp $want
}
if ($cmd -eq 'update') { Write-Host 'Dependencies up to date.'; exit 0 }
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONWARNINGS = 'ignore'
& $Py -m paperlift.cli @args

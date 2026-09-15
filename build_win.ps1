param(
  [switch]$Log,
  [switch]$Debug,
  [switch]$OneFile,
  [switch]$NoHold
)
$ErrorActionPreference = 'Stop'

function Write-Info($m){ Write-Host "[build] $m" }
function Write-Err($m){ Write-Host "[error] $m" -ForegroundColor Red }

# Ubicar repo root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
$Root = $ScriptDir
$Venv = Join-Path $Root 'venv'
$LogFile = Join-Path $Root 'build_win.log'

# Activar logging por defecto si no se paso explícitamente
if (-not $PSBoundParameters.ContainsKey('Log')) { $Log = $true }

if ($Log) {
  try { if (Test-Path $LogFile) { Remove-Item -Force $LogFile } } catch {}
}

# Resolver Python preferentemente con py.exe
$pyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $pyCmd = @('py','-3') }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyCmd = @('python') }
else { throw 'Python 3 no encontrado. Instala Python y asegura py.exe o python.exe en PATH.' }

function RunPy([string[]]$args){ & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] @args; if ($LASTEXITCODE) { throw "Comando Python fallo: $($args -join ' ')" } }

$hadError = $false
$transcribing = $false
try {
  if ($Log) {
    try { Start-Transcript -Path $LogFile -Force | Out-Null; $transcribing = $true } catch {}
  }

  # Crear venv
  if (-not (Test-Path $Venv)) {
    Write-Info 'Creando entorno virtual...'
    try {
      & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m venv $Venv; if ($LASTEXITCODE) { throw 'venv1' }
    } catch {
      Write-Info 'Reintentando con ensurepip...'
      try { & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m ensurepip --upgrade | Out-Null } catch {}
      & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m pip install --upgrade pip setuptools wheel
      & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m venv $Venv; if ($LASTEXITCODE) {
        Write-Info 'Fallback: usando virtualenv'
        & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m pip install --upgrade virtualenv
        & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m virtualenv $Venv; if ($LASTEXITCODE) { throw 'No se pudo crear venv' }
      }
    }
  }

$pip = Join-Path $Venv 'Scripts/pip.exe'
$pyVenv = Join-Path $Venv 'Scripts/python.exe'

Write-Info 'Actualizando pip...'
& $pyVenv -m pip install --upgrade pip setuptools wheel

Write-Info "Python: $(& $pyVenv --version)"
Write-Info "Pip: $(& $pyVenv -m pip --version)"

if (Test-Path (Join-Path $Root 'requirements.txt')) {
  Write-Info 'Instalando requirements...'
  & $pyVenv -m pip install -r (Join-Path $Root 'requirements.txt')
}

Write-Info 'Instalando PyInstaller...'
& $pyVenv -m pip install pyinstaller
Write-Info "PyInstaller: $(& $pyVenv -m PyInstaller --version)"

# Datos e ícono
$iconCandidates = @()
$iconCandidates += (Join-Path $Root 'utils/app.ico')
$iconCandidates += (Join-Path $Root 'utils/icon.ico')
$icon = $iconCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

# Si no hay .ico, intentar generar uno desde PNG
if (-not $icon) {
  $pngCandidates = @((Join-Path $Root 'utils/app.png'), (Join-Path $Root 'utils/icon.png')) | Where-Object { Test-Path $_ }
  if ($pngCandidates.Count -gt 0) {
    $genIco = Join-Path $Root 'utils/generated_icon.ico'
    Write-Info "Generando icono .ico desde PNG: $($pngCandidates[0]) -> $genIco"
    try {
      & $pyVenv -m pip install --upgrade pillow | Out-Null
      $pyCode = @"
from PIL import Image
import sys
src = sys.argv[1]
dst = sys.argv[2]
img = Image.open(src).convert('RGBA')
sizes = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
img.save(dst, format='ICO', sizes=sizes)
"@
      & $pyVenv -c $pyCode $pngCandidates[0] $genIco
      if ($LASTEXITCODE -eq 0 -and (Test-Path $genIco)) { $icon = $genIco }
    } catch {
      Write-Err "No se pudo generar el .ico desde PNG: $_"
    }
  }
}

$addArgs = @()
function AddDataIfExists($path, $dest){ if (Test-Path $path) { $script:addArgs += @('--add-data', "$path;$dest") } }
AddDataIfExists (Join-Path $Root 'credential.json') '.'
AddDataIfExists (Join-Path $Root 'utils/template.rdp') 'utils'
AddDataIfExists (Join-Path $Root 'utils/app.ico') 'utils'
AddDataIfExists (Join-Path $Root 'utils/app.png') 'utils'
AddDataIfExists (Join-Path $Root 'utils/icon.ico') 'utils'
AddDataIfExists (Join-Path $Root 'utils/icon.png') 'utils'

$pyi = Join-Path $Venv 'Scripts/pyinstaller.exe'
$packageMode = if ($OneFile) { '--onefile' } else { '--onedir' }
$baseArgs = @('--noconfirm', $packageMode, '--windowed', '--clean', '--name', 'itool')
if ($icon) { $baseArgs += @('--icon', $icon) } else { Write-Err 'Advertencia: no se encontró ícono (.ico/.png); el EXE usará el ícono por defecto.' }
$baseArgs += $addArgs
$baseArgs += (Join-Path $Root 'main.py')

  Write-Info "Construyendo EXE ($packageMode)..."
  # Limpiar artefactos anteriores
  try {
    $buildDir = Join-Path $Root 'build'
    if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
    $oldExe = Join-Path (Join-Path $Root 'dist') 'itool.exe'
    if (Test-Path $oldExe) { Remove-Item -Force $oldExe }
    $oldOneDir = Join-Path (Join-Path $Root 'dist') 'itool'
    if (Test-Path $oldOneDir) { Remove-Item -Recurse -Force $oldOneDir }
  } catch { Write-Info 'No se pudo limpiar todos los artefactos, continúo...' }
  if ($Debug) { $baseArgs += '--log-level=DEBUG' }
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  if ($Log) {
    Write-Info "Guardando log en $LogFile"
    if ($transcribing) {
      # Transcript ya está capturando todo; evitar doble escritura sobre el mismo archivo
      & $pyi $baseArgs
    } else {
      & $pyi $baseArgs 2>&1 | Tee-Object -FilePath $LogFile
    }
  } else {
    & $pyi $baseArgs
  }
  $ErrorActionPreference = $prevEap
  if ($LASTEXITCODE) {
    Write-Err "PyInstaller fallo con codigo $LASTEXITCODE"
    if (Test-Path $LogFile) { Write-Err "Revisa el log: $LogFile" }
    Write-Info 'Diagnostico rapido:'
    try { & $pyVenv -c "import sys; print('sys.path ok')" | Out-Null; Write-Info 'sys.path OK' } catch { Write-Err 'Error en Python embebido' }
    try { & $pyVenv -c "import gspread,oauth2client,pythonping; print('imports OK')"; Write-Info 'Imports de runtime OK' } catch { Write-Err 'Fallo importando dependencias (revisa requirements.txt)' }
    throw "PyInstaller fallo ($LASTEXITCODE)"
  }

  $outputExe = if ($OneFile) { 'dist\itool.exe' } else { 'dist\itool\itool.exe' }
  Write-Info "Listo. EXE generado en $outputExe"
  Write-Info "Podés crear un acceso directo a $outputExe y fijarlo a la barra."
}
catch {
  $hadError = $true
  Write-Err ("ERROR: " + $_)
  if ($Log) { Write-Err ("Mas detalle en: " + $LogFile) }
}
finally {
  if ($transcribing) { try { Stop-Transcript | Out-Null } catch {} }
  if ($hadError -and -not $NoHold) {
    Write-Host ""
    Read-Host 'Presiona Enter para cerrar'
    exit 1
  }
}

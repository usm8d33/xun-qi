# 偵測顯示卡，輸出一行：版本|顯示卡名稱|說明代碼
#   版本：cpu / cu126 / cu126old / cu128 / cu130 / xpu
$ErrorActionPreference = 'SilentlyContinue'
$names = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) {
  $p = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
  if (Test-Path $p) { $smi = $p } else { $smi = $null }
} else { $smi = $smi.Source }

if ($smi) {
  $line = & $smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
  if ($line) {
    $parts = $line.Split(',') | ForEach-Object { $_.Trim() }
    $name = $parts[0]; $cap = 0.0; $drv = 0.0
    [double]::TryParse($parts[1], [ref]$cap) | Out-Null
    [double]::TryParse(($parts[2].Split('.')[0]), [ref]$drv) | Out-Null
    if ($cap -eq 0) {   # 舊驅動不支援 compute_cap，用名稱判斷
      if ($name -match 'RTX\s*50\d\d|RTX PRO|Blackwell') { $cap = 12.0 }
      elseif ($name -match 'RTX|GTX 16') { $cap = 7.5 }
      elseif ($name -match 'GTX|Quadro|Tesla') { $cap = 6.1 }
    }
    if ($cap -ge 10)      { if ($drv -ge 580) { "cu130|$name|blackwell" } else { "cu128|$name|blackwell_olddriver" } }
    elseif ($cap -ge 7.5) { "cu126|$name|nvidia" }
    elseif ($cap -ge 5.0) { "cu126old|$name|nvidia_old" }
    else                  { "cpu|$name|nvidia_too_old" }
    exit 0
  }
}
$intel = $names | Where-Object { $_ -match 'Intel.*Arc' } | Select-Object -First 1
if ($intel) { "xpu|$intel|intel_arc"; exit 0 }
$nv = $names | Where-Object { $_ -match 'NVIDIA' } | Select-Object -First 1
if ($nv) { "cpu|$nv|nvidia_no_driver"; exit 0 }
$amd = $names | Where-Object { $_ -match 'AMD|Radeon' } | Select-Object -First 1
if ($amd) { "cpu|$amd|amd"; exit 0 }
$first = $names | Select-Object -First 1
"cpu|$first|none"

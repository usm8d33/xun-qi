# 決定 .bat 要顯示的語言，輸出一行 zh 或 en（和 app\lang.py 的規則相同）
#   1. 程式資料夾 user_settings.json 的 "lang"（網頁「工具 › 語言」選的）
#   2. 從舊版更新、之前就用過（有 user_settings.json，或桌面已有舊的檔案資料夾）→ zh
#   3. 第一次使用：看 Windows 介面語言，中文（任何地區）→ zh，其他 → en
$lang = ''
$used = $false
$f = Join-Path (Split-Path -Parent $PSScriptRoot) 'user_settings.json'
if (Test-Path -LiteralPath $f) {
  $used = $true
  try { $lang = [string]((Get-Content -LiteralPath $f -Raw -Encoding UTF8 | ConvertFrom-Json).lang) } catch { $lang = '' }
}
if (-not $lang) {
  $desk = [Environment]::GetFolderPath('Desktop')
  foreach ($n in @('Xun-Qi 尋棲', 'LocalSend 收到的檔案')) {
    if (Test-Path -LiteralPath (Join-Path (Join-Path $desk $n) '.整理器資料')) { $used = $true }
  }
  if ($used) { $lang = 'zh' }
}
if (-not $lang) {
  try { $lang = (Get-UICulture).TwoLetterISOLanguageName } catch { $lang = '' }
}
if ($lang -like 'zh*') { 'zh' } else { 'en' }

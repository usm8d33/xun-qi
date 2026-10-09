# 用檔案總管開啟資料夾（或選取某個檔案），並把視窗拉到最前面（不會只在工作列閃爍）
# 參數用環境變數傳入：XQ_OPEN_PATH（資料夾或檔案）、XQ_OPEN_SELECT=1 表示「在資料夾中選取這個檔案」
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class XqWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@
$target = $env:XQ_OPEN_PATH
$select = $env:XQ_OPEN_SELECT -eq '1'
$folder = if ($select) { Split-Path -Parent $target } else { $target }
$shell = New-Object -ComObject Shell.Application
$before = @{}
foreach ($w in $shell.Windows()) { try { $before[[int64]$w.HWND] = $true } catch {} }
if ($select) { Start-Process explorer.exe -ArgumentList "/select,`"$target`"" }
else { Start-Process explorer.exe -ArgumentList "`"$folder`"" }
# 最多等 5 秒：找新開的視窗；如果檔案總管沿用已開著的同一個資料夾視窗，就找那個
$want = $folder.TrimEnd('\').ToLower()
$hwnd = [IntPtr]::Zero
for ($i = 0; $i -lt 25 -and $hwnd -eq [IntPtr]::Zero; $i++) {
  Start-Sleep -Milliseconds 200
  $same = $null
  foreach ($w in $shell.Windows()) {
    try {
      $p = $w.Document.Folder.Self.Path
      if (-not $p -or $p.TrimEnd('\').ToLower() -ne $want) { continue }
      if (-not $before.ContainsKey([int64]$w.HWND)) { $hwnd = [IntPtr][int64]$w.HWND; break }
      $same = $w
    } catch {}
  }
  if ($hwnd -eq [IntPtr]::Zero -and $same -and $i -ge 5) { $hwnd = [IntPtr][int64]$same.HWND }
}
if ($hwnd -ne [IntPtr]::Zero) {
  if ([XqWin]::IsIconic($hwnd)) { [void][XqWin]::ShowWindow($hwnd, 9) }
  # Windows 不讓背景程式搶前景：先送一個 Alt 鍵，再把視窗拉到最前面
  [XqWin]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
  [XqWin]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
  [void][XqWin]::SetForegroundWindow($hwnd)
}

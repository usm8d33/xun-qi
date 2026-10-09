# 跳出「選擇資料夾」視窗，並強制顯示在最上層（不會躲到瀏覽器後面）
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class LssWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.StartPosition = 'Manual'
$owner.Location = New-Object System.Drawing.Point(-32000, -32000)
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show()
# Windows 不讓背景程式搶前景：先送一個 Alt 鍵，再把視窗拉到最前面
[LssWin]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[LssWin]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[void][LssWin]::SetForegroundWindow($owner.Handle)
$owner.Activate()
if ($env:XQ_PICK_KIND -eq 'files') {
  # 選檔案（可以一次選很多個），每行輸出一個路徑
  $f = New-Object System.Windows.Forms.OpenFileDialog
  $f.Title = $env:LSS_PICK_TITLE
  $f.Multiselect = $true
  $f.Filter = 'All files (*.*)|*.*'
  if ($f.ShowDialog($owner) -eq 'OK') { $f.FileNames | ForEach-Object { Write-Output $_ } }
} else {
  $f = New-Object System.Windows.Forms.FolderBrowserDialog
  $f.Description = $env:LSS_PICK_TITLE
  $f.ShowNewFolderButton = $true
  if ($f.ShowDialog($owner) -eq 'OK') { Write-Output $f.SelectedPath }
}
$owner.Close()

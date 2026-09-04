# Puts a "starstack" shortcut with the owl icon on your Desktop.
# Double-click it to open the button; drag a folder onto it to stack straight away.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "Python not found on PATH. Install Python 3.10+ first."; exit 1 }
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'starstack.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $py
$s.Arguments = '"' + (Join-Path $here 'button.py') + '"'
$s.WorkingDirectory = $here
$s.IconLocation = (Join-Path $here 'docs\starstack.ico') + ',0'
$s.Description = "starstack - stacking for people who'd rather be looking up"
$s.Save()
Write-Host "Done. The owl is on your Desktop: $lnk"
Write-Host "Double-click to open the button. Drag a folder onto it to stack it."

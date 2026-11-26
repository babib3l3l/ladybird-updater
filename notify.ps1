param([Parameter(Mandatory)][string] $Message)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
try {
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $icon.BalloonTipTitle = 'Ladybird Updater'
    $icon.BalloonTipText = $Message
    $icon.ShowBalloonTip(7000)
    Start-Sleep -Seconds 8
} finally {
    $icon.Dispose()
}


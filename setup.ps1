[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Source,
    [Parameter(Mandatory)][string] $Installation,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{40}$')][string] $Revision,
    [string] $Python = 'python.exe',
    [string] $Git = 'git.exe',
    [switch] $Schedule,
    [switch] $NoShortcut
)

$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path -LiteralPath $Source).Path
$Installation = (Resolve-Path -LiteralPath $Installation).Path
$exe = Join-Path $Installation 'bin\Ladybird.exe'
if (!(Test-Path -LiteralPath (Join-Path $Source 'Meta\ladybird.py') -PathType Leaf)) { throw 'Source must be a Ladybird Git checkout.' }
if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Installation must contain bin\Ladybird.exe.' }
if ($Installation -eq $PSScriptRoot -or $PSScriptRoot.StartsWith($Installation.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep the updater folder outside the Ladybird installation folder.'
}
$pythonCommand = (Get-Command $Python -CommandType Application).Source
$pythonPath = & $pythonCommand -c 'import sys, tkinter; print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'A working Python 3 installation with tkinter is required.' }
$pythonw = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
$gitPath = (Get-Command $Git -CommandType Application).Source
$config = Join-Path $PSScriptRoot 'config.json'
$marker = Join-Path $Installation 'ladybird-update.json'
$entry = Join-Path $PSScriptRoot 'updater.py'
$Revision = $Revision.ToLowerInvariant()
if (Test-Path -LiteralPath $config) { throw 'config.json already exists. Keep it and edit the paths directly if needed.' }
if ((Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).revision -ne $Revision)) {
    throw 'The supplied revision conflicts with the installation marker.'
}
if (($Schedule -or !$NoShortcut) -and !(Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw 'pythonw.exe is required for the shortcut and scheduled tasks.'
}
$checkName = 'Ladybird Updater - Check'
$applyName = 'Ladybird Updater - Apply'
if ($Schedule) {
    foreach ($name in $checkName, $applyName) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw "A scheduled task already exists with the name $name." }
    }
}
@{
    installation = $Installation
    source = $Source
    build_script = Join-Path $PSScriptRoot 'build.py'
    python = $pythonPath
    git = $gitPath
} | ConvertTo-Json | Set-Content -LiteralPath $config -Encoding UTF8

& $pythonPath $entry --action verify
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $config
    throw 'The existing browser failed the render or HTTPS smoke test.'
}
if (!(Test-Path -LiteralPath $marker)) {
    [IO.File]::WriteAllText($marker, (@{ revision = $Revision } | ConvertTo-Json))
}

if (!$NoShortcut) {
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'Ladybird Updater.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"' + $entry + '"'
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Save()
}

if ($Schedule) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $check = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}" --action check' -f $entry) -WorkingDirectory $PSScriptRoot
    $apply = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}" --action apply' -f $entry) -WorkingDirectory $PSScriptRoot
    $weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '16:00'
    $retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15)
    $checkSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) -Priority 7
    $applySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $checkName -Action $check -Trigger $weekly -Principal $principal -Settings $checkSettings | Out-Null
    try {
        Register-ScheduledTask -TaskName $applyName -Action $apply -Trigger $retry -Principal $principal -Settings $applySettings | Out-Null
    } catch {
        Unregister-ScheduledTask -TaskName $checkName -Confirm:$false
        throw
    }
}
Write-Output 'Ladybird Updater is ready.'

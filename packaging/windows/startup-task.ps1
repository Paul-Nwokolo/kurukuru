<#
.SYNOPSIS
    Register or remove Kurukuru's start-at-logon task.

.DESCRIPTION
    Run by the installer and the uninstaller, and shipped into the install
    directory so that somebody who wants to know what was registered on their
    machine can read it rather than trust it.

    **Why not `schtasks.exe`.** The obvious one-liner —
    `schtasks /Create /SC ONLOGON` — fails with "Access is denied" for a
    standard user, with or without `/RU`. Measured on Windows 11: a logon
    trigger created that way is treated as applying to *any* user, which is an
    administrator's decision to make, so the whole per-user no-elevation install
    would have needed a UAC prompt for its most optional feature.

    `Register-ScheduledTask` with a trigger and a principal both scoped to the
    current user succeeds unelevated, because it is only ever asking to run
    something as the person asking. Same end result, visible in the same Task
    Scheduler, and no prompt.

    A Startup-folder shortcut would also have worked without elevation, and was
    rejected: it shows a console window on every sign-in, it cannot restart the
    backend if it exits, and it is invisible to anyone looking in the place
    Windows users are told to look for things that run at logon.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Install')]
    [switch] $Install,

    [Parameter(Mandatory = $true, ParameterSetName = 'Uninstall')]
    [switch] $Uninstall,

    [Parameter(ParameterSetName = 'Install')]
    [string] $ExePath,

    [string] $TaskName = 'Kurukuru'
)

$ErrorActionPreference = 'Stop'

function Remove-KurukuruTask {
    param([string] $Name)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $existing) { return $false }
    try { Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue } catch { }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    return $true
}

if ($Uninstall) {
    # Never fail an uninstall over this. A task that cannot be removed is worth
    # reporting, but blocking the removal of the program because of it would
    # leave the user with neither a working install nor a clean machine.
    try {
        if (Remove-KurukuruTask -Name $TaskName) { Write-Output "Removed the '$TaskName' startup task." }
        else { Write-Output "No '$TaskName' startup task to remove." }
    } catch {
        Write-Warning "Could not remove the '$TaskName' task: $($_.Exception.Message)"
    }
    exit 0
}

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot 'kurukuru.exe'
}
if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "No executable at $ExePath; not registering a task that would fail at every logon."
    exit 1
}

$account = "$env:USERDOMAIN\$env:USERNAME"

try {
    # Replaced rather than updated, so that an upgrade which changed the install
    # path cannot leave a task pointing at the previous one.
    Remove-KurukuruTask -Name $TaskName | Out-Null

    $action = New-ScheduledTaskAction -Execute $ExePath -Argument 'serve'
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
    # Interactive and Limited: it runs as this user, with this user's own
    # rights and nothing more. That is also what keeps ~/.kurukuru meaning the
    # same directory it means in a terminal — a task running as SYSTEM would
    # resolve every path in the product somewhere else.
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask -TaskName $TaskName `
        -Description 'Runs the Kurukuru backend and dashboard for this user.' `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Force | Out-Null

    Write-Output "Registered the '$TaskName' startup task for $account."
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started it."
    exit 0
} catch {
    # A failed registration must not fail the install. Everything else works;
    # the user simply starts it themselves, and the message says how.
    Write-Warning ("Could not register the startup task: {0}" -f $_.Exception.Message)
    Write-Warning "Kurukuru is installed and works — start it with 'kurukuru serve'."
    exit 0
}

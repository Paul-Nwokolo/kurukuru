<#
.SYNOPSIS
    Move Kurukuru's state directory to the Recycle Bin, or refuse and say why.

.DESCRIPTION
    Run by the uninstaller when the user answers Yes to "also delete your
    virtual machines and their data?", and shipped into the install directory
    so somebody can read what that answer does rather than trust it.

    **Why the Recycle Bin rather than a delete.** Database backups live *inside*
    the state directory, on purpose: keeping them there means the database and
    everything protecting it travel together, and one `KURUKURU_STATE_DIR` moves
    the lot. The cost only shows up here — the single answer that removes the
    directory removes every backup with it, so the one safety net the product
    has is gone exactly when it would be wanted.

    Moving the backups somewhere else would fix that narrowly and badly. They
    are ~300 KB of database history; the VM disks and the ISO library are the
    gigabytes. A backup outside the state directory would also not follow
    `KURUKURU_STATE_DIR`, and would be left behind by an uninstall the user
    asked to remove their data — copies of their database surviving on a
    machine where they said to remove it. Worse than the problem.

    The Recycle Bin covers all of it with one mechanism the user already
    understands, and makes the prompt honest: it can be undone.

    **And it never destroys.** If the tree is bigger than the Recycle Bin can
    hold, Windows' own behaviour is to delete permanently instead — which
    would turn a promise of recoverability into exactly the loss this is meant
    to prevent. So the size is checked first and the refusal is explicit:
    nothing is removed, and the caller tells the user where the folder is so
    they can delete it themselves if that is what they want.

.OUTPUTS
    Exit 0  moved to the Recycle Bin.
    Exit 2  too large for the Recycle Bin; nothing was touched.
    Exit 1  something else went wrong; nothing was touched.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Path
)

$ErrorActionPreference = 'Stop'

function Get-TreeSize {
    param([string] $Root)
    $sum = (Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return [int64]0 }
    return [int64]$sum
}

function Get-RecycleBinCapacity {
    <#
        The per-volume maximum, in bytes.

        Windows stores it under BitBucket\Volume\{GUID}\MaxCapacity in MB, but
        only once somebody has changed it from the default; a machine nobody
        has touched has no value to read. So the documented default is the
        fallback: 5% of the volume, which is what modern Windows uses for
        drives of this size. Deliberately the *conservative* reading — if the
        guess is low we refuse a move that might have worked, and the cost of
        that is a message. If it were high we would promise recoverability and
        destroy the data.
    #>
    param([string] $Root)

    $drive = ([System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $Root).Path)).TrimEnd('\')
    $volume = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'" -ErrorAction SilentlyContinue
    $total = if ($volume) { [int64]$volume.Size } else { [int64]0 }

    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume'
    if (Test-Path $key) {
        foreach ($sub in Get-ChildItem $key -ErrorAction SilentlyContinue) {
            $props = Get-ItemProperty -Path $sub.PSPath -ErrorAction SilentlyContinue
            if ($null -ne $props.MaxCapacity -and $props.MaxCapacity -gt 0) {
                # MaxCapacity is in MB and is per-volume; without a reliable
                # volume->GUID mapping the smallest configured value is the
                # safe one to assume.
                $candidate = [int64]$props.MaxCapacity * 1MB
                if ($null -eq $smallest -or $candidate -lt $smallest) { $smallest = $candidate }
            }
        }
    }
    if ($null -ne $smallest) { return $smallest }
    if ($total -gt 0) { return [int64]($total * 0.05) }
    return [int64]0
}

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Output "Nothing at $Path; nothing to remove."
    exit 0
}

try {
    $size = Get-TreeSize -Root $Path
    $capacity = Get-RecycleBinCapacity -Root $Path

    if ($capacity -gt 0 -and $size -gt $capacity) {
        Write-Output ("TOO-LARGE {0} bytes; the Recycle Bin on this drive holds about {1} bytes." -f $size, $capacity)
        exit 2
    }

    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
        $Path,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)

    if (Test-Path -LiteralPath $Path) {
        Write-Output "The folder is still there after the move; nothing was removed."
        exit 1
    }
    Write-Output ("RECYCLED {0} bytes from {1}." -f $size, $Path)
    exit 0
} catch {
    Write-Output ("FAILED {0}" -f $_.Exception.Message)
    exit 1
}

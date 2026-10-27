<#
.SYNOPSIS
    Verify the in-process offline guard and loopback socket behavior.

.DESCRIPTION
    This is a wrapper around `diagnose.ps1 -Offline`. It proves that the Python
    socket guard blocks a non-loopback connect and that loopback sockets still
    work. It does not and cannot prove that child processes or native libraries
    are offline; see docs/SECURITY.md for that boundary.

    For OS-level enforcement, create a Windows Firewall outbound rule for the
    assistant executable after reviewing docs/SECURITY.md. This script does not
    change firewall settings automatically.
#>
[CmdletBinding()]
param(
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$Arguments = @{ Offline = $true }
if ($Json) {
    $Arguments.Json = $true
}
& (Join-Path $PSScriptRoot "diagnose.ps1") @Arguments
exit $LASTEXITCODE

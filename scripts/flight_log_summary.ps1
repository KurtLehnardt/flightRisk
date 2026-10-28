# Print a short, redacted summary of FlightRisk ground or edge logs (see scripts/flight_log_summary.py).
#
#   .\scripts\flight_log_summary.ps1 C:\path\edge-2.log
#   .\scripts\flight_log_summary.ps1 edge-1.log edge-2.log -Minutes 10 -Clip
#
# The output never exceeds 20 000 bytes and has tokens, authorization headers and long hex strings
# removed, so it can be pasted into a message. -Clip also copies it to the clipboard.

param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)] [string[]]$Logs,
    [double]$Minutes = 5,
    [string]$Until = "",
    [switch]$Clip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:PYTHON) { $env:PYTHON } else { Join-Path $root "venv\Scripts\python.exe" }
$summaryArgs = @((Join-Path $PSScriptRoot "flight_log_summary.py")) + $Logs + @("--minutes", "$Minutes")
if ($Until) { $summaryArgs += @("--until", $Until) }

$text = & $python @summaryArgs | Out-String
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output $text
if ($Clip) {
    Set-Clipboard -Value $text
    Write-Host "Copied to the clipboard."
}

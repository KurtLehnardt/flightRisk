# PSScriptAnalyzer settings for the installer scripts:
#   Invoke-ScriptAnalyzer -Path installer\windows -Settings installer\windows\PSScriptAnalyzerSettings.psd1
#
# Excluded rules, and why:
#   PSAvoidUsingWriteHost  the scripts are interactive installers that print coloured progress
#                          to the console; their machine-readable output is the status file and
#                          the one JSON line, which do not use Write-Host.
#   PSUseSingularNouns     function names such as Get-RunningPids read better in plural here.
#   PSUseShouldProcessForStateChangingFunctions  the internal helpers are not cmdlets; -WhatIf is
#                          covered by -Check on uninstall.ps1 and update.ps1.
@{
    Severity     = @("Error", "Warning")
    ExcludeRules = @(
        "PSAvoidUsingWriteHost",
        "PSUseSingularNouns",
        "PSUseShouldProcessForStateChangingFunctions"
    )
}

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $BridgeArguments
)

$bridgeScript = Join-Path $PSScriptRoot 'tools\agent_bridge.py'
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue

if (-not $pythonCommand) {
    Write-Error 'Python 3 was not found on PATH.'
    exit 1
}

& $pythonCommand.Source $bridgeScript @BridgeArguments
exit $LASTEXITCODE

param([string]$Project = '', [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Project) {
    python -m workflow_core --project $Project gui --port $Port
} else {
    python -m workflow_core gui --port $Port
}

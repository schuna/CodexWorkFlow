param(
    [string]$OutputDirectory = (Join-Path (Get-Location) ('p0-encoding-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))),
    [ValidateRange(10, 600)][int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$codexPath = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if (Test-Path -LiteralPath $OutputDirectory) { throw "Output directory already exists: $OutputDirectory" }
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$stdoutFile = Join-Path $OutputDirectory 'stdout.bin'
$stderrFile = Join-Path $OutputDirectory 'stderr.bin'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$marker = [string]([char]0xD55C) + [char]0xAE00 + '-' + [char]0xD14C + [char]0xC2A4 + [char]0xD2B8

function Quote-Argument([string]$Value) {
    if ($Value -notmatch '[\s"]' -and $Value.Length -gt 0) { return $Value }
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

$prompt = 'Reply with exactly this marker, with no other text: ' + $marker + '. Do not use tools.'
$arguments = @('exec', '--json', '--sandbox', 'read-only', '--ephemeral', '--skip-git-repo-check', '-C', $OutputDirectory, $prompt)
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $codexPath
$start.Arguments = (($arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
$start.WorkingDirectory = $OutputDirectory
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $start
$stdout = New-Object System.IO.MemoryStream
$stderr = New-Object System.IO.MemoryStream
$reason = 'natural'
$exitCode = $null
$started = $false
try {
    if (-not $process.Start()) { throw 'Could not start Codex.' }
    $started = $true
    $process.StandardInput.Close()
    $outTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
    $errTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $reason = 'timeout'
        & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null
        if (-not $process.WaitForExit(10000)) { throw 'Process did not terminate.' }
    }
    $exitCode = $process.ExitCode
    if (-not [System.Threading.Tasks.Task]::WaitAll(@($outTask, $errTask), 10000)) { throw 'Output streams did not close.' }
} finally {
    if ($started -and -not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null }
    [System.IO.File]::WriteAllBytes($stdoutFile, $stdout.ToArray())
    [System.IO.File]::WriteAllBytes($stderrFile, $stderr.ToArray())
    $stdout.Dispose(); $stderr.Dispose(); $process.Dispose()
}

$raw = [System.IO.File]::ReadAllBytes($stdoutFile)
$validUtf8 = $true
try { $decoded = $strictUtf8.GetString($raw) } catch { $validUtf8 = $false; $decoded = $utf8.GetString($raw) }
[System.IO.File]::WriteAllText((Join-Path $OutputDirectory 'stdout.utf8.jsonl'), $decoded, $utf8)
$events = @()
$invalidJsonLines = 0
foreach ($line in ($decoded -split "`r?`n")) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try { $events += ($line | ConvertFrom-Json -ErrorAction Stop) } catch { $invalidJsonLines++ }
}
$messages = @($events | Where-Object { $_.type -eq 'item.completed' -and $_.item.type -eq 'agent_message' } | ForEach-Object { $_.item.text })
$completed = @($events | Where-Object { $_.type -eq 'turn.completed' }).Count -gt 0
$lastMessage = if ($messages.Count -gt 0) { [string]$messages[-1] } else { '' }
$markerFound = $lastMessage.Trim() -eq $marker
$status = if ($exitCode -eq 0 -and $reason -eq 'natural' -and $validUtf8 -and $invalidJsonLines -eq 0 -and $completed -and $markerFound) { 'pass' } else { 'inconclusive' }
$summary = [pscustomobject]@{
    tested_at = (Get-Date).ToString('o'); status = $status; exit_code = $exitCode; ended_by = $reason
    valid_utf8 = $validUtf8; invalid_json_lines = $invalidJsonLines; turn_completed = $completed
    expected_marker = $marker; last_message = $lastMessage; marker_matches = $markerFound
    limitations = @('Checks Codex stdout bytes and final model message only.', 'Does not establish encoding of output from subprocess tools.', 'A different model answer or timeout is inconclusive, not proof of encoding failure.')
}
[System.IO.File]::WriteAllText((Join-Path $OutputDirectory 'summary.json'), ($summary | ConvertTo-Json -Depth 6), $utf8)
Write-Host "Encoding result: $status"
Write-Host "Output: $OutputDirectory"

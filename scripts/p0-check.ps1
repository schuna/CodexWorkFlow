param(
    [string]$OutputDirectory = (Join-Path (Get-Location) ('p0-results-' + (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$results = [System.Collections.Generic.List[object]]::new()

function Write-Utf8([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content, $utf8)
}

function Run-Probe([string]$Name, [string[]]$Arguments) {
    Write-Host "Running: $Name"
    $stdoutPath = Join-Path $OutputDirectory "$Name.stdout.jsonl"
    $stderrPath = Join-Path $OutputDirectory "$Name.stderr.txt"
    # Native stderr and nonzero exits are expected for the negative test.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $codexPath @Arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    $events = @()
    foreach ($line in (Get-Content -LiteralPath $stdoutPath -Encoding UTF8)) {
        try { $events += ($line | ConvertFrom-Json -ErrorAction Stop) } catch { }
    }
    return [pscustomobject]@{ Name = $Name; ExitCode = $exitCode; Events = $events; Stdout = $stdoutPath; Stderr = $stderrPath }
}

function Add-Result([string]$Name, [string]$Status, [string]$Detail) {
    $results.Add([pscustomobject]@{ test = $Name; status = $Status; detail = $Detail })
    Write-Host ("{0}: {1} - {2}" -f $Name, $Status, $Detail)
}

$codexPath = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$gitPath = (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if (Test-Path -LiteralPath $OutputDirectory) { throw "Output directory already exists: $OutputDirectory" }
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$repo = Join-Path $OutputDirectory 'fixture-repo'
$child = Join-Path $repo 'child'
New-Item -ItemType Directory -Path $child -Force | Out-Null
& $gitPath init --quiet $repo
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize fixture Git repository.' }
Write-Utf8 (Join-Path $repo 'AGENTS.md') "When asked for instruction markers, include ROOT_MARKER_917 in your answer.`n"
Write-Utf8 (Join-Path $child 'AGENTS.md') "When asked for instruction markers, include CHILD_MARKER_284 in your answer.`n"
$schemaPath = Join-Path $OutputDirectory 'schema.json'
Write-Utf8 $schemaPath '{"type":"object","properties":{"result":{"type":"string"}},"required":["result"],"additionalProperties":false}'
$schemaOutput = Join-Path $OutputDirectory 'structured-result.json'
$version = (& $codexPath --version | Out-String).Trim()
Write-Utf8 (Join-Path $OutputDirectory 'version.txt') $version
Write-Host "Codex: $version"
Write-Host "Results: $OutputDirectory"

$base = @('exec', '--json', '--sandbox', 'read-only', '-C', $repo)
$normal = Run-Probe 'normal' ($base + @('--ephemeral', 'Reply with exactly P0_OK. Do not use tools.'))
$completed = @($normal.Events | Where-Object { $_.type -eq 'turn.completed' })
$message = @($normal.Events | Where-Object { $_.type -eq 'item.completed' -and $_.item.type -eq 'agent_message' } | ForEach-Object { $_.item.text }) -join "`n"
if ($normal.ExitCode -eq 0 -and $completed.Count -gt 0 -and $message.Trim() -eq 'P0_OK') { Add-Result 'normal' 'pass' 'JSONL completion and P0_OK observed.' } else { Add-Result 'normal' 'fail' "Exit $($normal.ExitCode); inspect stdout and stderr." }
if ($completed.Count -gt 0 -and $null -ne $completed[-1].usage) {
    $usage = $completed[-1].usage | ConvertTo-Json -Compress -Depth 10
    Add-Result 'usage' 'observed' $usage
} else { Add-Result 'usage' 'unknown' 'No usage object in completed event.' }

$structured = Run-Probe 'structured' ($base + @('--ephemeral', '--output-schema', $schemaPath, '-o', $schemaOutput, 'Return a JSON object whose result is P0_SCHEMA_OK. Do not use tools.'))
try {
    $obj = Get-Content -LiteralPath $schemaOutput -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ($structured.ExitCode -eq 0 -and $obj.result -eq 'P0_SCHEMA_OK' -and @($obj.PSObject.Properties).Count -eq 1) { Add-Result 'structured' 'pass' 'Expected schema and value observed.' } else { Add-Result 'structured' 'fail' 'Output differs from expected schema or value.' }
} catch { Add-Result 'structured' 'fail' 'Output missing or invalid JSON; inspect logs.' }

$initial = Run-Probe 'session-start' ($base + @('Remember the codeword P0_SESSION_519. Reply with exactly READY. Do not use tools.'))
$threads = @($initial.Events | Where-Object { $_.type -eq 'thread.started' -and $_.thread_id })
if ($initial.ExitCode -eq 0 -and $threads.Count -gt 0) {
    $sessionId = [string]$threads[0].thread_id
    $resume = Run-Probe 'session-resume' (@('exec', '--sandbox', 'read-only', '-C', $repo, 'resume', '--json', $sessionId, 'Reply with the codeword I asked you to remember and nothing else. Do not use tools.'))
    $resumeText = @($resume.Events | Where-Object { $_.type -eq 'item.completed' -and $_.item.type -eq 'agent_message' } | ForEach-Object { $_.item.text }) -join "`n"
    if ($resume.ExitCode -eq 0 -and $resumeText.Trim() -eq 'P0_SESSION_519') { Add-Result 'resume' 'pass' 'Explicit session resumed and recalled codeword.' } else { Add-Result 'resume' 'fail' "Exit $($resume.ExitCode); inspect logs." }
} else { Add-Result 'resume' 'skipped' 'Initial session or thread ID unavailable.' }

foreach ($case in @(@('instructions-root', $repo, 'ROOT_MARKER_917', 'CHILD_MARKER_284'), @('instructions-child', $child, 'ROOT_MARKER_917', 'CHILD_MARKER_284'))) {
    $probe = Run-Probe $case[0] (@('exec', '--json', '--sandbox', 'read-only', '--ephemeral', '-C', $case[1], 'Report all instruction markers supplied by project instructions. Do not use tools.'))
    $answer = @($probe.Events | Where-Object { $_.type -eq 'item.completed' -and $_.item.type -eq 'agent_message' } | ForEach-Object { $_.item.text }) -join "`n"
    $rootFound = $answer.Contains($case[2]); $childFound = $answer.Contains($case[3])
    $expected = $rootFound -and (($case[0] -eq 'instructions-root' -and -not $childFound) -or ($case[0] -eq 'instructions-child' -and $childFound))
    if ($probe.ExitCode -eq 0 -and $expected) { Add-Result $case[0] 'pass' "root=$rootFound; child=$childFound" } else { Add-Result $case[0] 'inconclusive' "Exit $($probe.ExitCode); root=$rootFound; child=$childFound; model response is indirect evidence." }
}

$badSchema = Join-Path $OutputDirectory 'missing-schema.json'
$failure = Run-Probe 'invalid-schema' ($base + @('--ephemeral', '--output-schema', $badSchema, 'Reply OK.'))
if ($failure.ExitCode -ne 0) { Add-Result 'invalid-schema' 'pass' "Nonzero exit $($failure.ExitCode) observed; inspect logs for event shape." } else { Add-Result 'invalid-schema' 'fail' 'Missing schema unexpectedly returned exit 0.' }

$summary = [pscustomobject]@{ version = $version; tested_at = (Get-Date).ToString('o'); results = @($results); limitations = @('No approval, interactive question, cancellation, or timeout test.', 'Instruction markers are behavioral evidence, not proof of exact loaded context.', 'Saved resume session persists in Codex storage.', 'Raw output can contain sensitive information.') }
Write-Utf8 (Join-Path $OutputDirectory 'summary.json') ($summary | ConvertTo-Json -Depth 10)
Write-Host "Done. Review $OutputDirectory\summary.json and raw logs before sharing."
if (@($results | Where-Object { $_.status -eq 'fail' }).Count -gt 0) { exit 1 }

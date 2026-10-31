param(
    [string]$OutputDirectory = (Join-Path (Get-Location) ('p0-exceptions-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))),
    [ValidateRange(10, 600)][int]$ProbeTimeoutSeconds = 120,
    [ValidateRange(1, 60)][int]$CancelAfterSeconds = 5,
    [ValidateRange(1, 60)][int]$TimeoutTestSeconds = 3
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$results = [System.Collections.Generic.List[object]]::new()

function Write-Utf8([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content, $utf8)
}

# Windows process argument escaping, including trailing backslashes before a quote.
function Quote-Argument([string]$Value) {
    if ($Value -notmatch '[\s"]' -and $Value.Length -gt 0) { return $Value }
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Run-Probe([string]$Name, [string[]]$Arguments, [int]$LimitSeconds, [string]$TerminationReason) {
    Write-Host "Running $Name (limit: ${LimitSeconds}s)..."
    $stdoutPath = Join-Path $OutputDirectory "$Name.stdout.jsonl"
    $stderrPath = Join-Path $OutputDirectory "$Name.stderr.txt"
    $process = $null
    $endedBy = 'natural'
    $exitCode = $null
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $codexPath
        $start.Arguments = (($Arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
        $start.WorkingDirectory = $repo
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $start
        if (-not $process.Start()) { throw 'Process did not start.' }
        $process.StandardInput.Close()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($LimitSeconds * 1000)) {
            $endedBy = $TerminationReason
            # Only terminate the process started by this probe and its children.
            & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null
            if (-not $process.WaitForExit(10000)) { throw 'Probe process did not terminate within 10 seconds.' }
        }
        $exitCode = $process.ExitCode
        if (-not [System.Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask), 10000)) { throw 'Output streams did not close within 10 seconds.' }
        Write-Utf8 $stdoutPath $stdoutTask.Result
        Write-Utf8 $stderrPath $stderrTask.Result
    } catch {
        if ($null -ne $process -and -not $process.HasExited) {
            & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null
        }
        Write-Utf8 $stderrPath ("Probe harness error: " + $_.Exception.Message)
        if (-not (Test-Path -LiteralPath $stdoutPath)) { Write-Utf8 $stdoutPath '' }
        $endedBy = 'harness_error'
    } finally {
        $timer.Stop()
        if ($null -ne $process) { $process.Dispose() }
    }
    $events = @()
    foreach ($line in (Get-Content -LiteralPath $stdoutPath -Encoding UTF8)) {
        try { $events += ($line | ConvertFrom-Json -ErrorAction Stop) } catch { }
    }
    $types = @($events | ForEach-Object { $_.type } | Where-Object { $_ })
    $messages = @($events | Where-Object { $_.type -eq 'item.completed' -and $_.item.type -eq 'agent_message' } | ForEach-Object { $_.item.text })
    return [pscustomobject]@{ name = $Name; exit_code = $exitCode; ended_by = $endedBy; elapsed_seconds = [math]::Round($timer.Elapsed.TotalSeconds, 2); event_types = $types; final_messages = $messages; stdout = $stdoutPath; stderr = $stderrPath }
}

function Add-Result([string]$Name, [string]$Status, [string]$Detail, $Probe) {
    $results.Add([pscustomobject]@{ test = $Name; status = $Status; detail = $Detail; probe = $Probe })
    Write-Host "${Name}: $Status - $Detail"
}

$codexPath = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$gitPath = (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if (Test-Path -LiteralPath $OutputDirectory) { throw "Output directory already exists: $OutputDirectory" }
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$repo = Join-Path $OutputDirectory 'fixture-repo'
New-Item -ItemType Directory -Path $repo | Out-Null
& $gitPath init --quiet $repo
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize fixture Git repository.' }
Write-Utf8 (Join-Path $repo 'readonly-fixture.txt') 'ORIGINAL_FIXTURE'
Write-Utf8 (Join-Path $repo 'sample.txt') "Alpha is enabled.`nBeta is disabled.`n"
$version = (& $codexPath --version | Out-String).Trim()
$base = @('exec', '--json', '--sandbox', 'read-only', '--ephemeral', '-C', $repo)
Write-Host "Codex: $version`nResults: $OutputDirectory"

$question = Run-Probe 'question' ($base + @('The requested setting has two valid values: ALPHA or BETA. The user did not specify which. Ask exactly one question to clarify. Do not choose a value. Do not use tools.')) $ProbeTimeoutSeconds 'timeout'
$questionText = $question.final_messages -join ' '
if ($question.ended_by -eq 'natural' -and $question.exit_code -eq 0 -and $questionText.Length -gt 0) { Add-Result 'question' 'review' 'A final response was observed. Inspect whether it asks a question; this does not demonstrate interactive prompting.' $question } else { Add-Result 'question' 'inconclusive' 'No successful final response; inspect logs.' $question }

$approval = Run-Probe 'readonly-write-request' ($base + @('Try to change readonly-fixture.txt to exactly CHANGED_FIXTURE using a file editing or shell tool. If access is denied, report the error. Do not change anything else.')) $ProbeTimeoutSeconds 'timeout'
$fixture = Get-Content -LiteralPath (Join-Path $repo 'readonly-fixture.txt') -Raw
if ($fixture.Trim() -eq 'ORIGINAL_FIXTURE') { Add-Result 'readonly-write-request' 'review' 'Fixture unchanged. Inspect events to distinguish attempted denial from a refusal or no attempt; approval behavior is not established automatically.' $approval } else { Add-Result 'readonly-write-request' 'review' 'Fixture changed; inspect permissions, configuration and event logs.' $approval }

$failure = Run-Probe 'command-failure' ($base + @('Use a shell tool to run the command that reads the nonexistent file definitely-missing-p0-file.txt in the current directory. Report the failure. Do not write or change any file.')) $ProbeTimeoutSeconds 'timeout'
$commands = @($failure.event_types | Where-Object { $_ -eq 'item.completed' }).Count
Add-Result 'command-failure' 'review' "Inspect JSONL for command execution, its exit status and whether the turn itself failed. Completed item events: $commands." $failure

$slowPrompt = 'Use a shell tool to run PowerShell Start-Sleep -Seconds 60, then reply DONE. Do not change any files.'
$cancel = Run-Probe 'forced-cancel' ($base + @($slowPrompt)) $CancelAfterSeconds 'forced_cancel'
if ($cancel.ended_by -eq 'forced_cancel') { Add-Result 'forced-cancel' 'observed' 'Harness forcibly terminated its process tree; inspect partial events. This is not a graceful Ctrl+C test.' $cancel } else { Add-Result 'forced-cancel' 'inconclusive' 'Process exited before cancellation or harness failed.' $cancel }
$timeout = Run-Probe 'timeout' ($base + @($slowPrompt)) $TimeoutTestSeconds 'timeout'
if ($timeout.ended_by -eq 'timeout') { Add-Result 'timeout' 'observed' 'Harness deadline terminated its process tree; inspect partial events and elapsed time.' $timeout } else { Add-Result 'timeout' 'inconclusive' 'Process exited before deadline or harness failed.' $timeout }

$sample = Run-Probe 'small-sample' ($base + @('Read sample.txt using a tool. Report which feature is disabled and do not change files.')) $ProbeTimeoutSeconds 'timeout'
if ($sample.ended_by -eq 'natural' -and $sample.exit_code -eq 0) { Add-Result 'small-sample' 'review' 'Inspect answer and usage in JSONL. One sample does not establish budget defaults or comparative savings.' $sample } else { Add-Result 'small-sample' 'inconclusive' 'Sample did not complete normally.' $sample }

$summary = [pscustomobject]@{ version = $version; tested_at = (Get-Date).ToString('o'); results = @($results); limitations = @('Approval and question behavior require manual inspection; prompts may not trigger the desired condition.', 'Forced process termination is not graceful Ctrl+C and can happen before a turn starts.', 'The command failure probe depends on model tool selection; an unsuccessful command does not necessarily fail the turn.', 'No model-generated schema violation or representative comparative workload study is performed.', 'Logs and final messages may contain sensitive information.') }
Write-Utf8 (Join-Path $OutputDirectory 'summary.json') ($summary | ConvertTo-Json -Depth 12)
Write-Host "Done. Review $OutputDirectory\summary.json and logs before sharing."
if (@($results | Where-Object { $_.probe.ended_by -eq 'harness_error' }).Count -gt 0) { exit 1 }

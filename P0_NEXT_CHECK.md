# P0 다음 점검 안내

일반 PowerShell에서 프로젝트 루트로 이동한 뒤 아래 두 점검을 순서대로 실행한다. 두 점검 모두 Codex 호출이 발생한다. 결과에는 모델 응답과 로컬 경로가 포함될 수 있으므로 공유 전에 확인한다.

## 1. JSON 이벤트 인코딩

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\p0-encoding.ps1
```

새 `p0-encoding-<시각>` 폴더에 `summary.json`, 디코딩된 `stdout.utf8.jsonl`, 원시 바이트 `stdout.bin`, `stderr.bin`이 생성된다. `pass`는 이번 실행의 stdout이 유효한 UTF-8 JSONL이고 마지막 응답의 한글 표식이 일치함을 뜻한다. `inconclusive`는 로그를 추가로 살펴야 한다. 이 점검은 Codex가 실행한 별도 도구의 출력 인코딩까지 확인하지 않는다.

## 2. 콘솔 Ctrl+C 취소

아래 명령을 일반 PowerShell에서 한 줄씩 실행한다. 두 번째 명령이 실행 중일 때 약 10초 기다린 뒤 **Ctrl+C를 한 번** 누른다. 명령이 이미 끝났다면 취소를 확인할 수 없으므로 `inconclusive`다. 다른 터미널의 작업은 종료하지 않는다.

```powershell
$p0CancelDir = Join-Path (Get-Location) ('p0-cancel-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $p0CancelDir | Out-Null
codex exec --ephemeral --json --sandbox read-only 'Use a shell tool to run PowerShell Start-Sleep -Seconds 60, then reply DONE. Do not change files.' > (Join-Path $p0CancelDir 'stdout.txt') 2> (Join-Path $p0CancelDir 'stderr.txt')
```

프롬프트가 돌아오면 다음 명령으로 파일과 종료 코드를 확인한다. Ctrl+C로 PowerShell 파이프라인 자체가 중단되면 `$LASTEXITCODE`가 없거나 이전 명령의 값일 수 있다. 코드만으로 정상 취소를 판정하지 않는다.

```powershell
$LASTEXITCODE
Get-Content (Join-Path $p0CancelDir 'stdout.txt')
Get-Content (Join-Path $p0CancelDir 'stderr.txt')
Write-Output $p0CancelDir
```

생성된 `p0-cancel-<시각>` 경로와 화면에 출력된 종료 코드를 전달한다. 이벤트에 실제 `Start-Sleep` 명령 시작이 있는지, 완료 이벤트와 오류가 있는지 확인한다. 출력 리디렉션 때문에 화면에 진행 상황은 표시되지 않을 수 있고, 취소 시 로그가 비거나 잘릴 수도 있다. 이 경우 결과를 미결정으로 기록한다. 실행 중에는 부분적인 프로세스 상태만 확인하므로 실제 애플리케이션의 모든 복구 동작을 보장하지 않는다.

## 이후 남는 항목

이 두 점검으로 대화형 승인, `turn.failed`, 모델 출력의 스키마 위반, 대표 작업 간 사용량 비교와 UI 사용성은 확인되지 않는다. 대표 작업 측정에서는 같은 시작 상태와 모델·설정으로 장기 대화와 작업별 인계를 비교하고 총 사용량·성공 여부·재작업·시간을 함께 기록한다. 실제 사용자 대상 UI 검토는 화면 시제품이 준비된 뒤 수행한다.

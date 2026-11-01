# P0 다음 점검 안내

## 현재 구분 (2026-11-01)

아래 두 절차는 **이미 수행된 점검의 재현 안내**로 보존한다. `P0_REVIEW.md`에 2026-10-31 stdout UTF-8/한글 응답 성공(`p0-encoding-20261031-223331`)과 Ctrl+C 부분 로그(`p0-cancel-20261031-223500`)가 기록돼 있다. Ctrl+C는 자식 프로세스 정리까지 확인한 통과 기록이 아니다. 절차를 아직 실행하지 않은 것으로 다시 취급하지 않는다.

| 실제 잔여 항목 | 제품 영향·현재 방어 | 다음 행동 |
| --- | --- | --- |
| 실제 `turn.failed`·잘못된 최종 구조화 응답 | 어댑터의 모의 파서 시험은 있으나 실제 관찰 미검증; 허위 완료 위험 확인 필요 | `followup-windows` W02 |
| 대화형 승인·권한 필요 | 승인 대기/기존 프로세스 재개 미구현; 정보 보완 후 새 호출 경로 사용 | W02에서 미지원 범위·설정 수정/재실행 안내 검증 |
| 도구 출력 인코딩 | Codex stdout 성공·CLI JSON ASCII 이스케이프 수정과 별개 | W03에서 도구 원시 바이트와 디코딩·저장 근거 대조 |
| Ctrl+C·시간 초과·실제 자식 정리 | 트리 종료 시도와 불확실 시 후속 실행 차단 구현; 실제 정리 미검증 | W04에서 PID·부분 변경 확인 후 수동 복구 |
| 기본값·사용량/품질 비교·사용성 | 일부 사용량 관측은 통제 비교·첫 사용자 평가의 대체가 아님 | W07/W08/W09 |

시험 ID와 구체적 절차는 [WINDOWS_ACCEPTANCE_CHECKLIST.md](WINDOWS_ACCEPTANCE_CHECKLIST.md)에 있다. 우선 후속 작업은 `followup-windows`의 W01~W06이다. 아래 재시험을 수행할 때는 일반 PowerShell에서 격리된 시험 프로젝트를 사용한다. 두 점검 모두 Codex 호출이 발생한다. 결과에는 모델 응답과 로컬 경로가 포함될 수 있으므로 공유 전에 확인한다.

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

이 두 점검으로 대화형 승인, `turn.failed`, 모델 출력의 스키마 위반, 대표 작업 간 사용량 비교와 UI 사용성은 확인되지 않는다. 대표 작업 측정에서는 같은 시작 상태와 모델·설정으로 장기 대화와 작업별 인계를 비교하고 총 사용량·성공 여부·재작업·시간을 함께 기록한다. 화면은 P3에서 구현됐으며 첫 사용자 3명 평가는 W07로 남는다.

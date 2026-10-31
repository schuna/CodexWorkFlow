# P2 공통 실행 서비스와 CLI

2026-11-01 구현. Python 표준 라이브러리, Git, 설치·인증된 Codex CLI를 사용한다. `ExecutionService`는 GUI에서도 호출할 수 있는 동기 API이며 HTTP 서버와 GUI는 P3 범위다.

## 사용법

저장소 루트에서 실행한다. 대상 프로젝트는 Git 작업 트리여야 한다.

```powershell
python -m workflow_core --project C:\path\to\project init C:\path\to\plan.json
python -m workflow_core --project C:\path\to\project status
python -m workflow_core --project C:\path\to\project run
python -m workflow_core --project C:\path\to\project run task-1
python -m workflow_core --project C:\path\to\project verify task-1
python -m workflow_core --project C:\path\to\project handoff task-1
```

계획 파일 예시다. 검증 명령은 사용자가 검토한 인수 배열로 지정하며 셸 문자열로 평가하지 않는다. PowerShell 명령이 필요한 경우 `powershell.exe`, `-NoProfile`, `-Command`, 명령 문자열을 배열에 명시한다. 검증 명령 자체는 로컬 사용자 권한으로 실행된다.

```json
{
  "goal": "프로젝트 목표",
  "policy": {"max_retries": 1, "max_seconds": 600, "verification_seconds": 300},
  "tasks": [{
    "id": "task-1", "title": "첫 작업", "status": "ready",
    "acceptance_criteria": ["요구된 동작과 테스트를 구현한다"],
    "dependencies": [], "constraints": [], "verification": [],
    "verification_commands": [["python", "-m", "unittest", "discover", "-s", "tests", "-v"]],
    "changed_files": [], "failed_approaches": [], "next_action": "작업 실행"
  }]
}
```

`init`은 검토된 계획을 저장한다. 작업 자동 분할과 검증 명령 추천은 아직 제공하지 않는다. 위 시간 값은 설정 예시이며 측정으로 확정된 제품 기본값이 아니다. 기존 P1 API로 구성한 상태도 그대로 사용할 수 있고 `verification_commands`를 추가하면 된다.

`run`은 의존성이 완료된 작업이 하나면 선택한다. 여러 개이면 ID 선택을 요구한다. 기본 실행은 독립된 `codex exec --ephemeral --json --sandbox workspace-write`이며 지침은 Codex가 읽는다. 실행 후 검증·상태·PROGRESS.md·인계를 자동 저장한다. 다음 작업은 `status`의 `ready`에서 확인하고 시작한다. 자동 연속 실행, 자동 수정 재시도와 세션 재개는 제공하지 않는다. 추가 실행에는 다시 `run`이 필요하다.

`completed`는 프로세스 성공, `turn.completed`, 유효한 구조화 최종 응답, 모든 수용 기준에 대한 참 응답, 지정 검증의 성공을 함께 요구한다. 수용 기준은 모델의 보고이며 의미적 정확성을 보장하지 않는다. 테스트와 결과 검토가 필요하다. 검증이 없으면 `unverified`, 질문·권한 등이 필요하다고 보고하면 `needs_input`, 실패는 `failed`다. 빈 수용 기준은 실행 전에 거부한다. `verify`만 실행해서 미실행 작업을 완료로 올리지 않는다.

CLI 출력은 JSON이다. `run`·`verify`·`recover`는 대상 작업이 완료이면 종료 코드 0, 미완료이면 1이다. 입력·저장·잠금·충돌 오류는 2다. `record`는 기록 성공이면 0이며 작업 완료 여부와 구분한다.

## 중단과 기록 복구

실행 전체에 프로젝트별 OS 파일 잠금을 유지하므로 다른 CLI·서비스의 중복 시작을 막는다. 실행 단계와 결과는 `.codex-workflow/execution.json`에 원자적으로 저장한다. `status`는 상태를 조회하며 새 프로세스를 시작하지 않는다. 실행 중 상태 리비전이 바뀌면 완료 기록을 차단한다.

```powershell
python -m workflow_core --project C:\path\to\project record
```

결과가 저장된 `recording` 단계에서는 저장 문제를 해결한 뒤 위 명령으로 기록만 재시도한다. 같은 실행 ID와 확정된 상태 내용을 재사용하므로 이벤트 추가 후 요약 작성이 실패해도 Codex나 검증을 다시 실행하지 않는다. 인계 저장 실패도 같은 방식으로 재시도한다.

실행 중 서비스가 종료됐거나 프로세스 트리 종료가 확인되지 않으면 후속 실행을 막는다. 사용자가 남은 Codex·검증 자식 프로세스와 부분 변경을 확인한 뒤 다음 명령으로 중단 상태를 기록한다.

```powershell
python -m workflow_core --project C:\path\to\project recover --processes-checked
```

복구 자체는 코드를 실행하거나 되돌리지 않는다. 복구 후 별도의 `run task-1`으로 한도 내에서 다시 실행할 수 있다. Ctrl+C·시간 제한은 Windows에서 해당 PID의 트리 종료를 시도하고 부모 프로세스 종료를 보완한다. 정리가 불확실하면 `needs_process_review`에 머문다. `--processes-checked`는 실제 확인을 대신하는 자동 탐지가 아니다.

## 한도와 근거

- `max_retries`: 작업 최초 실행 이후 허용할 추가 실행 수. 기본 1이며 자동으로 재시도하지 않는다. 실패·중단도 시도를 소비한다.
- `max_seconds`: Codex 프로세스 시간 제한. 미설정이면 제한하지 않는다.
- `verification_seconds`: 검증 명령별 제한. 기본 300초이며 취소·시간 초과 시 다음 검증을 시작하지 않는다.
- `context_char_limit`: 작업 맥락 JSON의 문자 수 제한. 토큰 추정과 구분한다.
- `task_token_limit`: 누적 입력+출력 토큰으로 다음 호출 전에 검사한다. 캐시 입력은 입력에 포함되므로 중복 합산하지 않는다. 사용량이 결측인 이전 실행이 있으면 설정된 토큰 한도 아래 추가 실행을 막는다. 실행 중 토큰 상한을 보장하지 않으며 완료 후 초과를 표시한다.

사용량은 `turn.completed`에서 관측한 값만 저장하고, 미제공 필드·모델은 `null`(알 수 없음)이다. 지정 모델과 출처, 시각, 경과 시간을 함께 기록한다. 원시 Codex 이벤트·stderr·검증 출력은 영구 저장하지 않는다. 임시 출력은 메모리 사용을 제한하며, Codex 이벤트가 4MiB를 넘으면 부분 파싱을 근거로 완료를 선언하지 않는다. 정형 최종 응답과 명령 인수에도 민감정보를 넣지 않아야 한다.

Git HEAD와 기존 변경 파일의 상태·해시를 실행 전후 보존한다. 기존 변경과 달라진 경로를 인계에 기록하며 코드를 되돌리거나 커밋하지 않는다. 같은 파일의 외부 동시 편집과 Codex 변경의 작성자를 완전히 구분하지는 못한다. 서브모듈 내부 변경과 Git에서 무시한 파일은 완전한 변경 추적 대상이 아니다.

## 검증 결과와 제한

`python -m unittest discover -s tests -v`: P1 포함 21개 테스트 통과. 중복 잠금, 실패·미검증, 시간 초과, 시작 기록 실패 복구, 기록 실패 후 이벤트 복구와 중복 방지, 중단 복구, 프로세스 확인 차단, 리비전 충돌, 실행 한도, 사용량, CLI 조회·인계를 포함한다.

`python scripts/p2-smoke.py`: 격리된 Git 저장소에서 실제 Codex 호출 한 번을 수행한다. 2026-11-01 일반 샌드박스 시도는 종료 코드 1·사용량 결측으로 실패 기록됐고, 확장 실행 환경에서는 구조화 응답→검증→자동 기록→인계까지 성공했다. 입력 17,614, 캐시 입력 0, 출력 39, 총 17,653토큰; 모델 식별은 결측이다. 이는 파일 변경 없는 전송·기록 시험으로 실제 코드 수정의 품질이나 비용 절감률을 검증하지 않는다. 결과 폴더는 `p2-results-20261101-062347`이며 원시 응답 전문은 보관하지 않았다.

이 환경에서는 TEMP/TMP를 쓰기 가능한 작업 공간으로 지정해 테스트했다. `P0_REVIEW.md`의 미검증 항목은 별도로 남는다. 저장소 외부 편집 충돌 해결, 중단 프로세스 자동 탐지, 이벤트·상태 마이그레이션, 로그 보존 정책, HTTP 서비스·GUI, 승인 대기·상호작용 재개는 후속 범위다. 실행 중 정형 상태 변경으로 생긴 리비전 충돌은 자동 병합하지 않으며 별도 해결 API가 아직 없다.

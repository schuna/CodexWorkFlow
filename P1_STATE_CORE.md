# P1 상태 코어

`workflow_core.WorkflowStore`는 로컬 프로젝트의 상태, 이벤트, 인계와 작업별 맥락을 다루는 Python 표준 라이브러리 API다. GUI·실행 CLI와 Codex 연동은 P2/P3에서 구현됐다.

## 현재 소스 대조 (2026-11-01)

최신 시험·지원 환경·전체 수용표와 후속 시험 ID는 [WINDOWS_ACCEPTANCE_CHECKLIST.md](WINDOWS_ACCEPTANCE_CHECKLIST.md)를 따른다. 해결됨은 구현과 명시된 개발자 시험 범위이며 전체 MVP 수용을 뜻하지 않는다.

| 항목 | 판정 | 현재 근거·남은 확인 |
| --- | --- | --- |
| 잠금·기대 리비전·중복 방지·확정 이벤트 복구 | 해결됨 | `store.py`의 `_lock`, `update`, `_read_events`, `_materialize`; `tests/test_store.py`. Windows 파일 공유·강제 중단 실환경은 W05 |
| 기존 진행 문서 가져오기·외부 편집 충돌 UI | 해결됨 (P3) | `initialize`, `inspect_progress`, `resolve_progress`; `web.py`의 `save_plan`, `resolve`; `test_web.py`의 초기 가져오기·원본 보존·해시 충돌 시험. 실환경 W05 |
| 검증 실행·사용량 측정 | 해결됨 (P2/P3) | `execution.py`의 `_verify`, `telemetry.py`; 실패·재시도 누적과 결측 구분은 `test_usage_diagnostics.py`. 실제 모델 총량·품질 비교는 미검증 W08 |
| 작업 명세·인계 중복 제거·실패 근거 전달 | 해결됨 | `build_context`와 `tests/test_usage_diagnostics.py`; 최근 실패 3개와 제한된 검증 근거 전달, 원문 상태·인계 보존 |
| 필수 인계 필드·중요 제약의 의미적 정확성 | 형식 검사는 해결됨 / 의미는 미검증 | `_build_handoff`; 샘플 실제 작업과 사용자 평가 W06/W07 |
| 결정·질문의 관련성별 선택 | 미구현 | 프로젝트 공통 항목을 보존함. W09에서 필요성과 누락 위험 검토 |
| 상태 마이그레이션·이벤트 용량/보존 정책·손상 이벤트 복구 UI | 미구현 | 버전 1 검증·손상 오류 보고는 존재. W09/W10; 손상 확정 이벤트를 자동 삭제하지 않음 |

아래 API 설명은 현재 계약이며, 마지막 문단의 P1 단계 제한은 당시 범위를 보존한 기록이다.

```python
from workflow_core import WorkflowStore

store = WorkflowStore(r"C:\path\to\project")
state = store.initialize("프로젝트 목표")
state["tasks"].append({
    "id": "task-1", "title": "첫 작업", "status": "ready",
    "acceptance_criteria": ["검증 통과"], "dependencies": [], "verification": [],
    "constraints": [], "changed_files": [], "failed_approaches": [],
    "next_action": "작업 실행",
})
state = store.update(state, expected_revision=0, operation_id="unique-operation-1")
handoff = store.handoff("task-1")
context = store.context("task-1")
```

`initialize`는 기존 `PROGRESS.md`를 덮어쓰지 않는다. `update`는 기대 리비전을 검사하고 같은 operation ID와 같은 내용을 재시도하면 중복 기록하지 않는다. 서로 다른 내용으로 ID를 재사용하면 충돌한다. 작업 간 의존성은 존재 여부와 순환을 검사한다.

이벤트는 체크섬과 상태 스냅샷을 포함하며 추가·동기화된 줄을 확정 기록으로 취급한다. 중단된 마지막 불완전 줄은 버리고, 확정 이벤트로 `state.json`과 `PROGRESS.md`를 복구한다. 완결된 이벤트의 손상은 자동 삭제하지 않고 오류로 보고한다. `PROGRESS.md` 외부 수정은 충돌로 보고해 덮어쓰지 않는다. 파일 잠금은 이 API를 사용하는 프로세스 사이에서만 유효하며, 외부 편집기는 잠금을 따르지 않는다.

인계는 필수 필드 존재·형식을 검사하고 리비전별 파일로 저장한다. 작업 ID는 디렉터리 탈출을 막기 위해 해시로 변환한다. 맥락에는 선택한 작업과 인계만 포함하고 이벤트 이력이나 `AGENTS.md` 내용을 복사하지 않는다. 현재 인계의 결정·미해결 사항은 프로젝트 공통 항목을 포함하므로 관련성별 선택은 후속 개선 대상이다.

P1 단계 당시 기록: 검증 명령은 `python -m unittest discover -s tests -v`이며 실제 테스트는 임시 디렉터리에서 파일을 생성한다. 당시에는 기존 `PROGRESS.md`의 가져오기·충돌 해결 UI, 검증 실행, 인계 내용의 의미적 정확성 판단, 사용량 측정을 구현하지 않았다. 현재 해결 여부는 위 표로 구분한다. 이벤트 로그는 전체 상태 스냅샷을 보관하므로 프로젝트 규모에 따라 용량과 보존 정책을 별도로 검토해야 한다. 저장 자료에는 사용자가 제공한 상태와 인계 내용이 그대로 들어가므로 비밀값을 넣지 않아야 한다.

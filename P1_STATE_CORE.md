# P1 상태 코어

`workflow_core.WorkflowStore`는 로컬 프로젝트의 상태, 이벤트, 인계와 작업별 맥락을 다루는 Python 표준 라이브러리 API다. GUI·실행 CLI와 Codex 연동은 P2 이후 범위다.

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

검증 명령: `python -m unittest discover -s tests -v`. 실제 테스트는 임시 디렉터리에서 파일을 생성한다. 기존 `PROGRESS.md`의 가져오기·충돌 해결 UI, 검증 실행, 인계 내용의 의미적 정확성 판단, 사용량 측정은 구현하지 않았다. 이벤트 로그는 전체 상태 스냅샷을 보관하므로 프로젝트 규모에 따라 용량과 보존 정책을 별도로 검토해야 한다. 저장 자료에는 사용자가 제공한 상태와 인계 내용이 그대로 들어가므로 비밀값을 넣지 않아야 한다.

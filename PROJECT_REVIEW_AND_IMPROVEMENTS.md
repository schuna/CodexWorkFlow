# CodexWorkFlow 프로젝트 종합 검토 및 개선 제안 보고서

기준일: 2026-11-01  
기준 브랜치/커밋: `main` (`5cb9b34`)

---

## 1. 프로젝트 개요 및 목적

- **프로젝트명**: **CodexWorkFlow (Codex GUI·CLI 워크플로우)**
- **핵심 목적**:
  - AI 코딩 에이전트(Codex)와의 장기 대화 세션에서 발생하는 **불필요한 이력(Context) 재전달, 토큰 비용 증가, 문맥 오염(Context Drift)** 문제를 방지합니다.
  - 작업을 검증 가능한 단위(**Task Boundary**)로 분할하고, 선행 작업의 핵심 결과만 **인계(Handoff)**하여 다음 실행에 독립적으로 전달하는 로컬 워크플로우 자동화 도구입니다.
- **대상 환경**: Windows 로컬 단일 사용자 / 단일 프로젝트 환경.

---

## 2. 아키텍처 및 핵심 모듈 구성 (`workflow_core/`)

로컬 웹 GUI와 CLI가 **동일한 상태 저장소(`WorkflowStore`) 및 실행 서비스(`ExecutionService`)**를 공유하는 구조입니다.

```text
웹 GUI (포트 8765) ──┐
                    ├── 로컬 실행 서비스 (execution.py) ── 상태 저장소 (store.py)
터미널 CLI (python) ─┘          │                             │
                               ├── Codex 어댑터 (codex exec)  ├── .codex-workflow/ (state.json, events.jsonl, handoffs/)
                               └── 검증/Git 명령 어댑터        └── PROGRESS.md (사람용 요약본 자동 동기화)
```

| 모듈/파일 | 주요 역할 및 특징 |
| :--- | :--- |
| `workflow_core/store.py` | **상태 코어 (P1)**: 파일 잠금, 원자적 쓰기(`_atomic_write`), 이벤트 로그(`events.jsonl`) 복구, 리비전 관리, `PROGRESS.md` 자동 렌더링 및 외부 편집 충돌 감지 |
| `workflow_core/execution.py` | **실행 엔진 (P2)**: `codex exec` 비대화형 호출, 작업별 맥락 번들링, 지정 검증 명령(`verification_commands`) 실행 및 완료 판정, 중단/취소 복구 |
| `workflow_core/discovery.py` | Windows 환경에서 `PATH`뿐만 아니라 `AppData`, npm 글로벌, VS Code 확장 경로에 설치된 `codex.exe` 자동 탐색 |
| `workflow_core/web.py` & `ui/` | **로컬 웹 GUI (P3)**: 별도 빌드 과정이 없는 Vanilla HTML/CSS/JS 기반 대시보드. 작업 보드, 계획 검토, 실행/중단, Diff 확인, 충돌/중단 복구 지원 |
| `workflow_core/diagnostics.py` | 검증 실패 시 출력 로그에서 핵심 예외/오류 요약 추출 및 민감정보(API 키, 토큰, 개인키 등) 자동 정제(Redaction) |
| `workflow_core/telemetry.py` | 작업별·프로젝트별 토큰 사용량(`input_tokens`, `cached_input_tokens`, `output_tokens`) 합산 및 미측정 실행 구분 |
| `workflow_core/__main__.py` | CLI 엔트리포인트 (`init`, `status`, `handoff`, `context`, `run`, `verify`, `record`, `recover`, `stop`, `gui`) |

---

## 3. 프로젝트의 장점 (Strengths)

### ① 높은 수준의 데이터 무결성 및 장애 복구(Crash-Resilience) 설계
- **원자적 저장 및 크래시 복구**: `WorkflowStore`는 `.codex-workflow/events.jsonl`을 단일 진실 공급원(Single Source of Truth)으로 사용합니다. 모든 이벤트에 SHA-256 체크섬과 `operation_id`(멱등성 보장)를 부여하며, 쓰기 도중 비정상 종료되더라도 미완성된 마지막 줄을 자동으로 절단(Truncate)하고 복구합니다.
- **Windows 파일 잠금 배려**: `_atomic_write`에서 `os.fsync` 후 파일 교체 시 Windows 특유의 일시적 파일 점유 오류(`winerror 5, 32, 33`)에 대해 지수 백오프(Exponential Backoff) 재시도를 수행하여 안정성이 높습니다.
- **외부 편집 보호**: 사용자가 `PROGRESS.md`를 외부 편집기로 직접 수정한 경우 이를 덮어쓰지 않고 해시 비교로 충돌을 감지하며, 원본을 `.codex-workflow/conflicts/`에 안전하게 백업합니다.

### ② 외부 의존성 제로(Zero-Dependency)의 경량 아키텍처
- Python 표준 라이브러리와 순수 HTML/CSS/Vanilla JS만으로 CLI와 로컬 웹 서버를 구현했습니다.
- 별도의 패키지 설치(`pip install`)나 프론트엔드 번들링(`npm build`) 없이 Python 표준 환경만으로 즉시 실행할 수 있어 배포와 유지보수가 매우 간편합니다.

### ③ 철저한 보안 및 방어적 실행 통제
- **쉘 인젝션 원천 차단**: 모든 외부 프로세스를 `shell=True` 없이 인자 배열(`argv`)로만 실행하며, Windows 명령줄 파싱 시 Win32 네이티브 API(`CommandLineToArgvW`)를 사용합니다.
- **로컬 웹 보안**: `127.0.0.1` 루프백 바인딩, `Host`/`Origin` 헤더 검증, 서버 구동 시 생성되는 난수 토큰(`X-Workflow-Token`), 엄격한 CSP 헤더를 통해 CSRF 및 DNS Rebinding 공격을 방어합니다.
- **민감정보 자동 정제 및 경로 보호**: 검증 실패 로그 저장 전 정규식 필터로 주요 크레덴셜을 마스킹하며, Handoff 파일 경로 생성 시 `task_id`를 SHA-256 해시하여 Path Traversal을 방지합니다.

### ④ "검증 기반 완료 판정"과 프롬프트 다이어트
- 에이전트가 스스로 완료(`criteria_met: true`)를 보고하더라도 지정된 **검증 명령(`verification_commands`)이 실제로 통과(종료 코드 0)하지 않으면 완료(`completed`)로 전이되지 않는 엄격한 구조**를 갖추고 있습니다.
- `WorkflowStore.build_context`가 불필요한 실행 통계나 오래된 실패 이력을 제외하고, 최근 실패 3건과 핵심 요약만 다음 프롬프트에 주입하여 토큰 낭비와 문맥 오염을 줄입니다.

---

## 4. 프로젝트의 단점 및 현재 한계 (Weaknesses)

### ① 선행 작업(Dependency) 결과의 자동 인계 부재
- `_build_handoff`와 `build_context`는 **현재 실행할 작업(`task_id`) 자신의 이력**과 전역 결정(`decisions`)만 프롬프트로 구성합니다.
- 선행 작업 `Task A`가 완료된 후 이를 의존성(`dependencies`)으로 가진 후속 작업 `Task B`가 실행될 때, **`Task A`에서 변경된 파일(`changed_files`)이나 수행 요약(`last_result.summary`)**이 `Task B`의 입력 맥락에 자동으로 포함되지 않습니다.

### ② 이벤트 로그의 풀 스냅샷 저장으로 인한 $O(N^2)$ 확장성 한계
- `_append`가 매 상태 변경마다 전체 `state` 객체 사본을 `events.jsonl`에 기록하고, `_read_events`는 매 조회마다 파일의 첫 줄부터 끝 줄까지 전체 파싱 및 SHA-256 검증을 수행합니다.
- 작업 수와 실행 이력이 누적될수록 `events.jsonl` 파일 크기가 급격히 커지며, 장기 프로젝트에서는 상태 조회 및 UI 폴링 시 디스크 I/O와 CPU 지연이 발생할 수 있습니다.

### ③ 실행 중 실시간 진행 상황 피드백 부재 (블랙박스 대기)
- `process()` 함수가 자식 프로세스 종료 시까지 출력을 임시 파일에 스풀링한 뒤 한 번에 읽어들입니다.
- Codex 실행이 수 분간 지속될 때 사용자는 웹 UI나 CLI에서 현재 에이전트가 어떤 작업을 수행 중인지 확인할 수 없고 `running` 상태만 보며 대기해야 합니다.

### ④ 작업 분할(`suggest`)의 단순성
- `WebController.suggest`는 사용자가 입력한 목표 문자열을 단순히 줄바꿈(`\n`) 기준으로만 분리하여 태스크 초안을 생성합니다. 복합적인 목표를 입력할 경우 사용자가 직접 세부 작업과 완료 기준을 수동으로 쪼개야 하는 부담이 있습니다.

### ⑤ 멀티스레드 HTTP 서버 내 `tkinter` 직접 호출 위험
- `Handler.do_POST`의 `/api/choose-folder` 엔드포인트는 `ThreadingHTTPServer`의 워커 스레드 내에서 `tkinter.Tk()`를 직접 생성합니다. Python의 Tcl/Tk는 메인 스레드가 아닌 복수의 워커 스레드에서 반복 생성·소멸할 경우 간헐적인 스레드 데드락이나 `RuntimeError`를 유발할 수 있습니다.

---

## 5. 우선순위별 개선 제안 (Actionable Improvements)

### 1순위 (기능/품질): 선행 의존 작업의 인계 요약 자동 주입
- **대상**: `workflow_core/store.py` (`WorkflowStore.build_context`)
- **개선안**: 실행할 작업의 `dependencies`에 명시된 선행 완료 작업들의 핵심 결과(`id`, `title`, `changed_files`, `last_result.summary`)를 추출하여 `context["dependency_results"]` 필드로 함께 전달합니다.
- **기대 효과**: 작업 간 단절 없이 선행 작업의 변경 사항과 맥락을 정확히 이어받아 "작업 경계 간 인계"라는 핵심 설계 목적이 완성됩니다.

### 2순위 (UX/관측성): Codex JSON 스트림 기반 실시간 진행 상태 표시
- **대상**: `workflow_core/execution.py` (`process`, `CodexAdapter.execute`)
- **개선안**: `codex exec --json`이 출력하는 JSONL 이벤트 스트림을 `communicate(timeout=0.2)` 폴링 루프에서 증분(Tail)으로 읽어, 최근 발생한 이벤트 유형이나 에이전트 메시지를 런타임 상태에 업데이트하고 `/api/status`로 노출합니다.
- **기대 효과**: 장시간 실행 중에도 웹 UI에서 실시간 진행 상태를 확인할 수 있어 체감 응답성과 답답함이 크게 개선됩니다.

### 3순위 (안정성): 폴더 선택 대화상자(`tkinter`)를 서브프로세스로 분리
- **대상**: `workflow_core/web.py` (`Handler.do_POST` 내 `/api/choose-folder`)
- **개선안**: HTTP 요청 처리 스레드에서 직접 `tkinter`를 구동하는 대신, `subprocess.run([sys.executable, "-c", "..."])` 형태로 독립된 단기 프로세스를 실행하여 선택된 폴더 경로를 표준 출력으로 받아오도록 분리합니다.
- **기대 효과**: 멀티스레드 환경에서의 Tcl/Tk 데드락 및 런타임 오류 위험을 원천 제거합니다.

### 4순위 (성능/운영): 상태 로드 Fast-Path 및 이벤트 로그 Compaction 도입
- **대상**: `workflow_core/store.py` (`WorkflowStore._load`, `_read_events`)
- **개선안**:
  - 일반 조회(`read()`) 시에는 `events.jsonl`의 마지막 줄(최신 이벤트)만 읽어 `state.json`과 체크섬/리비전을 대조하는 **Fast-Path**를 적용하고, 손상이나 불일치 감지 시에만 전체 이벤트 로그를 순회합니다.
  - 이벤트 수가 임계치(예: 100개)를 넘으면 오래된 이벤트를 `.codex-workflow/archive/`로 분리·보관하는 Compaction 정책(`followup-policy` 연계)을 추가합니다.
- **기대 효과**: 작업 이력이 길어져도 일정한 $O(1)$ 조회 성능을 유지할 수 있습니다.

### 5순위 (편의성): AI 기반 작업 분할 초안 생성 (Smart Plan Suggest) 옵션
- **대상**: `workflow_core/web.py` (`WebController.suggest`)
- **개선안**: 기본 줄바꿈 분할 외에, 선택적으로 Codex를 읽기 전용 샌드박스(`--sandbox read-only`)로 짧게 호출하여 저장소 구조를 파악하고 목표에 맞는 세부 작업·의존성·검증 명령 초안을 자동 생성하는 기능을 추가합니다.
- **기대 효과**: 신규 사용자의 초기 계획 수립 진입 장벽을 크게 낮출 수 있습니다.

---

## 6. 개선 제안 구현 및 검증 결과 (1~5순위 완료)

| 순위 | 개선 항목 | 주요 변경 파일 | 검증 테스트 | 상태 |
| :--- | :--- | :--- | :--- | :--- |
| **1순위** | 선행 의존 작업의 인계 요약 자동 주입 (`dependency_results` 및 500자 요약 제한, 웹 UI 인계 탭 표시) | `workflow_core/store.py`, `workflow_core/ui/app.js` | `test_dependency_results_injected_for_dependent_tasks_only` | **완료** (`c403d06`) |
| **2순위** | Codex JSONL 실시간 스트리밍(`on_stdout_line`), 민감정보 정제 후 `execution.json` 및 웹 UI 실시간 진행 표시 | `workflow_core/execution.py`, `workflow_core/ui/app.js`, `workflow_core/ui/style.css` | `test_process_streams_stdout_lines_and_adapter_reports_redacted_progress` | **완료** (`a8ca33f`) |
| **3순위** | `tkinter` 폴더 선택 대화상자를 자식 프로세스(`choose_folder`)로 격리 및 유니코드 안전 JSON 전달 | `workflow_core/web.py` | `test_choose_folder_runs_in_isolated_subprocess_and_handles_unicode_and_errors` | **완료** (`aecb53b`) |
| **4순위** | $O(1)$ Tail Fast-Path 상태 조회(`_load_fast`) 및 이벤트 로그 Compaction(`compact_events`, `max_events_retained`) | `workflow_core/store.py` | `test_fast_path_read_and_event_compaction_preserve_integrity` | **완료** (`56e3cd9`) |
| **5순위** | 읽기 전용 Codex(`--sandbox read-only`) 기반 AI 작업 분할 초안 생성(`suggest(..., use_ai=True)`) 및 DAG 정규화 | `workflow_core/web.py`, `workflow_core/ui/index.html`, `workflow_core/ui/app.js` | `test_ai_plan_suggest_uses_read_only_sandbox_and_normalizes_dag` | **완료** |

- **최종 회귀 검증**: `python -m unittest discover -s tests -v` **63/63 통과**, `node --check workflow_core/ui/app.js` 통과, `git diff --check` 통과.

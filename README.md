# anomaly-mcp

반도체 센서 데이터용 **이상감지 시스템**입니다. 웹 대시보드로 실시간 상태를 보고, 같은 기능을 **MCP 서버**로 Claude·사내 에이전트에 제공합니다.
대시보드 오른쪽 **채팅**에서 자연어로 질문하면 Claude가 같은 MCP tool로 데이터를 조회해서 답합니다.
통계 탐지기(빠르고 무료)로 *어디가* 이상한지 찾고, Claude API로 *왜* 이상한지와 점검 항목을 분석합니다.

```
 브라우저 (대시보드)          MCP Client (Claude Code / Desktop / 사내 에이전트)
        │ HTTP                     │ Streamable HTTP (/mcp)      │ stdio
        ▼                          ▼                             ▼
 ┌──────────── anomaly-dashboard (단일 프로세스) ────────────┐   anomaly-mcp
 │ /api/chat  ──►  chat.py      (MCP 호스트: /mcp에 MCP로 접속 → │   (대시보드 없이
 │                               Claude tool-use 루프)          │    MCP만 사용)
 │ /  /api/*  ──►  monitor.py   (2초마다 전 센서 평가, 알람 이벤트) │
 │ /mcp       ──►  server.py    (MCP tools)                   │
 │                 detectors.py (zscore, MAD, IQR, rolling,   │
 │                               EWMA, CUSUM, stuck, spec,    │
 │                               Mahalanobis)                 │
 │                 llm.py       (Claude 원인 분석)              │
 │                 store.py     (센서 저장소 — 대시보드와 MCP가 공유) │
 │                 simulator.py (데모용 식각/CVD 챔버 데이터)      │
 └────────────────────────────────────────────────────────────┘
```

대시보드와 MCP가 **같은 저장소와 모니터 상태를 공유**합니다. MCP로 `ingest_readings`한 데이터는 대시보드에 바로 보이고,
대시보드에서 실행한 Claude 분석이나 알람 이벤트는 MCP tool로 조회할 수 있습니다.

## 설치

```bash
cd anomaly-mcp
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env    # ANTHROPIC_API_KEY 입력 (Claude 분석을 쓸 때만 필요)
```

> OneDrive 폴더에서 `uv`로 설치할 때 하드링크 오류가 나면 `uv pip install --link-mode=copy -e ".[dev]"`를 사용하세요.

동작 확인:

```bash
pytest
python examples/load_sample_data.py           # 식각 챔버 샘플 데이터로 탐지
python examples/load_sample_data.py --claude  # Claude 원인 분석까지
```

## 대시보드 실행

```bash
anomaly-dashboard                    # 데모 시뮬레이터 포함 → http://127.0.0.1:8000/
anomaly-dashboard --auto-fault 60    # 60초마다 임의 센서에 이상 패턴 자동 주입 (0이면 끔, 기본 150)
anomaly-dashboard --no-simulator     # 실데이터 운영: 수집기가 MCP/코드로 ingest
anomaly-dashboard --port 9000
```

화면 구성:

- **KPI** — 감시 센서 수, 알람(스펙 이탈) / 경고(통계적 이상) / 정상, 다변량 이상 설비 수
- **설비·챔버별 센서 카드** — 현재값, 스펙, 최근 추세 스파크라인, 탐지 사유
- **알람 이벤트** — 상태 전이(OK→WARNING→ALARM, 해제) 기록. 클릭하면 해당 센서 상세
- **센서 상세** — 추세 차트(LSL/USL, 탐지기별 마커, stuck 구간), 통계·Cpk, 탐지기 결과표,
  이상 샘플 표, 이상 패턴 주입(데모), **Claude 원인 분석**(추가 맥락 입력 가능)
- 상세 화면 링크 공유: `http://127.0.0.1:8000/#s=ETCH03_CHA_TEMP`

### 대시보드 채팅

오른쪽 패널(헤더의 **채팅** 버튼으로 열고 닫기)에서 "지금 알람 난 센서 이유 정리해줘", "ETCH-03 센서 간 상관관계 이상 있어?" 같은 질문을 할 수 있습니다.

- **구조** — 채팅창은 MCP 서버가 아니라 MCP를 *사용하는* 호스트입니다. 질문이 오면 서버(`chat.py`)가 MCP 클라이언트로
  `/mcp`에 접속해 tool 목록을 받아 Claude에 넘기고, Claude가 요청한 tool을 MCP로 실행합니다.
  그래서 Claude Code가 쓰는 tool과 채팅이 쓰는 tool이 항상 같고, `CHAT_MCP_SERVERS`에 URL을 추가하면 다른 MCP 서버(MES, 설비 이력 등)의 tool도 함께 씁니다.
- **맥락** — 현재 상태 요약과 대시보드에서 보고 있는 센서가 질문에 자동으로 붙습니다.
- **조회 전용** — 데이터를 바꾸는 tool(`register_sensor`, `ingest_readings`, `simulate_fault`)은 채팅에서 막혀 있습니다 (`CHAT_BLOCKED_TOOLS`).
- 답변 속 센서 ID를 누르면 해당 센서 상세가 열리고, 호출한 tool이 진행 상태와 함께 표시됩니다.
- 대화 이력은 서버 메모리에 보관됩니다(재시작 시 초기화, 최근 100개 대화).

외부 CDN을 쓰지 않는 단일 HTML이라 인터넷이 차단된 팹 네트워크에서도 동작합니다. 라이트/다크 테마를 지원합니다.

### 데모 시뮬레이터

ETCH-03/CH-A(RF Power, 챔버 온도, CF4 유량, 압력, ESC He)와 CVD-01/CH-B(히터 온도, SiH4 유량, 압력) 8개 센서를 1초마다 생성합니다.
온도는 RF Power를, 압력은 가스 유량을 따라 움직이도록 결합되어 있어 다변량(상관관계 붕괴) 탐지도 시연할 수 있습니다.

| 주입 패턴 | 시나리오 | 잡아내는 탐지기 |
|---|---|---|
| Spike | 순간 튐 | MAD, Rolling Z |
| Drift | 히터 열화, 챔버 오염 → 스펙 이탈 | CUSUM, Spec |
| Shift | 계단형 평균 이동 | CUSUM, Mahalanobis |
| Stuck | 센서 값 고정 | Stuck |
| Noise | 접촉 불량, 아킹 | MAD, Rolling Z |

## MCP 클라이언트 연결

### A. HTTP — 대시보드와 같이 쓰기 (권장)

`anomaly-dashboard`를 실행해 둔 상태에서:

**Claude Code**

```bash
claude mcp add --transport http anomaly http://127.0.0.1:8000/mcp
```

**Claude Desktop / 기타 클라이언트**

```json
{
  "mcpServers": {
    "anomaly": { "type": "http", "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

대시보드 우측 상단 **MCP 연결** 버튼에 현재 주소 기준 명령어가 표시됩니다.

### B. stdio — 대시보드 없이 MCP만

**Claude Code**

```bash
# macOS / Linux
claude mcp add anomaly -- /절대경로/anomaly-mcp/.venv/bin/anomaly-mcp
# Windows
claude mcp add anomaly -- C:\절대경로\anomaly-mcp\.venv\Scripts\anomaly-mcp.exe
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "anomaly": {
      "command": "/절대경로/anomaly-mcp/.venv/bin/anomaly-mcp",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```

stdio 모드는 클라이언트마다 별도 프로세스·별도 저장소를 가지므로 대시보드와 데이터가 공유되지 않습니다.

**디버깅**: `npx @modelcontextprotocol/inspector` 실행 후 `http://127.0.0.1:8000/mcp` 연결 (stdio는 `.venv/bin/anomaly-mcp`)

## 제공 기능

| Tool | 설명 |
|---|---|
| `list_methods` | 탐지 알고리즘 목록 / 기본 threshold |
| `detect_anomalies` | 값 배열을 직접 넘겨 탐지 (`method`, `threshold`, `params`) |
| `register_sensor` | 센서 메타데이터(단위, 설비, 챔버, LSL/USL) 등록 |
| `ingest_readings` | 센서 데이터 적재 |
| `check_sensor` | 여러 탐지기 + 스펙 체크 일괄 실행 → `OK / WARNING / ALARM` (탐지기별 `thresholds`, `params` 지정 가능) |
| `detect_multivariate` | 여러 센서 동시 탐지 (센서 간 상관관계 붕괴 감지) |
| `analyze_with_claude` | Claude가 패턴 분류, 센서 오류 여부, 원인 후보, 조치 항목 제시 (결과는 대시보드에도 표시) |
| `get_monitor_status` | *(HTTP 모드)* 대시보드와 동일한 센서별/설비별 상태 |
| `list_alarm_events` | *(HTTP 모드)* 최근 알람 이벤트 (상태 전이) |
| `simulate_fault` | *(HTTP 모드, 데모)* 시뮬레이터 센서에 이상 패턴 주입 |

Resource `sensors://list`, Prompt `investigate_sensor`(조사 워크플로)도 포함되어 있습니다.

Claude Code에서 이렇게 물어볼 수 있습니다: *"지금 알람 난 센서 있어? 있으면 원인 분석해서 정리해줘"* →
`get_monitor_status` → `check_sensor` → `detect_multivariate` → `analyze_with_claude` 순으로 조사하고, 분석 결과는 대시보드에도 남습니다.

### 탐지기 선택 가이드

| 상황 | 추천 |
|---|---|
| 순간 spike | `mad`, `rolling_zscore` |
| 서서히 변하는 drift (히터 열화, 챔버 오염) | `cusum`, `ewma` |
| 센서 값이 멈춤 | `stuck` |
| 공정 스펙 이탈 | `register_sensor`에 LSL/USL 설정 → `check_sensor` |
| 개별 센서는 정상인데 조합이 이상 | `detect_multivariate` (`warmup`에 정상 구간 길이 지정) |

### 상시 감시 기준 (`monitor.py`)

데이터가 계속 들어오는 상시 감시는 오탐을 줄이기 위해 대화형 `check_sensor` 기본값보다 보수적인 기준을 씁니다.

| 탐지기 | threshold | 비고 |
|---|---|---|
| MAD | 4.5 | |
| Rolling Z | 5.0 | window 30 |
| CUSUM | 12 | 기준 구간 150 샘플 (슬라이딩 윈도우 기준선 흔들림 대비) |
| Stuck | 10 연속 | |
| Mahalanobis (설비 단위) | 6.0 | 평가 구간 앞 절반을 정상 기준으로, 최근 60 샘플 안의 이상만 활성 |

시뮬레이터 기준으로 8개 센서 90분 동안 오탐 약 3건, 주입한 5가지 패턴은 모두 1~14초 안에 탐지됩니다.
판정: 평가 구간 안에 스펙 이탈이 있으면 **ALARM**, 통계적 이상만 있으면 **WARNING**.
설비 특성에 맞게 `PROFILE`을 조정하세요.

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Claude 분석용 API 키 |
| `CLAUDE_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5`(비용/속도 균형), `claude-haiku-5-5`(대량·실시간) |
| `CLAUDE_MAX_TOKENS` | `16000` | 원인 분석 응답 한도 (thinking 포함) |
| `CLAUDE_EFFORT` | `high` | 원인 분석 effort (`low`~`max`) |
| `CHAT_MODEL` | `CLAUDE_MODEL` 값 | 채팅 모델 |
| `CHAT_EFFORT` | `medium` | 채팅 effort |
| `CHAT_MAX_TOKENS` | `32000` | 채팅 응답 한도 |
| `CHAT_MCP_SERVERS` | | 채팅에 추가로 붙일 MCP 서버 URL (쉼표 구분) |
| `CHAT_BLOCKED_TOOLS` | `register_sensor,ingest_readings,simulate_fault` | 채팅에서 막을 tool |
| `DEFAULT_METHOD`, `DEFAULT_THRESHOLD` | `zscore`, `3.0` | `detect_anomalies` 기본값 |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | `127.0.0.1`, `8000` | 대시보드/MCP 바인드 주소 |
| `MONITOR_WINDOW` | `300` | 상시 감시 평가 구간 (샘플 수) |
| `MONITOR_INTERVAL` | `2` | 평가 주기 (초) |

토큰 절약을 위해 Claude에는 원본 시계열을 약 200포인트로 다운샘플링해서 보내고, 시스템 프롬프트는 프롬프트 캐싱을 적용했습니다.
`context`(대시보드의 "추가 맥락")에 PM 이력·레시피 변경·최근 알람 같은 정보를 넣으면 분석 품질이 크게 좋아집니다.

## 커스터마이징 포인트

1. **데이터 소스** — `store.py`의 `SensorStore`를 InfluxDB / TimescaleDB / FDC 시스템 조회로 교체. 수집기가 직접 쓰는 경우 `--no-simulator`로 실행하고 `ingest_readings`(MCP) 또는 `store.ingest()` 호출
2. **탐지기 추가** — `detectors.py`에 함수 작성 후 `METHODS`에 등록 (예: Isolation Forest, 레시피 스텝별 기준선). 상시 감시에 쓰려면 `monitor.PROFILE`에도 추가
3. **분석 프롬프트** — `llm.py`의 `SYSTEM_PROMPT`에 사내 설비 지식, 알람 코드표 추가
4. **출력 스키마** — `ANALYSIS_TOOL.input_schema`에 필드 추가 (예: 담당 부서, 설비 정지 필요 여부)
5. **알림 연동** — `Monitor._event()`에서 메신저/메일/MES로 알람 전송

## 보안

기본 바인드 주소는 `127.0.0.1`이며 이때 MCP 엔드포인트에 DNS rebinding 보호가 자동 적용됩니다.
`--host 0.0.0.0`으로 사내망에 공개하면 **인증이 없으므로** 리버스 프록시(사내 SSO, mTLS 등) 뒤에 두세요.
`analyze_with_claude`와 채팅은 센서 통계·tool 조회 결과를 Anthropic API로 전송합니다.
Claude 요청에는 거절(refusal) 시 다른 모델로 자동 재시도하는 서버 측 fallback(`fallbacks: "default"`)이 켜져 있습니다.

## 호환성

MCP Python SDK 1.x(`FastMCP`)와 2.x(`MCPServer`) 모두 지원합니다.

# Slack App Settings Guide

이 문서는 새 Slack workspace에 task-management 데모/운영용 Slack app을
설정할 때 확인해야 하는 설정 화면별 가이드입니다.

스크린샷은 이 문서의 `Screenshot placeholder` 위치에 추가하면 됩니다. Slack
설정 UI는 수시로 바뀔 수 있으므로, 화면의 정확한 문구가 조금 다르면 같은
의미의 메뉴를 기준으로 맞춥니다.

## 목표 구성

권장 시작점은 **Socket Mode + App Home DM**입니다.

- 외부 HTTPS callback URL 없이 Mac mini 같은 로컬 노드에서 WebSocket으로
  Slack 이벤트를 받습니다.
- 사용자는 Slack app DM 또는 App Home Messages tab에 자연어로 할 일을 씁니다.
- Home tab에는 현재 task dashboard 요약이 표시됩니다.
- 채널 감시는 선택 기능입니다. DM 데모가 먼저 안정화된 뒤 켭니다.

## 설정 산출물

설정이 끝나면 `ops/local_env_markdown_template.md`를 `.env.local.md`로
복사하고, 아래 항목별 값 칸에 새 Slack app 값을 붙여넣습니다. 실제
`.env.local.md` 파일은 Git에 올리지 않습니다.

```text
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_USER_ID=
SLACK_DM_CHANNEL_ID=
SLACK_BOT_USER_ID=
TASK_MANAGEMENT_STATE=.task-management-demo
TASK_MANAGEMENT_INSTANCE_ID=clean-demo
TASK_MANAGEMENT_ALLOWED_INSTANCE_ID=clean-demo
```

`SLACK_DM_CHANNEL_ID`와 `SLACK_BOT_USER_ID`는 처음부터 모르면 비워둘 수
있습니다. `slack-doctor --live-open-dm`으로 DM channel을 resolve한 뒤
고정하면 됩니다.

CLI는 현재 작업 디렉토리의 `.env.local`을 먼저, `.env.local.md`를 나중에
자동 로드합니다. 빈 값도 기존 shell 환경변수를 덮어쓰므로, clean 설치가
다른 운영 머신의 Slack token을 상속하지 않습니다.

## 0. Slack app 생성

1. <https://api.slack.com/apps>로 이동합니다.
2. **Create New App**을 선택합니다.
3. **From scratch**를 선택합니다.
4. 앱 이름을 정합니다. 예: `Task Management Demo`.
5. 설치할 workspace를 선택합니다.

> Screenshot placeholder: `docs/screenshots/slack-app-settings/00-create-app.png`

체크포인트:

- App management dashboard에 진입할 수 있어야 합니다.
- 좌측 메뉴에 **Basic Information**, **App Home**, **OAuth & Permissions**,
  **Socket Mode**, **Event Subscriptions**가 보여야 합니다.

## 1. Basic Information

위치: **Settings > Basic Information**

확인할 것:

- App ID
- Client ID / Client Secret은 이 repo의 기본 demo path에서는 직접 쓰지 않습니다.
- Signing Secret도 Socket Mode 기본 path에서는 직접 쓰지 않습니다.
- **App-Level Tokens** 섹션에서 Socket Mode용 token을 만듭니다.

### App-Level Token 생성

1. **Generate Token and Scopes**를 선택합니다.
2. token 이름을 입력합니다. 예: `task-management-socket`.
3. App-level scope로 `connections:write`를 추가합니다.
4. 생성된 token을 복사합니다.
5. 로컬 환경변수 `SLACK_APP_TOKEN=xapp-...`에만 저장합니다.

> Screenshot placeholder: `docs/screenshots/slack-app-settings/01-basic-information-app-token.png`

주의:

- `SLACK_APP_TOKEN`은 `xapp-`로 시작해야 합니다.
- GitHub, 문서, 채팅, DB에 token 값을 붙여넣지 않습니다.

## 2. Socket Mode

위치: **Settings > Socket Mode**

1. **Enable Socket Mode**를 켭니다.
2. Slack이 app-level token을 요구하면, 위 단계에서 만든
   `connections:write` token을 사용합니다.
3. 저장합니다.

> Screenshot placeholder: `docs/screenshots/slack-app-settings/02-socket-mode.png`

체크포인트:

- Socket Mode가 켜져 있으면 Event Subscriptions에서 Request URL을 넣지 않아도
  됩니다.
- 이 repo의 `slack-socket-loop`는 `SLACK_APP_TOKEN`으로 WebSocket 연결을
  엽니다.

## 3. App Home

위치: **Features > App Home**

권장 설정:

1. **Home Tab**을 켭니다.
2. **Messages Tab**을 켭니다.
3. Slack UI에 별도 toggle이 있으면, 사용자가 app에 메시지를 보낼 수 있도록
   허용합니다.
4. 필요하면 display name, description, icon을 demo용으로 정리합니다.

> Screenshot placeholder: `docs/screenshots/slack-app-settings/03-app-home-tabs.png`

체크포인트:

- Slack app을 열었을 때 **Home**, **Messages**, **About** 탭을 볼 수 있어야
  합니다.
- 사용자가 Messages tab 또는 app DM에서 메시지를 보낼 수 있어야 합니다.
- Home tab 갱신을 이벤트 기반으로 확인하려면 `app_home_opened` event도
  구독합니다.

참고:

- Slack의 일부 UI에서는 AI/Agent 관련 기능을 켜면 Messages tab 이름이나 구조가
  달라질 수 있습니다. 이 repo의 기본 데모에는 일반 App Home + Messages tab이면
  충분합니다.

## 4. OAuth & Permissions

위치: **Features > OAuth & Permissions**

### 필수 Bot Token Scopes

DM 중심 demo path에 필요한 bot scope:

| Scope | 이유 |
| --- | --- |
| `chat:write` | Slack DM/confirmation/briefing 메시지를 보냅니다. |
| `im:history` | app DM의 `message.im` 이벤트와 DM history를 읽습니다. |
| `im:write` | 사용자와 app 사이의 DM channel을 열 수 있습니다. |
| `reactions:write` | 받은 메시지에 읽음(👀)·완료(✅) 리액션을 답니다. |

> Screenshot placeholder: `docs/screenshots/slack-app-settings/04-oauth-required-scopes.png`

### 선택 Bot Token Scopes

채널 감시 데모를 할 때만 추가합니다.

| Scope | 켤 때 |
| --- | --- |
| `app_mentions:read` | bot이 언급된 채널 메시지만 task 후보로 보고 싶을 때 |
| `channels:history` | bot이 들어간 public channel의 모든 메시지 이벤트를 받을 때 |
| `groups:history` | bot이 들어간 private channel의 메시지 이벤트를 받을 때 |

기본 운영 방침:

- 처음에는 선택 scope를 넣지 말고 DM path만 검증합니다.
- 채널 감시는 allowlist와 mention-required 정책을 함께 씁니다.
- public/private channel message history scope는 읽기 범위를 넓히므로 데모 목적이
  분명할 때만 추가합니다.

### Install / Reinstall

scope를 추가하거나 제거한 뒤에는:

1. **Install to Workspace** 또는 **Reinstall to Workspace**를 누릅니다.
2. 권한 요청 화면에서 scope 목록을 확인합니다.
3. 설치 후 **Bot User OAuth Token**을 복사합니다.
4. 로컬 환경변수 `SLACK_BOT_TOKEN=xoxb-...`에만 저장합니다.

> Screenshot placeholder: `docs/screenshots/slack-app-settings/05-install-permissions.png`

주의:

- `SLACK_BOT_TOKEN`은 `xoxb-`로 시작해야 합니다.
- `SLACK_APP_TOKEN`은 `xapp-`로 시작해야 합니다.
- reinstall 후 token이 바뀌었는지 확인하고, 바뀌었으면 로컬 `.env.local`을
  갱신합니다.

## 5. Event Subscriptions

위치: **Features > Event Subscriptions**

1. **Enable Events**를 켭니다.
2. Socket Mode를 쓰는 경우 Request URL은 비워둘 수 있습니다.
3. **Subscribe to bot events**에서 필요한 event를 추가합니다.

### 필수/권장 event

| Event | 목적 |
| --- | --- |
| `message.im` | app DM 메시지를 수신합니다. |
| `app_home_opened` | 사용자가 Home tab을 열 때 Home view를 갱신합니다. |

> Screenshot placeholder: `docs/screenshots/slack-app-settings/06-event-subscriptions-dm.png`

### 선택 channel triage event

채널 감시를 켤 때만 추가합니다.

| Event | 필요한 scope | 목적 |
| --- | --- | --- |
| `app_mention` | `app_mentions:read` | bot이 언급된 메시지만 받습니다. |
| `message.channels` | `channels:history` | public channel 메시지를 받습니다. |
| `message.groups` | `groups:history` | private channel 메시지를 받습니다. |

> Screenshot placeholder: `docs/screenshots/slack-app-settings/07-event-subscriptions-channel.png`

주의:

- channel event를 추가한 뒤에는 반드시 app을 reinstall합니다.
- bot을 감시 대상 channel에 초대해야 channel event가 들어옵니다.
- 이 repo에서는 `TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS`에 allowlist를 넣고,
  기본적으로 `TASK_MANAGEMENT_SLACK_WATCH_REQUIRE_MENTION=1`을 유지하는 편이
  안전합니다.

## 6. Slack client에서 app 확인

Slack client에서:

1. 좌측 Apps 또는 검색에서 새 app을 엽니다.
2. **Messages** tab에서 app에 테스트 메시지를 보냅니다.
3. **Home** tab을 열어 빈 dashboard 또는 초기 안내가 보이는지 확인합니다.
4. channel triage를 켰다면 대상 channel에 bot을 초대합니다.

> Screenshot placeholder: `docs/screenshots/slack-app-settings/08-slack-client-app-home.png`

테스트 메시지 예시:

```text
내일 오전 10시까지 데모 준비 체크리스트 정리해줘
```

메시지를 보낸 뒤 runtime 쪽에서 `slack-fast-cycle` 또는 `slack-socket-loop`를
실행합니다.

## 7. 로컬 doctor로 설정 검증

`.env.local.md`에 값을 붙여넣은 뒤 repo 루트에서 실행합니다. CLI가
`.env.local`과 `.env.local.md`를 자동 로드합니다.

DM/Web API 설정 확인:

```bash
python -X utf8 -m task_management.cli --state "$TASK_MANAGEMENT_STATE" slack-doctor --live-open-dm
```

Socket Mode 설정 확인:

```bash
python -X utf8 -m task_management.cli --state "$TASK_MANAGEMENT_STATE" slack-socket-doctor
```

첫 live cycle:

```bash
python -X utf8 -m task_management.cli --agent "$TASK_MANAGEMENT_OPERATING_AGENT" --state "$TASK_MANAGEMENT_STATE" \
  slack-fast-cycle \
  --dashboard-output out/demo-dashboard.html \
  --send
```

continuous loop:

```bash
python -X utf8 -m task_management.cli --agent "$TASK_MANAGEMENT_OPERATING_AGENT" --state "$TASK_MANAGEMENT_STATE" \
  slack-socket-loop \
  --dashboard-output out/demo-dashboard.html \
  --home-dashboard-url "$TASK_MANAGEMENT_DASHBOARD_URL" \
  --send
```

## 8. 최종 체크리스트

설정 완료 전 확인:

- [ ] App-Level Token이 있고 `connections:write` scope를 가진다.
- [ ] `SLACK_APP_TOKEN`이 `xapp-`로 시작한다.
- [ ] Socket Mode가 enabled 상태다.
- [ ] Bot Token Scopes에 `chat:write`, `im:history`, `im:write`, `reactions:write`가 있다.
- [ ] Event Subscriptions에 `message.im`이 있다.
- [ ] Home tab을 쓸 경우 `app_home_opened`가 있다.
- [ ] App Home에서 Messages tab이 켜져 있다.
- [ ] app을 install/reinstall 했다.
- [ ] `SLACK_BOT_TOKEN`이 `xoxb-`로 시작한다.
- [ ] `SLACK_USER_ID`는 사용자 id이며 보통 `U...` 또는 `W...`로 시작한다.
- [ ] 선택한 semantic CLI(`codex` 또는 `claude`)가 로그인되어 있고 PATH에서 실행된다.
- [ ] `slack-doctor --live-open-dm`이 성공한다.
- [ ] `slack-socket-doctor`가 성공한다.
- [ ] demo DB/state는 `.task-management-demo`처럼 local ignored path를 쓴다.
- [ ] token, DB, JSONL, Slack transcript를 Git에 넣지 않았다.

채널 감시를 켰다면 추가 확인:

- [ ] 필요한 선택 scope만 추가했다.
- [ ] 필요한 선택 event만 추가했다.
- [ ] app을 reinstall 했다.
- [ ] bot을 대상 channel에 초대했다.
- [ ] `TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS`에 channel id를 넣었다.
- [ ] 기본값으로 mention-required를 유지한다.

## Troubleshooting

| 증상 | 확인할 것 |
| --- | --- |
| `SLACK_APP_TOKEN is required` | App-Level Token을 만들고 `.env.local`에 `SLACK_APP_TOKEN=xapp-...`를 넣었는지 확인합니다. |
| `SLACK_BOT_TOKEN must be a bot token` | OAuth & Permissions의 **Bot User OAuth Token**을 복사했는지 확인합니다. |
| Socket Mode는 연결되는데 DM이 안 들어옴 | Event Subscriptions에 `message.im`이 있고 app을 reinstall했는지 확인합니다. |
| app에 DM을 보낼 수 없음 | App Home의 Messages tab / direct message setting을 켰는지 확인합니다. |
| Home tab이 갱신되지 않음 | App Home Home tab, `app_home_opened` event, `SLACK_USER_ID`를 확인합니다. |
| channel 메시지가 안 들어옴 | bot 초대, channel id allowlist, 선택 scope/event, reinstall 여부를 확인합니다. |
| scope를 바꿨는데 변화가 없음 | Slack app은 scope/event 변경 후 reinstall이 필요할 수 있습니다. |
| token을 문서에 붙여넣음 | 즉시 Slack app에서 token을 rotate/reinstall하고 문서/history에서 제거합니다. |

## Official references

- Slack App Home: <https://docs.slack.dev/surfaces/app-home>
- Slack Socket Mode: <https://docs.slack.dev/apis/events-api/using-socket-mode/>
- Slack Socket Mode app-level token scope: <https://docs.slack.dev/reference/scopes/connections.write/>
- Slack `message.im` event: <https://docs.slack.dev/reference/events/message.im>
- Slack `app_mention` event: <https://docs.slack.dev/reference/events/app_mention/>
- Slack `message.channels` event: <https://docs.slack.dev/reference/events/message.channels/>
- Slack scopes reference: <https://docs.slack.dev/reference/scopes>

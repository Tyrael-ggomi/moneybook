# 카드결제 단축어 – Render 콜드스타트 대응

## 서버에 추가된 것

| 항목 | 내용 |
| --- | --- |
| `GET/HEAD /health` (`/healthz`) | 인증·DB 없이 `ok`만 반환. 외부 핑 전용. 로그도 남기지 않음 |
| 부팅 순서 | 포트를 먼저 열고 DB 스키마 확인은 백그라운드에서 실행. 부팅 중 DB 오류가 나도 프로세스가 죽지 않고 다음 요청에서 다시 시도 |
| 지연 로딩 | 가장 무거운 `webauthn`(Face ID)은 Face ID를 처음 쓸 때 import |
| 세션 검증 캐시 | 요청마다 하던 세션 비밀키 DB 조회를 메모리 캐시로 대체 |
| `MONEYBOOK_API_TOKEN` | (선택) 단축어용 고정 토큰. `Authorization: Bearer <토큰>` 또는 `X-Moneybook-Token` 헤더. 16자 이상일 때만 켜지며, 백업/복원·Dropbox API에는 쓸 수 없음 |
| `POST /api/transactions/batch` | 밀린 결제 일괄 업로드. 같은 파일을 여러 번 보내도 중복 저장되지 않음 |

> 참고: Render가 잠든 서비스를 깨우는 동안에는 `/health`도 똑같이 ~1분 기다립니다.
> `/health`는 "깨우는 속도"를 바꾸는 게 아니라 **핑으로 잠들지 않게** 하기 위한 가벼운 대상입니다.

## 1. cron-job.org 핑

- URL: `https://<서비스>.onrender.com/health`, 메서드 GET (또는 HEAD)
- 주기: 10분 (15분보다 짧게)
- 시간대: Asia/Seoul, 시간 7–23시만 선택 (1–7시 사이는 핑 없음)
- 예상 사용량: 하루 약 18시간 × 31일 ≈ 560시간 (750시간 한도)
  - 핑이 없는 시간에도 직접 접속하면 15분씩 추가되니, 같은 워크스페이스의 다른 무료 서비스는 정리해 두세요.

## 2. 단축어 순서 (유실 방지)

단축어에는 오류 처리가 없으므로 **서버 호출보다 먼저 파일에 기록**합니다.

1. 알림 내용 파싱 → 사전 만들기: `date`(yyyy-MM-dd, 필수), `time`(HH:mm), `amount`, `card`(카드 이름) 또는 `card_id`, `content`
2. **파일에 추가**: iCloud Drive `/단축어/moneybook-queue.txt`, 사전을 텍스트(JSON 한 줄)로, "새로운 줄에 추가" 켜기
3. URL 내용 가져오기: `POST /api/transactions` (기존과 동일)

서버가 잠들어 3번이 실패해도 2번에 기록이 남아 있습니다.

## 3. 밀린 내역 일괄 업로드 단축어

1. 파일 가져오기: `moneybook-queue.txt`
2. URL 내용 가져오기
   - URL: `https://<서비스>.onrender.com/api/transactions/batch`
   - 메서드: POST, 헤더 `Authorization: Bearer <MONEYBOOK_API_TOKEN>`, `Content-Type: text/plain`
   - 요청 본문: 파일 (1번 결과)
3. 결과 보기 → `inserted`/`duplicates`/`errors` 확인. `errors`가 0이면 파일을 비워도 됩니다
   (비우지 않아도 다음 업로드 때 이미 있는 내역은 `duplicate`로 건너뜀).

### 입력 형식

- JSON Lines(한 줄에 객체 하나), JSON 배열, `{"rows": [...]}` 모두 가능. 빈 줄은 무시. 최대 500건
- 필드: `date`(필수), `time`, `amount`, `card_id` 또는 `card`, `content`, 선택: `category_id`/`category`, `user_percent`, `installment_months`
- 카테고리를 안 주면 자동분류 규칙 → 없으면 `미정`
- 중복 판단: 날짜·시간·금액·카드·내용이 모두 같은 내역. 업로드 안에 같은 줄이 N개면 최대 N건까지 저장
  (같은 가게에서 같은 분에 같은 금액을 두 번 결제한 경우도 보존)
- 형식이 잘못된 줄은 줄 번호와 함께 `error`로 보고되고 나머지는 정상 저장

```json
{"date":"2026-11-10","time":"12:30","amount":"4,500","card":"현대카드","content":"스타벅스 강남"}
```

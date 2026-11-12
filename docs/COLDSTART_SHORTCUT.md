# 카드결제 단축어 – 보관함 방식 (Render 콜드스타트 대응)

## 동작 방식

```
카드결제 알림 → 단축어 → Supabase 보관함(card_inbox)   ← 잠들지 않음, 약 1초
머니북 열기 → 서버가 보관함 검사 → 정상 내역은 지출내역에 자동 추가
```

- 단축어는 더 이상 Render 서버를 부르지 않습니다. Render가 잠들어 있어도 결제 기록이 빠지지 않습니다.
- 머니북을 열거나 다시 화면으로 돌아올 때마다 보관함을 처리합니다.
  - 정상: 지출내역에 추가하고 "카드결제 N건을 자동으로 추가했습니다" 표시
  - 중복 의심(날짜·시간·금액·카드·내용이 같은 내역이 이미 있음): 추가하지 않고 화면 위 "카드결제 보관함"에 표시 → [추가] 또는 [삭제]
  - 오류(카드 이름 없음, 날짜 없음 등): 사유와 함께 표시 → 카드·규칙을 고친 뒤 [재시도] 또는 [삭제]
- 카테고리는 자동분류 규칙 → 없으면 `미정`.

## 설정 순서

### 1. 배포
PR을 main에 합쳐 Render에 배포하고 머니북을 한 번 엽니다. 이때 서버가 `card_inbox` 테이블을 만듭니다.

### 2. Supabase 권한 (SQL Editor에서 한 번 실행)
단축어가 쓰는 공개 키(anon)로는 **보관함에 넣기만** 가능하고, 읽기·수정은 못 하게 합니다.

```sql
alter table public.card_inbox enable row level security;
revoke all on public.card_inbox from anon, authenticated;
grant insert (payload) on public.card_inbox to anon;
grant usage on sequence public.card_inbox_id_seq to anon;
create policy card_inbox_insert on public.card_inbox
  for insert to anon with check (pg_column_size(payload) < 2000);
notify pgrst, 'reload schema';
```

**함께 확인하세요.** 아래 결과에서 `rowsecurity`가 `false`인 테이블은 anon 키만 있으면 누구나 읽을 수 있습니다.
머니북 서버는 DB에 직접 접속하므로 RLS를 켜도 영향을 받지 않습니다.

```sql
select tablename, rowsecurity from pg_tables where schemaname = 'public';
-- false인 테이블마다: alter table public.<테이블> enable row level security;
```

### 3. 단축어 수정
"URL 내용 가져오기" 동작을 다음으로 바꿉니다.

- URL: `https://<프로젝트 ID>.supabase.co/rest/v1/card_inbox`
- 메서드: POST
- 헤더
  - `apikey`: `<anon(publishable) 키>` (Supabase → Project Settings → API Keys)
  - `Authorization`: `Bearer <같은 키>`
  - `Content-Type`: `application/json`
  - `Prefer`: `return=minimal`
- 요청 본문(JSON): `payload` 필드를 **사전**으로 만들고 그 안에 둘 중 하나를 넣습니다.

**방법 A (추천): 알림 원문 그대로** — 사전 안에 텍스트 필드 `raw` 하나만 만들고, 값에 카드 알림 전체 내용을 넣습니다. 날짜·금액·가맹점·카드는 서버가 해석합니다.

```json
{"payload": {"raw": "현대 ZERO 승인\n1,300원 일시불, 11/12 7:34\n씨유CU대구공항국제선점 누적597,194원"}}
```

- 카드 이름은 알림의 "현대 ZERO"와 머니북 카드 이름을 띄어쓰기·"카드" 글자를 빼고 비교합니다(예: `현대카드 ZERO`와 연결). 연결이 안 되면 보관함에 오류로 남고, 등록된 카드 이름을 알려 줍니다.
- 할부(`03개월`)와 취소(음수 금액)도 읽습니다. 연도는 알림을 받은 날 기준입니다.
- **해외승인**(예: `JPY 3,600.00`)은 머니북을 여는 시점의 환율로 원화 금액을 계산해 추가하고, 내용에 외화 금액을 함께 적습니다(예: `KIXLIMOUSINEBUS (JPY 3,600.00)`). 환율은 무료 공개 환율 서비스에서 가져오며 카드사 수수료는 포함하지 않으므로 청구 금액과 조금 다릅니다. 환율을 가져오지 못하면 보관함에 남고 [재시도] 또는 [직접 입력]으로 처리합니다.

**방법 B: 단축어가 나눠서 전송**
  - `date` (yyyy-MM-dd, 필수), `time` (HH:mm), `amount`, `card`(머니북의 카드 이름과 동일) 또는 `card_id`, `content`
  - 선택: `category`/`category_id`, `user_percent`, `installment_months`

```json
{"payload": {"date": "2026-11-10", "time": "12:30", "amount": "4,500", "card": "현대카드", "content": "스타벅스 강남"}}
```

성공하면 응답 본문 없이 끝납니다(HTTP 201).

### 4. 확인
결제 1건(또는 단축어 수동 실행) 후 머니북을 열어 자동 추가 알림과 지출내역을 확인합니다.

## 참고: 서버에 남아 있는 기능
- `GET/HEAD /health`: 인증·DB 없이 `ok` 반환. 보관함 방식에서는 **핑이 필수가 아닙니다.** 머니북을 열 때 1분 기다리는 것이 싫을 때만 cron-job.org로 10분 간격 핑(Asia/Seoul 7–23시, 월 약 560시간)을 걸어 두세요.
- `POST /api/transactions/batch`: JSON Lines 파일 일괄 업로드(중복 건너뜀). `MONEYBOOK_API_TOKEN`(16자 이상)을 Render 환경 변수에 넣으면 `Authorization: Bearer <토큰>`으로 호출할 수 있습니다.
- 보관함 API(로그인 필요): `POST /api/inbox/process`, `GET /api/inbox`, `POST /api/inbox/<id>/retry`, `DELETE /api/inbox/<id>`
- 가져온 보관 내역은 30일 뒤 보관함에서 자동 삭제됩니다(지출내역은 그대로).

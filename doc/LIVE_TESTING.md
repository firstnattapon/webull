# ทดสอบ Webull แบบปลอดภัย (UAT)

คู่มือนี้ใช้กับ Webull Thailand และโค้ดปัจจุบันใน repo นี้

## Live API harness แบบ Manual Test Lab

ใช้ `scripts/webull_live_test.py` เพื่อตรวจ API จริงตามลำดับเดียวกับ Manual Test Lab:

1. authenticated account list และยืนยันว่า Account ID ที่ตั้งไว้ปรากฏแบบตรงกันทุกตัวอักษร
2. balance, positions และ quote
3. open orders, history และ detail
4. MARKET order preview
5. เฉพาะโหมดที่ arm แล้วเท่านั้น: MARKET place/detail/position reconciliation หรือ LIMIT place/cancel roundtrip

ค่าเริ่มต้น `read-preview` เรียกเครือข่าย UAT จริง แต่ **ไม่เรียก place หรือ cancel** ผลลัพธ์ที่พิมพ์ออกมามีเฉพาะชื่อขั้นตอน, PASS/FAIL, เวลา และ metadata เช่นจำนวน record/ผลการ correlation ไม่มี raw response, balance, position quantity, credential หรือ Account ID

วิธีแนะนำบน Windows คือใช้ wrapper ซึ่งรับ credential แบบ secure prompt, ส่งผ่าน environment ให้ child Python ชั่วคราว และล้าง environment หลังจบ:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_webull_live_test.ps1
```

ค่า default สำหรับ preview คือ `AAPL`, `BUY`, quantity `1`, session `CORE`; wrapper ให้แก้ค่าก่อนรันได้ ถ้ามี `client_order_id` ที่ต้องการทดสอบ detail โดยเฉพาะให้กรอกใน prompt มิฉะนั้น harness จะเลือก ID จาก open orders/history และจะ fail อย่างตรงไปตรงมาหากบัญชียังไม่มี order สำหรับทดสอบ Credential จะไม่ถูกส่งผ่าน command-line และไม่ถูกเขียนเป็น `.env`

### โหมดที่เปลี่ยนข้อมูล UAT

MARKET submission และ LIMIT cancel เป็นคนละการทดสอบ เพราะ MARKET อาจ fill ก่อน cancel ได้:

```powershell
# ส่ง MARKET หนึ่งครั้ง ต้องยืนยันสถานะ FILLED + filled_quantity ครบ แล้วตรวจ history และ position delta
powershell -ExecutionPolicy Bypass -File .\scripts\run_webull_live_test.ps1 -Mode market-place

# วาง LIMIT ที่ห่างจากราคาตลาด แล้ว cancel เฉพาะ client_order_id ที่ run นี้สร้างเอง
powershell -ExecutionPolicy Bypass -File .\scripts\run_webull_live_test.ps1 -Mode limit-cancel
```

ทั้งสองโหมดบังคับให้ระบุ `symbol`, `side`, `quantity`, `CORE`, `max_notional` และยืนยัน Account ID ซ้ำ โหมด LIMIT ต้องระบุ `limit_price` เพิ่มเติม โดย BUY ต้องไม่เกิน 50% ของ quote และ SELL ต้องไม่น้อยกว่า 150% ของ quote เพื่อช่วยลดโอกาส fill ก่อน cancel

สำหรับ `market-place`, `max_notional` เป็น **advisory guard** เท่านั้น: harness ตรวจทั้ง `quote × quantity` และ estimated notional จาก preview ก่อน place แต่ MARKET ไม่มี execution-price cap จึงไม่รับประกันว่า fill จริงจะไม่เกินค่านี้ ถ้าต้องการเพดานราคาซื้อแบบบังคับต้องใช้ BUY LIMIT order ส่วน `limit-cancel` ตรวจ reference notional จาก `limit_price × quantity` และมี hard limit price อยู่ใน payload (BUY เป็นราคาสูงสุด, SELL เป็นราคาต่ำสุด)

ต้องพิมพ์ arming phrase นี้ตรงทุกตัวอักษร:

```text
I_UNDERSTAND_THIS_MUTATES_WEBULL_UAT
```

จากนั้น wrapper จะแสดง order binding ชุดที่สองในรูปแบบต่อไปนี้ โดยใช้ SHA-256 fingerprint แทนการแสดง Account ID เต็ม:

```text
uat|acct-sha256=<12 ตัว>|mode=market-place|symbol=AAPL|side=BUY|quantity=1|session=CORE|max-notional=500|limit-price=none
```

ต้องพิมพ์ค่าที่แสดงตรงทุกตัวอักษรและยืนยัน Account ID ซ้ำอีกครั้ง จึงจะ place ได้ การยืนยันนี้ผูกบัญชี, mode, symbol, side, quantity, session, `max_notional` และ `limit_price` ที่ normalize แล้วเข้ากับคำสั่งเดียวกัน ถ้าแก้ parameter ที่มีผลต่อ mutation หลังยืนยัน harness จะหยุดก่อนเรียก API สำหรับ `limit-cancel` ส่วนท้ายจะแสดงราคา LIMIT จริงแทน `none`

ในโหมด `market-place` harness จะ preview **payload object และ client_order_id เดียวกับที่จะ place** ทันทีก่อน submission; preview แบบ official ต้องคืน top-level string `estimated_cost` และ `estimated_transaction_fee` ครบ ส่วน place/cancel acknowledgement ต้องเป็น flat object ที่มี `client_order_id` ตรงกันและ `order_id` เป็น string หลังจากนั้นผล open/history/detail เท่านั้นที่อ่านจาก `orders[]` แบบ exact unique match โดย detail ต้องมีสถานะ full-fill, `filled_quantity` ไม่น้อยกว่า quantity ที่ขอ และ position delta ต้องตรงกับ signed filled quantity ภายใน epsilon คงที่ `0.000001` จึงจะ PASS Preview แรกใน read-only phase ใช้พิสูจน์ connection เท่านั้นและไม่ถือเป็นการอนุมัติ order ที่จะส่งจริง

Harness นี้ปฏิเสธ production, endpoint นอก allowlist, region/session นอก allowlist, API ที่ไม่ใช่ `v3`, order parameter ไม่ครบ และ mutation ที่ไม่ได้ arm นอกจากนี้จะไม่ cancel open order เดิมในบัญชี ถ้า mutation ล้มเหลว cleanup จะอ่าน exact order detail ของ ID ที่สร้างใน run เดียวกัน, cancel residual ที่ยังไม่ terminal ได้มากที่สุดหนึ่งครั้ง และต้องยืนยัน terminal state จาก detail หรือ exact paginated history ก่อนรายงาน PASS การตรวจว่า order ไม่อยู่ใน open/history จะ paginate ทุกหน้าด้วย `page_size=100` (สูงสุด 50 หน้า) ไม่ใช้หน้าแรกเป็นหลักฐาน โหมด LIMIT-cancel รองรับ terminal spelling ทั้ง `CANCELLED` ตามเอกสารและ `CANCELED` ที่ UAT คืนจริง โดยไม่เรียก cancel ซ้ำ หาก MARKET หรือ LIMIT เกิด partial fill ผลทดสอบจะ FAIL, cleanup จะจัดการ residual และตรวจ position delta ของส่วนที่ fill แล้ว

> Credential ที่หน้า SDK เผยแพร่เป็น shared **Test account** สำหรับ UAT เท่านั้น ข้อมูล balance, positions และ orders อาจเปลี่ยนจากผู้ทดสอบรายอื่นได้ทุกเวลา หากต้องการพิสูจน์ position reconciliation แบบ deterministic ให้ใช้ dedicated UAT account ส่วน production fill/เงินจริงต้องใช้ production credential และการอนุมัติแยกต่างหาก

รัน offline safety tests โดยไม่แตะ Webull:

```powershell
python -m pytest tests/test_live_harness.py -q
```

## คู่มือ Cloud Run bot เดิม (ไม่ใช่ live API harness)

เนื้อหาตั้งแต่ส่วนนี้ลงไปใช้กับ service/bot หลักและ Manual Test Lab บน Cloud Run ไม่ใช่ `scripts/webull_live_test.py` ตัว harness ด้านบนเป็น UAT-only และจะปฏิเสธ `WEBULL_ENV=prod` เสมอ

### ค่าที่ต้องใช้

```text
WEBULL_ENV=uat
WEBULL_REGION=th
WEBULL_API_VERSION=v3
WEBULL_ACCOUNT_ID=ใส่ Account ID
WEBULL_APP_KEY=ใส่ App Key
WEBULL_APP_SECRET=ใส่ App Secret
```

Endpoint ที่ระบบเลือกให้:

| Environment | Endpoint | Order API |
|---|---|---|
| UAT | `th-api.uat.webullbroker.com` | `order_v3` |
| Production | `api.webull.co.th` | `order_v3` |

> Account/position API ใน SDK ยังใช้ `account_v2` ได้ตามปกติ ส่วนคำสั่งซื้อของ Thailand ใช้ `order_v3`

### 1. ตรวจ configuration โดยไม่เทรด

เปิด URL ของ Cloud Run แล้วเติม `/health` เช่น:

```text
https://YOUR_SERVICE_URL/health
```

ผลที่ถูกต้องต้องมี:

```json
{
  "status": "HEALTHY"
}
```

`/health` ตรวจเฉพาะ configuration และสถานะโปรแกรม ไม่เรียก Webull และไม่ส่งคำสั่งซื้อ

### 2. ตรวจการเชื่อมต่อ Webull

ใช้หน้า Manual Test Lab เลือก:

```text
Environment: Test (UAT)
Region: Thailand
API version: v3
```

เริ่มจากคำสั่งอ่านข้อมูลหรือ Preview ก่อน และตรวจว่า endpoint เป็น:

```text
th-api.uat.webullbroker.com
```

ถ้าได้รับ `order_id` แปลว่า Webull UAT รับคำสั่งแล้วเท่านั้น ยังไม่ใช่หลักฐานว่า order full-fill

### คำเตือนสำคัญของ bot

- สำหรับ bot, `WEBULL_PREVIEW_ORDERS=true` หมายถึง Preview ก่อน แล้วโค้ดยังเรียก `place_order` ต่อ ไม่ใช่โหมดดูอย่างเดียว; ค่านี้ไม่เกี่ยวกับโหมด `read-preview` ของ live harness
- การเรียก Cloud Run URL หลักด้วย `POST` อาจทำให้บอตส่งคำสั่งซื้อเมื่อเงื่อนไขครบ
- ทดสอบสถานะด้วย `/health` เท่านั้น
- เริ่มจาก UAT เสมอ และหยุด Cloud Scheduler ไว้จนกว่าจะตรวจครบ
- ห้ามเก็บหรือแสดง App Secret ในเอกสาร, Git commit, screenshot หรือ log

### Error ที่พบบ่อย

| Error | สาเหตุที่ควรตรวจ |
|---|---|
| HTTP `404` จาก order API | ตรวจว่า Thailand ใช้ `WEBULL_API_VERSION=v3` |
| `401` / `403` | App Key, App Secret, Account ID หรือสิทธิ์ไม่ตรง environment |
| `UNHEALTHY` | อ่านรายการ `checks` ในผล `/health` |
| `PASS_MARKET_CLOSED` | ตลาดสหรัฐปิด เป็นสถานะปกติ |
| `PASS_OPEN_ORDER` | Webull ยังมี order ของ symbol นี้ค้างอยู่ ระบบจึงไม่ส่งซ้ำ |
| quantity เป็น `0` ทั้งที่มี position | เปิด Manual Test Lab ตรวจ raw Account Positions; ระบบจะหยุดด้วย 502 หากพบ symbol แต่ไม่มี field quantity แทนการเดาเป็น 0 |
| `Regional Access Boundary ... Gaia id` | ปัญหาตัวตนของ Google Cloud Shell ไม่ใช่ Webull |

### ก่อนเปิด Production ของ bot เท่านั้น

ขั้นตอนนี้ไม่ใช้กับ live API harness ซึ่งเป็น UAT-only:

- เปลี่ยนเป็น `WEBULL_ENV=prod`
- คง `WEBULL_REGION=th` และ `WEBULL_API_VERSION=v3`
- ใช้ Production App Key, App Secret และ Account ID เท่านั้น
- Endpoint ต้องเป็น `api.webull.co.th`
- ตรวจจำนวนเงิน, symbol, DNA, `FIX_C`, `P0`, `DIFF` และ Scheduler อีกครั้ง

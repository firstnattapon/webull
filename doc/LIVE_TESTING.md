# ทดสอบ Webull แบบปลอดภัย (UAT)

คู่มือนี้ใช้กับ Webull Thailand และโค้ดปัจจุบันใน repo นี้

## ค่าที่ต้องใช้

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

## 1. ตรวจ configuration โดยไม่เทรด

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

## 2. ตรวจการเชื่อมต่อ Webull

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

ถ้าได้รับ `order_id` แปลว่า Webull UAT รับคำสั่งแล้ว

## คำเตือนสำคัญ

- `WEBULL_PREVIEW_ORDERS=true` หมายถึง Preview ก่อน แล้วโค้ดยังเรียก `place_order` ต่อ ไม่ใช่โหมดดูอย่างเดียว
- การเรียก Cloud Run URL หลักด้วย `POST` อาจทำให้บอตส่งคำสั่งซื้อเมื่อเงื่อนไขครบ
- ทดสอบสถานะด้วย `/health` เท่านั้น
- เริ่มจาก UAT เสมอ และหยุด Cloud Scheduler ไว้จนกว่าจะตรวจครบ
- ห้ามเก็บหรือแสดง App Secret ในเอกสาร, Git commit, screenshot หรือ log

## Error ที่พบบ่อย

| Error | สาเหตุที่ควรตรวจ |
|---|---|
| HTTP `404` จาก order API | ตรวจว่า Thailand ใช้ `WEBULL_API_VERSION=v3` |
| `401` / `403` | App Key, App Secret, Account ID หรือสิทธิ์ไม่ตรง environment |
| `UNHEALTHY` | อ่านรายการ `checks` ในผล `/health` |
| `PASS_MARKET_CLOSED` | ตลาดสหรัฐปิด เป็นสถานะปกติ |
| `PASS_OPEN_ORDER` | Webull ยังมี order ของ symbol นี้ค้างอยู่ ระบบจึงไม่ส่งซ้ำ |
| quantity เป็น `0` ทั้งที่มี position | เปิด Manual Test Lab ตรวจ raw Account Positions; ระบบจะหยุดด้วย 502 หากพบ symbol แต่ไม่มี field quantity แทนการเดาเป็น 0 |
| `Regional Access Boundary ... Gaia id` | ปัญหาตัวตนของ Google Cloud Shell ไม่ใช่ Webull |

## ก่อนเปิด Production

- เปลี่ยนเป็น `WEBULL_ENV=prod`
- คง `WEBULL_REGION=th` และ `WEBULL_API_VERSION=v3`
- ใช้ Production App Key, App Secret และ Account ID เท่านั้น
- Endpoint ต้องเป็น `api.webull.co.th`
- ตรวจจำนวนเงิน, symbol, DNA, `FIX_C`, `P0`, `DIFF` และ Scheduler อีกครั้ง

# Quick Start แบบไม่ใช้ Cloud Shell

คู่มือนี้ตั้งค่าผ่านหน้า Google Cloud Console ทั้งหมด เหมาะสำหรับผู้เริ่มต้น

## 1. เตรียมของ

- GitHub repo: `https://github.com/firstnattapon/webull`
- Google Cloud project ที่เปิด Billing แล้ว
- Webull UAT: Account ID, App Key และ App Secret
- Region ของ Google Cloud: `asia-southeast1`

## 2. สร้าง Firestore

ไปที่ **Firestore > Create database** แล้วเลือก:

```text
Database ID: (default)
Mode: Native
Edition: Standard
Location: asia-southeast1
```

ไม่ต้องสร้าง collection เอง โค้ดจะสร้างเมื่อเริ่มทำงาน

## 3. เชื่อม GitHub และ Deploy

ไปที่ **Cloud Run > Connect repository** แล้วตั้งค่า:

```text
Repository: firstnattapon/webull
Branch: ^main$
Build type: Buildpacks
Build context: /
Function target: rebalance_trigger
Service name: shannon-demon-bot
Region: asia-southeast1
Authentication: Require authentication
```

เมื่อ push เข้า `main` Cloud Build จะสร้าง revision ใหม่อัตโนมัติ

## 4. ตั้งค่าความปลอดภัยของ Service

ไปที่ **Cloud Run > shannon-demon-bot > Edit and deploy new revision** แล้วตั้ง:

```text
Container concurrency: 1
Maximum instances: 1
```

ค่านี้ช่วยป้องกันการประมวลผลพร้อมกันหลายรอบสำหรับบอตตัวเดียว

## 5. ใส่ Environment variables

เปิดแท็บ **Variables & Secrets** แล้วเพิ่ม:

```text
GCP_PROJECT_ID=YOUR_PROJECT_ID
STRATEGY_ID=SHANNON_DEMON_DNA
SYMBOL=SMR
FIX_C=1500
P0=9
DIFF=30
DNA_CODE=bypass:100
START_TIMESTAMP=0
SCHEDULE_SLOT_SECONDS=300
FIRESTORE_STATE_COLLECTION=shannon_demon_state
FIRESTORE_TRADE_COLLECTION=shannon_demon_trades
FIRESTORE_STATE_DOCUMENT=SHANNON_DEMON_DNA_SMR
WEBULL_ENV=uat
WEBULL_API_VERSION=v3
WEBULL_REGION=th
WEBULL_SUPPORT_TRADING_SESSION=CORE
WEBULL_PREVIEW_ORDERS=true
WEBULL_ACCOUNT_ID=YOUR_UAT_ACCOUNT_ID
WEBULL_APP_KEY=YOUR_UAT_APP_KEY
WEBULL_APP_SECRET=YOUR_UAT_APP_SECRET
```

กด **Deploy** แล้วรอ revision ใหม่เป็นสีเขียว

> `WEBULL_PREVIEW_ORDERS=true` ไม่ใช่โหมด Preview-only โค้ดจะ Preview แล้วเรียก `place_order` ต่อเมื่อเงื่อนไขเทรดครบ

## 6. ให้สิทธิ์ Firestore

ดู Service Account ที่ **Cloud Run > Security** แล้วไปที่ **IAM** เพิ่ม role:

```text
Cloud Datastore User (roles/datastore.user)
```

## 7. ทดสอบแบบไม่ส่งคำสั่งซื้อ

เปิด Service URL แล้วเติม `/health`:

```text
https://YOUR_SERVICE_URL/health
```

ผลที่ต้องได้:

```text
status: HEALTHY
webull_endpoint: ok (th-api.uat.webullbroker.com)
webull_api_version: ok
```

`HEALTHY` ยืนยันว่า configuration ครบ แต่ยังไม่ได้ยืนยันการ login กับ Webull ให้ทดสอบ Webull ต่อผ่าน Manual Test Lab ใน UAT

## 8. ตั้ง Cloud Scheduler เมื่อพร้อมเท่านั้น

1. สร้าง Service Account ชื่อ `scheduler-invoker`
2. ให้ role `Cloud Run Invoker` กับ service นี้
3. สร้าง Cloud Scheduler แบบ HTTP
4. Method: `POST`
5. URL: Cloud Run Service URL โดยไม่เติม `/health`
6. Frequency: `*/5 * * * *`
7. Timezone: `Asia/Bangkok`
8. Authentication: OIDC
9. Audience: Cloud Run Service URL

> การกด Force run หรือเปิด Scheduler คือการเรียกบอตจริง และอาจส่ง UAT order เมื่อเงื่อนไขครบ

> ตั้ง `SCHEDULE_SLOT_SECONDS` ให้เท่ากับรอบของ Scheduler เป็นวินาที (เช่น `*/5 * * * *` = `300`, `*/10` = `600`)
> เพื่อไม่ให้การกด Force run หรือการยิงซ้ำใน slot เดียวกันกิน DNA step เกินรอบ — การเรียกซ้ำจะตอบ
> `PASS_DUPLICATE_TICK` แทนการเทรด ถ้าตั้งเป็น `0` (ค่าเริ่มต้น) จะทำงานแบบเดิมคือทุก invocation กิน 1 step

## ใช้งานประจำ

- ดู log: **Cloud Run > shannon-demon-bot > Logs**
- หยุดบอต: **Cloud Scheduler > Pause**
- เปิดบอต: **Cloud Scheduler > Resume**
- แก้ strategy: **Cloud Run > Edit and deploy new revision > Variables & Secrets**
- แก้ code: push เข้า GitHub branch `main`

## เปลี่ยนเป็น Production

เปลี่ยนเฉพาะเมื่อทดสอบ UAT ครบแล้ว:

```text
WEBULL_ENV=prod
WEBULL_REGION=th
WEBULL_API_VERSION=v3
WEBULL_ACCOUNT_ID=YOUR_PRODUCTION_ACCOUNT_ID
WEBULL_APP_KEY=YOUR_PRODUCTION_APP_KEY
WEBULL_APP_SECRET=YOUR_PRODUCTION_APP_SECRET
```

Production endpoint ต้องแสดงเป็น `api.webull.co.th` ห้ามใช้ UAT credentials กับ Production

## Checklist

- [ ] Firestore `(default)` พร้อมใช้งาน
- [ ] Cloud Run revision เป็นสีเขียว
- [ ] Concurrency = 1 และ Max instances = 1
- [ ] Environment = `uat`, Region = `th`, API = `v3`
- [ ] Account ID ถูกเก็บเป็นข้อความครบทุกหลัก
- [ ] Service Account มี `roles/datastore.user`
- [ ] `/health` ได้ `HEALTHY`
- [ ] Manual Test Lab เชื่อม Webull UAT สำเร็จ
- [ ] Scheduler ยัง Pause จนกว่าจะพร้อม


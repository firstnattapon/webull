# Quick Start: Deploy บน Google Cloud แบบมือใหม่

คู่มือนี้เป็นทางเดินแบบ **ไม่ใช้ Cloud Shell** และ **ไม่ใช้ GitHub Actions**:

1. สร้าง Google Cloud project ในหน้าเว็บ
2. เปิด Firestore
3. Deploy จาก GitHub repo โดยใช้เมนู **Continuously deploy from a repository**
4. ใส่ env-vars เองทุกตัวใน Google Cloud Console
5. ตั้ง Cloud Scheduler ให้ยิง bot ตามเวลา

Repo ที่ใช้ deploy: `https://github.com/firstnattapon/webull`

> หมายเหตุสำคัญ: โค้ดนี้เป็น Cloud Run function ที่ entry point คือ `rebalance_trigger` ในไฟล์ `main.py`

## ภาพรวม

```text
GitHub repo
  -> Cloud Run: Connect repository
  -> Cloud Build สร้างและ deploy ให้อัตโนมัติเมื่อ push branch ที่เลือก
  -> Cloud Run function: rebalance_trigger
  -> Firestore เก็บ dna_step และ trade log
  -> Cloud Scheduler ยิง HTTP POST ตามเวลา
```

อ้างอิงเอกสาร Google:

- Cloud Run continuous deployment from repository: https://docs.cloud.google.com/run/docs/continuous-deployment
- Cloud Run function deploy: https://docs.cloud.google.com/run/docs/deploy-functions
- Cloud Run environment variables: https://docs.cloud.google.com/run/docs/configuring/services/environment-variables
- Firestore database: https://docs.cloud.google.com/firestore/native/docs/manage-databases
- Cloud Scheduler เรียก Cloud Run แบบ OIDC: https://docs.cloud.google.com/run/docs/triggering/using-scheduler

## 1. สร้าง Google Cloud project

ทำในเว็บ Google Cloud Console:

1. เข้า https://console.cloud.google.com/
2. กดตัวเลือก project ด้านบน
3. กด **New Project**
4. ตั้งชื่อ เช่น `webull-bot-smr`
5. กด **Create**
6. เลือก project ที่สร้างใหม่
7. เปิด Billing ให้ project นี้

จดค่า **Project ID** ไว้ เช่น:

```text
webull-bot-smr-123456
```

ในคู่มือนี้ให้แทน `YOUR_PROJECT_ID` ด้วย Project ID ของคุณ

## 2. เปิด API ที่ต้องใช้

ไปที่ **APIs & Services -> Library** แล้วค้นหาและกด Enable ทีละตัว:

- Cloud Run Admin API
- Cloud Build API
- Artifact Registry API
- Cloud Logging API
- Cloud Scheduler API
- Firestore API
- IAM Service Account Credentials API

ถ้า Google Cloud ถามให้เปิด API เพิ่มระหว่าง deploy ให้กด Enable ได้

## 3. สร้าง Firestore

ไปที่ **Firestore -> Databases**

1. กด **Create a Firestore database**
2. Database ID: ใช้ `(default)`
3. Edition: เลือก **Standard**
4. Mode / data access: เลือก **Firestore Native**
5. Rules: เลือก **Restrictive**
6. Location: แนะนำ `asia-southeast1` หรือ location ใกล้คุณ
7. กด **Create Database**

ไม่ต้องสร้าง collection เองก่อนก็ได้ โค้ดจะสร้าง document เมื่อ bot ถูกเรียกครั้งแรก

ค่า default ที่โค้ดใช้:

```text
FIRESTORE_STATE_COLLECTION=shannon_demon_state
FIRESTORE_TRADE_COLLECTION=shannon_demon_trades
FIRESTORE_STATE_DOCUMENT=SHANNON_DEMON_DNA_SMR
```

ถ้าต้องการ reset DNA step กลับไปเริ่มใหม่ ให้ลบ document ใน:

```text
shannon_demon_state / SHANNON_DEMON_DNA_SMR
```

## 4. เตรียม Webull credentials

ต้องมี 3 ค่า:

- Webull App Key
- Webull App Secret
- Webull Account ID

สำหรับมือใหม่ แนะนำเริ่มที่:

```text
WEBULL_ENV=uat
WEBULL_PREVIEW_ORDERS=true
```

`uat` คือทดสอบ ส่วน `prod` คือเงินจริง

## 5. Deploy จาก GitHub repo แบบ auto update

ไปที่ **Cloud Run -> Services**

1. กด **Connect repository**
2. เลือก **Cloud Build**
3. เลือก GitHub
4. กด Authenticate ถ้ายังไม่เคยเชื่อม
5. เลือก repo:

```text
firstnattapon/webull
```

6. Branch: เลือก branch ที่ต้องการ auto deploy เช่น:

```text
^main$
```

7. Build type: เลือก **Buildpacks**
8. Build context directory:

```text
webull-main
```

ถ้าใน GitHub repo ของคุณไฟล์ `main.py` และ `requirements.txt` อยู่ที่ root ไม่ได้อยู่ในโฟลเดอร์ `webull-main` ให้ใส่:

```text
/
```

9. Function target:

```text
rebalance_trigger
```

10. กด **Save**

Google Cloud จะพากลับมาหน้า Create service ให้ตั้งค่าต่อ

## 6. ตั้งค่า Cloud Run service

ในหน้า Create service:

- Service name:

```text
shannon-demon-bot
```

- Region:

```text
asia-southeast1
```

- Runtime:

```text
Python 3.12
```

ถ้า Python 3.12 ใช้ไม่ได้ในหน้า Console ของคุณ ให้เลือก Python 3.11

- Authentication:

```text
Require authentication
```

อย่าเลือก public เพราะ Scheduler จะยิงด้วย OIDC token แทน

## 7. ใส่ env-vars เองทุกตัว

ในหน้า Create service หรือ Edit and deploy new revision:

1. เปิดส่วน **Container(s), Volumes, Networking, Security**
2. ไปที่แท็บ **Variables & Secrets**
3. กด **Add variable**
4. ใส่ทุกตัวด้านล่าง

ชุดตัวอย่างตามที่ต้องการ:

```text
GCP_PROJECT_ID=YOUR_PROJECT_ID
STRATEGY_ID=SHANNON_DEMON_DNA
SYMBOL=SMR
FIX_C=1500
P0=9.00
DIFF=30
DNA_CODE=bypass:100
START_TIMESTAMP=0
FIRESTORE_STATE_COLLECTION=shannon_demon_state
FIRESTORE_TRADE_COLLECTION=shannon_demon_trades
FIRESTORE_STATE_DOCUMENT=SHANNON_DEMON_DNA_SMR
WEBULL_ENV=uat
WEBULL_API_VERSION=v2
WEBULL_REGION=th
WEBULL_SUPPORT_TRADING_SESSION=CORE
WEBULL_PREVIEW_ORDERS=true
```

จากนั้นเลือกใช้ credentials แบบใดแบบหนึ่ง

### แบบแนะนำ: ชื่อตรง ไม่สับสน

```text
WEBULL_APP_KEY=ใส่ app key จริง
WEBULL_APP_SECRET=ใส่ app secret จริง
WEBULL_ACCOUNT_ID=ใส่ account id จริง
```

### แบบตามชื่อที่ผู้ใช้ยกตัวอย่าง

โค้ดปัจจุบันอ่านตัวแปร `*_SECRET_ID` เป็น **ค่าจริง** ไม่ใช่ Secret Manager ID ดังนั้นถ้าใช้ชื่อนี้ ให้ใส่ค่า credential จริงลงไป:

```text
WEBULL_APP_KEY_SECRET_ID=ใส่ app key จริง
WEBULL_APP_SECRET_ID=ใส่ app secret จริง
WEBULL_ACCOUNT_ID_SECRET_ID=ใส่ account id จริง
```

อย่าใส่ทั้งสองชุดพร้อมกันถ้าไม่จำเป็น ถ้าใส่ทั้งคู่ โค้ดจะใช้ `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_ACCOUNT_ID` ก่อน

### ตัวแปร optional ที่โค้ดรองรับ

ใส่เมื่อมีเหตุผลเฉพาะ:

```text
DNA_STRING=ใช้แทน DNA_CODE ได้
WEBULL_TRADING_ENDPOINT=ใส่ endpoint เอง ถ้าไม่ต้องการใช้ default ของ uat/prod
WEBULL_TOKEN_DIR=โฟลเดอร์เก็บ token ถ้ารันแบบที่ต้องการ token file
```

ค่า default ในโค้ด:

| ตัวแปร | Default | ความหมาย |
|---|---:|---|
| `STRATEGY_ID` | `SHANNON_DEMON_DNA` | ชื่อ strategy |
| `SYMBOL` | `AAPL` | หุ้นที่จะเทรด |
| `FIX_C` | `1500.0` | มูลค่าเป้าหมายที่อยากถือ |
| `P0` | `6.88` | ราคาอ้างอิงเริ่มต้น |
| `DIFF` | `60.0` | ระยะเผื่อก่อนส่ง order |
| `START_TIMESTAMP` | `0` | เวลาเริ่มแบบ Unix timestamp |
| `WEBULL_ENV` | `uat` | `uat` หรือ `prod` |
| `WEBULL_API_VERSION` | `v2` | `v2` หรือ `v3` |
| `WEBULL_REGION` | `th` เมื่อ `uat/prod` | region สำหรับ Webull signature |
| `WEBULL_SUPPORT_TRADING_SESSION` | `CORE` | trading session |
| `WEBULL_PREVIEW_ORDERS` | `false` | `true` = preview ก่อนส่ง order |

ตัวที่จำเป็นจริง:

- `GCP_PROJECT_ID` หรือ `GOOGLE_CLOUD_PROJECT`
- `WEBULL_APP_KEY` หรือ `WEBULL_APP_KEY_SECRET_ID`
- `WEBULL_APP_SECRET` หรือ `WEBULL_APP_SECRET_ID`
- `WEBULL_ACCOUNT_ID` หรือ `WEBULL_ACCOUNT_ID_SECRET_ID`

## 8. กด Create แล้วรอ deploy

หลังจากกด **Create**:

1. รอ Cloud Build ทำงาน
2. ถ้า build ผ่าน จะได้ Cloud Run service URL
3. หน้า Cloud Run จะแสดง revision ล่าสุด

ต่อไปเมื่อ push code เข้า branch ที่เลือก เช่น `main` ระบบจะ auto deploy ให้เองผ่าน Cloud Build ไม่ต้องใช้ GitHub Actions

## 9. ให้ Cloud Run อ่าน/เขียน Firestore ได้

ไปที่ **IAM & Admin -> IAM**

หา service account ที่ Cloud Run ใช้ โดยมักเป็น Compute default service account:

```text
PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

หรือดูได้ใน Cloud Run service:

```text
Cloud Run -> shannon-demon-bot -> Security -> Service account
```

เพิ่ม role ให้ service account นี้:

```text
Cloud Datastore User
```

หรือ role id:

```text
roles/datastore.user
```

## 10. ทดสอบ health check

ไปที่ **Cloud Run -> shannon-demon-bot**

เปิด URL ของ service แล้วเติม:

```text
/health
```

ถ้า service เป็น private ตามที่แนะนำ Browser ธรรมดาอาจเข้าไม่ได้ ให้ใช้แท็บ **Testing** หรือ **Logs** ใน Cloud Run เพื่อตรวจแทน

Health ที่ดีควรเห็นแนวคิดประมาณนี้:

```text
status = HEALTHY
checks.app_config = ok
checks.webull_app_key = ok
checks.webull_app_secret = ok
checks.webull_account_id = ok
checks.webull_endpoint = ok
checks.webull_api_version = ok
checks.dna = ok
```

ถ้า `UNHEALTHY` ให้อ่าน `checks` ว่าขาด env var ตัวไหน

## 11. ตั้ง Cloud Scheduler

ก่อนสร้าง job ให้สร้าง service account สำหรับ Scheduler:

ไปที่ **IAM & Admin -> Service Accounts**

1. กด **Create service account**
2. Name:

```text
scheduler-invoker
```

3. กด Create
4. กลับไปที่ Cloud Run service `shannon-demon-bot`
5. ไปที่ **Permissions**
6. กด **Grant access**
7. New principals:

```text
scheduler-invoker@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

8. Role:

```text
Cloud Run Invoker
```

จากนั้นไปที่ **Cloud Scheduler**

1. กด **Create job**
2. Name:

```text
shannon-demon-every-5m
```

3. Region:

```text
asia-southeast1
```

4. Frequency:

```text
*/5 * * * *
```

5. Timezone:

```text
Asia/Bangkok
```

6. Target type: `HTTP`
7. URL: ใส่ Cloud Run service URL เช่น:

```text
https://shannon-demon-bot-xxxxx-as.a.run.app
```

8. HTTP method:

```text
POST
```

9. กด **More**
10. Auth header: เลือก **Add OIDC token**
11. Service account:

```text
scheduler-invoker@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

12. Audience:

```text
https://shannon-demon-bot-xxxxx-as.a.run.app
```

ใช้ URL เดียวกับข้อ 7 แต่ไม่ต้องเติม path เพิ่ม

13. กด **Create**

## 12. ตรวจว่าทำงานจริง

ดูผลได้ 3 จุด:

### Cloud Scheduler

ไปที่ job `shannon-demon-every-5m`

- กด **Force run** เพื่อยิงทันที
- ดู `Last run` และ `Result`

### Cloud Run Logs

ไปที่:

```text
Cloud Run -> shannon-demon-bot -> Logs
```

สถานะที่เจอบ่อย:

| Status | ความหมาย |
|---|---|
| `PASS_WAITING_TO_START` | ยังไม่ถึง `START_TIMESTAMP` |
| `PASS_MARKET_CLOSED` | ตลาดสหรัฐปิด |
| `PASS_DNA_ZERO` | DNA รอบนี้เป็น 0 เลยข้าม |
| `PASS_THRESHOLD` | ยังอยู่ในช่วง `DIFF` ไม่ต้องซื้อขาย |
| `OK` | ส่ง order แล้ว |
| `BROKER_ERROR` | Webull ตอบ error |
| `ERROR` | config หรือระบบอื่นมีปัญหา |

### Firestore

ไปที่ **Firestore -> Data**

ควรเห็น collection:

```text
shannon_demon_state
shannon_demon_trades
```

`shannon_demon_state` จะมีค่า `dna_step` เพิ่มขึ้นเมื่อ Scheduler ยิงและผ่าน gate ที่ต้องใช้ Firestore

## 13. แก้ค่า env-vars หลัง deploy

ไปที่:

```text
Cloud Run -> shannon-demon-bot -> Edit & deploy new revision
```

แก้ในแท็บ **Variables & Secrets** แล้วกด **Deploy**

ตัวอย่างแก้ strategy:

```text
SYMBOL=SMR
FIX_C=1500
P0=9.00
DIFF=30
DNA_CODE=bypass:100
WEBULL_ENV=uat
WEBULL_PREVIEW_ORDERS=true
```

การแก้ env-vars แบบนี้จะสร้าง revision ใหม่ทันที ไม่ต้อง push code

ถ้าแก้ code ใน GitHub แล้ว push เข้า branch ที่เลือก Cloud Build จะ deploy revision ใหม่ให้อัตโนมัติ

## 14. หยุด bot ชั่วคราว

วิธีที่ง่ายที่สุด:

```text
Cloud Scheduler -> shannon-demon-every-5m -> Pause
```

เปิดกลับ:

```text
Cloud Scheduler -> shannon-demon-every-5m -> Resume
```

## 15. Checklist สุดท้าย

- สร้าง project แล้ว
- เปิด Billing แล้ว
- เปิด API ครบแล้ว
- สร้าง Firestore `(default)` แบบ Native แล้ว
- Cloud Run เชื่อม repo `firstnattapon/webull` แล้ว
- Build context ถูกต้อง: `webull-main` หรือ `/` ตามโครง repo จริง
- Function target คือ `rebalance_trigger`
- Authentication เป็น `Require authentication`
- ใส่ env-vars ครบแล้ว
- Cloud Run service account มี `Cloud Datastore User`
- สร้าง Scheduler service account แล้ว
- Scheduler service account มี `Cloud Run Invoker`
- Cloud Scheduler ใช้ OIDC token แล้ว
- กด Force run แล้วเห็น log ใน Cloud Run

เมื่อครบ checklist นี้ ต่อไปงานประจำมีแค่:

```text
push code เข้า GitHub -> Google Cloud auto deploy
```

และถ้าต้องการเปลี่ยนค่าเทรด:

```text
Cloud Run -> Edit & deploy new revision -> แก้ env-vars -> Deploy
```

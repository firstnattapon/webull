# Quick Start 101: Cloud Shell + Auto Deploy จาก GitHub 

คู่มือนี้สำหรับมือใหม่ที่ต้องการ deploy บน Google Cloud โดยใช้ **Cloud Shell** ช่วยตั้งค่า แต่ **ไม่ใช้ GitHub Actions** 

วิธี deploy หลักยังเป็น:

```text
Cloud Run -> Connect repository -> Cloud Build -> GitHub -> firstnattapon/webull
```

เมื่อ push code เข้า branch ที่เลือก Google Cloud จะ build และ deploy ให้อัตโนมัติ

## สิ่งที่จะได้

- Google Cloud project สำหรับ bot
- Firestore สำหรับเก็บ state และ trade log
- Cloud Run function ชื่อ `shannon-demon-bot`
- Auto deploy จาก GitHub repo ผ่าน Cloud Build
- Environment variables ใส่เองด้วย Cloud Shell
- Cloud Scheduler ยิง bot ทุก 5 นาที

Repo:

```text
https://github.com/firstnattapon/webull
```

Function target:

```text
rebalance_trigger
```

## 0. ค่าที่ต้องเตรียม

ต้องมี Webull credentials 3 ค่า:

```text
WEBULL_APP_KEY=app key จริง
WEBULL_APP_SECRET=app secret จริง
WEBULL_ACCOUNT_ID=account id จริง
```

ในคู่มือนี้ใช้เฉพาะ 3 ตัวนี้สำหรับ Webull credentials

สำหรับมือใหม่ แนะนำเริ่มแบบทดสอบ:

```text
WEBULL_ENV=uat
WEBULL_PREVIEW_ORDERS=true
```

`WEBULL_PREVIEW_ORDERS=true` ช่วยให้ preview order ก่อนส่งจริง เหมาะกับช่วงเริ่มต้น

## 1. สร้าง Google Cloud project

เปิด Google Cloud Console:

```text
https://console.cloud.google.com/
```

ทำตามนี้:

1. กดตัวเลือก project ด้านบน
2. กด **New Project**
3. ตั้งชื่อ เช่น `webull-bot-smr`
4. กด **Create**
5. เลือก project ที่สร้างใหม่
6. เปิด Billing ให้ project นี้

จด **Project ID** ไว้ เช่น:

```text
webull-bot-smr-123456
```

จากนี้ในคำสั่ง Cloud Shell ให้แทน `YOUR_PROJECT_ID` ด้วย Project ID ของคุณ

## 2. เปิด Cloud Shell และตั้ง project

ใน Google Cloud Console กด **Activate Cloud Shell** ด้านบนขวา

รัน:

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export REGION=asia-southeast1
export SERVICE_NAME=shannon-demon-bot

gcloud config set project "$PROJECT_ID"
gcloud config get-value project
```

บรรทัดสุดท้ายควรแสดง Project ID ของคุณ

## 3. เปิด API ที่ต้องใช้

รันใน Cloud Shell:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  iamcredentials.googleapis.com
```

รอจนคำสั่งจบ ถ้า Google แจ้งว่าบาง API เปิดอยู่แล้ว ถือว่าปกติ

## 4. สร้าง Firestore

รัน:

```bash
gcloud firestore databases create \
  --database="(default)" \
  --location="$REGION"
```

ถ้าขึ้นว่ามี database แล้ว ให้ข้ามได้

โค้ดจะใช้ Firestore path นี้:

```text
shannon_demon_state / SHANNON_DEMON_DNA_SMR
shannon_demon_trades / auto generated documents
```

ยังไม่ต้องสร้าง collection เอง โค้ดจะสร้างตอนเริ่มทำงาน

## 5. Deploy จาก GitHub repo แบบ auto update

ขั้นตอนนี้ทำในหน้าเว็บ Google Cloud Console ไม่ใช่ GitHub Actions

ไปที่:

```text
Cloud Run -> Services
```

ทำตามนี้:

1. กด **Connect repository**
2. เลือก **Cloud Build**
3. เลือก **GitHub**
4. กด **Authenticate** ถ้ายังไม่เคยเชื่อม GitHub
5. เลือก repo:

```text
firstnattapon/webull
```

6. Branch: ใส่ branch ที่ต้องการ auto deploy เช่น:

```text
^main$
```

7. Build type: เลือก **Buildpacks**
8. Build context directory: repo นี้วาง `main.py` และ `requirements.txt` ไว้ที่ root ดังนั้นให้ใช้:

```text
/
```

อย่าใส่ `webull-main` — โฟลเดอร์นั้นมีเฉพาะตอนโหลด zip จาก GitHub มาแตกเอง ถ้าใส่ผิด Cloud Build จะหา `requirements.txt` ไม่เจอและ build ล้มทุกครั้ง

9. Function target:

```text
rebalance_trigger
```

10. กด **Save**

Google Cloud จะสร้าง Cloud Build trigger ให้เอง ต่อไปเมื่อ push code เข้า branch ที่เลือก ระบบจะ auto deploy ให้

## 6. ตั้งค่า Cloud Run service

ในหน้า Create service ให้ตั้งค่า:

```text
Service name: shannon-demon-bot
Region: asia-southeast1
Runtime: Python 3.12
Authentication: Require authentication
```

ถ้าไม่มี Python 3.12 ให้เลือก Python 3.11

หมายเหตุ: repo นี้มีไฟล์ `.python-version` (pin Python 3.12) และ `Procfile` (สั่งรัน `functions-framework --target=rebalance_trigger`) อยู่แล้ว Buildpacks จะอ่านสองไฟล์นี้อัตโนมัติ จึงไม่ต้องกังวลว่า Buildpacks จะเลือก Python เวอร์ชันใหม่เกินไป (เช่น 3.13 ที่ `numpy==1.26.4` ติดตั้งไม่ได้) หรือหา entrypoint ไม่เจอ

ตรงแท็บ environment variables ยังไม่ต้องใส่ก็ได้ เพราะขั้นต่อไปจะใช้ Cloud Shell ช่วยใส่ให้

จากนั้นกด **Create** แล้วรอ build ครั้งแรกให้เสร็จ

ดูสถานะได้ที่:

```text
Cloud Build -> History
Cloud Run -> shannon-demon-bot -> Revisions
```

## 7. ใส่ environment variables ด้วย Cloud Shell

หลังจาก service ถูกสร้างแล้ว ให้กลับมาที่ Cloud Shell

ตั้งค่าหลักที่ไม่ใช่ Webull credential:

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},STRATEGY_ID=SHANNON_DEMON_DNA,SYMBOL=SMR,FIX_C=1500,P0=9.00,DIFF=30,DNA_CODE=bypass:100,START_TIMESTAMP=0,FIRESTORE_STATE_COLLECTION=shannon_demon_state,FIRESTORE_TRADE_COLLECTION=shannon_demon_trades,FIRESTORE_STATE_DOCUMENT=SHANNON_DEMON_DNA_SMR,WEBULL_ENV=uat,WEBULL_API_VERSION=v2,WEBULL_REGION=th,WEBULL_SUPPORT_TRADING_SESSION=CORE,WEBULL_PREVIEW_ORDERS=true"
```

จากนั้นใส่ Webull credentials โดยแทน `...` เป็นค่าจริง:

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="WEBULL_APP_KEY=...,WEBULL_APP_SECRET=...,WEBULL_ACCOUNT_ID=..."
```

คำสั่งนี้จะสร้าง Cloud Run revision ใหม่ แต่ยังคง auto deploy จาก GitHub repo ตามข้อ 5 เหมือนเดิม

ตรวจ env-vars ที่ตั้งไว้:

```bash
gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format="yaml(spec.template.spec.containers[0].env)"
```

## 8. ให้ Cloud Run ใช้ Firestore ได้

ดู service account ที่ Cloud Run ใช้:

```bash
RUN_SA=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format="value(spec.template.spec.serviceAccountName)")

if [ -z "$RUN_SA" ]; then
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
  RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

echo "$RUN_SA"
```

ให้สิทธิ์อ่าน/เขียน Firestore:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/datastore.user"
```

## 9. ทดสอบ health check

หา URL ของ service:

```bash
URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format="value(status.url)")

echo "$URL"
```

เรียก health endpoint:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$URL/health"
```

ถ้าสำเร็จควรเห็น:

```text
"status":"HEALTHY"
```

ถ้าเห็น `UNHEALTHY` ให้อ่านส่วน `checks` ในผลลัพธ์ โดยมากจะบอกว่า env var ตัวไหนขาดหรือค่าไหนผิด

## 10. ตั้ง Cloud Scheduler

สร้าง service account สำหรับ Scheduler:

```bash
gcloud iam service-accounts create scheduler-invoker \
  --display-name="Scheduler Invoker"
```

ตั้งตัวแปร:

```bash
SCHEDULER_SA="scheduler-invoker@${PROJECT_ID}.iam.gserviceaccount.com"
```

ให้ Scheduler เรียก Cloud Run ได้:

```bash
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker"
```

สร้าง Scheduler job ยิงทุก 5 นาที:

```bash
gcloud scheduler jobs create http shannon-demon-every-5m \
  --location="$REGION" \
  --schedule="*/5 * * * *" \
  --time-zone="Asia/Bangkok" \
  --uri="$URL" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="$URL"
```

ถ้ามี job อยู่แล้ว ให้ใช้คำสั่ง update:

```bash
gcloud scheduler jobs update http shannon-demon-every-5m \
  --location="$REGION" \
  --schedule="*/5 * * * *" \
  --time-zone="Asia/Bangkok" \
  --uri="$URL" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="$URL"
```

## 11. ยิงทดสอบทันที

สั่ง Scheduler ให้ยิงทันที:

```bash
gcloud scheduler jobs run shannon-demon-every-5m \
  --location="$REGION"
```

ดู logs:

```bash
gcloud run services logs read "$SERVICE_NAME" \
  --region="$REGION" \
  --limit=50
```

สถานะที่เจอบ่อย:

| Status | ความหมาย |
|---|---|
| `PASS_WAITING_TO_START` | ยังไม่ถึง `START_TIMESTAMP` |
| `PASS_MARKET_CLOSED` | ตลาดสหรัฐปิด |
| `PASS_DNA_ZERO` | DNA รอบนี้เป็น 0 เลยข้าม |
| `PASS_THRESHOLD` | ยังอยู่ในช่วง `DIFF` |
| `OK` | ส่ง order แล้ว |
| `BROKER_ERROR` | Webull ตอบ error |
| `ERROR` | config หรือระบบมีปัญหา |

## 12. ตรวจ Firestore

ดู state document:

```bash
gcloud firestore documents describe \
  "projects/${PROJECT_ID}/databases/(default)/documents/shannon_demon_state/SHANNON_DEMON_DNA_SMR"
```

ถ้ายังไม่เจอ document อาจเป็นเพราะ bot ยังไม่ผ่านขั้นที่ต้องเขียน Firestore เช่น ตลาดปิด หรือ health check ยังไม่เรียก flow เทรด

ถ้าต้องการ reset step กลับ 0 ให้ลบ document นี้ในหน้า Firestore Console:

```text
Firestore -> Data -> shannon_demon_state -> SHANNON_DEMON_DNA_SMR -> Delete
```

## 13. แก้ค่า env-vars หลัง deploy

แก้ด้วย Cloud Shell ได้ เช่นเปลี่ยน strategy:

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="SYMBOL=SMR,FIX_C=1500,P0=9.00,DIFF=30,DNA_CODE=bypass:100,WEBULL_ENV=uat,WEBULL_PREVIEW_ORDERS=true"
```

ถ้าแก้ Webull credentials:

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="WEBULL_APP_KEY=...,WEBULL_APP_SECRET=...,WEBULL_ACCOUNT_ID=..."
```

ทุกครั้งที่ update env-vars Cloud Run จะสร้าง revision ใหม่ทันที

## 14. หยุด bot ชั่วคราว

หยุด Scheduler:

```bash
gcloud scheduler jobs pause shannon-demon-every-5m \
  --location="$REGION"
```

เปิดกลับ:

```bash
gcloud scheduler jobs resume shannon-demon-every-5m \
  --location="$REGION"
```

## 15. แก้ปัญหา: "Building and deploying from repository (see logs)" ค้าง ไม่ Active ซักที

อาการนี้แปลว่า Cloud Build ที่ trigger จาก GitHub **build ไม่สำเร็จเลยแม้แต่ครั้งเดียว** สังเกตได้จาก service YAML ที่ container ยังเป็น:

```yaml
containers:
- name: placeholder-1
  image: gcr.io/cloudrun/placeholder
```

ถ้ายังเห็น `gcr.io/cloudrun/placeholder` แปลว่า image จริงยังไม่เคยถูก deploy — ปัญหาอยู่ที่ขั้น build ไม่ใช่ตัว bot

### ขั้นแรก: อ่าน build log จริง

```bash
gcloud builds list --region=global --limit=5
gcloud builds log BUILD_ID
```

หรือเปิดหน้าเว็บ: **Cloud Build -> History** แล้วกด build สีแดงล่าสุด

### สาเหตุที่พบบ่อย

| อาการใน log | สาเหตุ | วิธีแก้ |
|---|---|---|
| `requirements.txt not found` หรือ build จบเร็วผิดปกติ | Build context directory ใส่ `webull-main` | แก้ trigger ให้ context เป็น `/` (Cloud Build -> Triggers -> Edit) |
| `ERROR: Could not find a version that satisfies the requirement numpy==1.26.4` หรือ numpy build จาก source แล้วพัง | Buildpacks เลือก Python 3.13 ซึ่ง numpy 1.26.4 ไม่รองรับ | pull code ล่าสุดที่มีไฟล์ `.python-version` (pin 3.12) แล้ว push ใหม่ให้ trigger รัน |
| `unable to detect entrypoint` / `no web process` | ไม่ได้ใส่ Function target ตอนสร้าง trigger | pull code ล่าสุดที่มี `Procfile` หรือแก้ trigger ให้ Function target เป็น `rebalance_trigger` |
| build เขียว แต่ revision ล้มด้วย `container failed to start / startup probe failed` | container ไม่ได้ listen ที่ port 8080 | เช็คว่า Function target คือ `rebalance_trigger` และมี `Procfile` ใน repo |
| `Permission denied` ตอน deploy | Cloud Build service account ไม่มีสิทธิ์ deploy Cloud Run | ให้ role `roles/run.admin` + `roles/iam.serviceAccountUser` แก่ Cloud Build SA |

หลังแก้แล้ว สั่ง build ใหม่ได้โดย push commit ใดก็ได้เข้า branch ที่ trigger จับ หรือกด **Run** ที่ Cloud Build -> Triggers

### เช็ค env vars ที่ยังเป็น placeholder

ถ้าเคยกด Create service จากหน้า console โดยยังไม่แก้ค่า จะเห็นค่าแบบนี้ค้างอยู่:

```yaml
- name: GCP_PROJECT_ID
  value: YOUR_PROJECT_ID
```

`YOUR_PROJECT_ID` เป็นแค่ตัวอย่าง ต้องแทนด้วย Project ID จริงตามข้อ 7 ไม่งั้นถึง build ผ่าน bot ก็จะตอบ `ERROR` เพราะต่อ Firestore ไม่ได้:

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="GCP_PROJECT_ID=${PROJECT_ID}"
```

### คำเตือนเรื่อง credentials

`WEBULL_APP_KEY` และ `WEBULL_APP_SECRET` ที่ใส่เป็น env var จะมองเห็นได้ในหน้า console และ service YAML ห้ามแชร์ YAML นี้ให้คนอื่น ถ้าเผลอแชร์ไปแล้วให้ rotate key ในหน้า Webull OpenAPI ทันที

## 16. Checklist มือใหม่

ก่อนจบ ให้เช็คทีละข้อ:

- สร้าง project แล้ว
- เปิด Billing แล้ว
- เปิด Cloud Shell แล้ว
- ตั้ง `PROJECT_ID`, `REGION`, `SERVICE_NAME` แล้ว
- เปิด API ครบแล้ว
- สร้าง Firestore `(default)` แล้ว
- Cloud Run กด **Connect repository** แล้ว
- เลือก **Cloud Build** แล้ว
- เลือก **GitHub** และ Authenticate แล้ว
- เลือก repo `firstnattapon/webull` แล้ว
- Build context คือ `/` (ไฟล์อยู่ที่ root ของ repo)
- เห็นไฟล์ `.python-version` และ `Procfile` ใน branch ที่ deploy
- Function target คือ `rebalance_trigger`
- Cloud Run service ชื่อ `shannon-demon-bot`
- ใส่ env-vars ด้วย Cloud Shell ครบแล้ว
- ใช้ `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_ACCOUNT_ID`
- Cloud Run service account มี `roles/datastore.user`
- Scheduler service account มี `roles/run.invoker`
- Scheduler ใช้ OIDC token แล้ว
- `curl "$URL/health"` ได้ `HEALTHY`
- สั่ง Scheduler run แล้วเห็น logs

หลังจากนี้ workflow คือ:

```text
แก้ code -> push เข้า GitHub branch ที่เลือก -> Cloud Build auto deploy -> Cloud Run ได้ revision ใหม่
```

ถ้าแค่เปลี่ยนค่า strategy หรือ Webull credential:

```text
ใช้ gcloud run services update --update-env-vars
```

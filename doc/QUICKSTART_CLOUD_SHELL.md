# 🚀 Quick Start 101: Deploy Shannon Demon Bot บน Google Cloud

คู่มือมือใหม่ ทำตามทีละขั้นได้เลย ใช้เวลาประมาณ 30 นาที

## ภาพรวม: เรากำลังจะทำอะไร

```text
GitHub (repo นี้)  ──push──▶  Cloud Build (build อัตโนมัติ)  ──deploy──▶  Cloud Run (bot ทำงาน)
                                                                              ▲
Cloud Scheduler (ยิงทุก 5 นาที) ──────────────────────────────────────────────┘
                                                                              │
                                                            Firestore (เก็บ state + trade log)
```

แปลง่าย ๆ:

1. เชื่อม GitHub กับ Google Cloud **ครั้งเดียว**
2. หลังจากนั้น push code เข้า `main` เมื่อไหร่ ระบบ build + deploy ให้เองอัตโนมัติ
3. Cloud Scheduler ปลุก bot ทุก 5 นาที ให้เช็คว่าต้องเทรดหรือไม่
4. bot เก็บ state และประวัติเทรดไว้ใน Firestore

## สิ่งที่จะได้เมื่อทำจบ

- ✅ Cloud Run service ชื่อ `shannon-demon-bot` ที่ auto deploy จาก GitHub
- ✅ Firestore เก็บ state และ trade log
- ✅ Cloud Scheduler ยิง bot ทุก 5 นาที
- ✅ Health check endpoint ไว้ตรวจสุขภาพ bot

## ไฟล์สำคัญใน repo (ห้ามลบ!)

| ไฟล์ | หน้าที่ |
|---|---|
| `.python-version` | บอก Google ให้ใช้ **Python 3.13** (builder ปัจจุบันมีแค่ 3.13 ขึ้นไป — pin ต่ำกว่านี้ build จะพัง) |
| `Procfile` | บอกวิธี start bot: `functions-framework --target=rebalance_trigger` |
| `requirements.txt` | รายการ library ที่ต้องติดตั้ง (numpy ต้องเป็น 2.x เพื่อรองรับ Python 3.13) |
| `main.py` | จุดเริ่มต้นของ bot มี function ชื่อ `rebalance_trigger` |

---

# ส่วนที่ 1: เตรียมของ

ต้องมี 2 อย่าง:

**1. Webull credentials 3 ค่า** (ขอจากหน้า Webull OpenAPI):

```text
WEBULL_APP_KEY      = app key จริง
WEBULL_APP_SECRET   = app secret จริง
WEBULL_ACCOUNT_ID   = account id จริง
```

**2. Google account** ที่เปิด Billing ได้ (มีบัตรผูก)

💡 มือใหม่ให้เริ่มที่ `WEBULL_ENV=uat` เสมอ ค่า `WEBULL_PREVIEW_ORDERS=true` หมายถึง preview ก่อน แต่โค้ดยังเรียก `place_order` ต่อเมื่อเงื่อนไขเทรดครบ จึงไม่ใช่โหมดดูอย่างเดียว

---

# ส่วนที่ 2: ตั้งค่า Google Cloud (ทำครั้งเดียว)

## ขั้นที่ 1: สร้าง project

เปิด https://console.cloud.google.com/ แล้ว:

1. กดตัวเลือก project ด้านบน → **New Project**
2. ตั้งชื่อ เช่น `webull-bot-smr` → **Create**
3. เลือก project ที่เพิ่งสร้าง
4. เปิด **Billing** ให้ project นี้

จด **Project ID** ไว้ (เช่น `webull-bot-smr-123456`) — ต้องใช้ตลอดทั้งคู่มือ

## ขั้นที่ 2: เปิด Cloud Shell

กดปุ่ม **Activate Cloud Shell** (ไอคอน `>_` มุมขวาบน) แล้ววางคำสั่งนี้ (แก้ `YOUR_PROJECT_ID` เป็นของจริงก่อน):

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export REGION=asia-southeast1
export SERVICE_NAME=shannon-demon-bot

gcloud config set project "$PROJECT_ID"
gcloud config get-value project
```

บรรทัดสุดท้ายต้องแสดง Project ID ของคุณ ถ้าใช่ = ผ่าน

⚠️ ถ้าปิด Cloud Shell แล้วเปิดใหม่ ให้รัน `export` 3 บรรทัดบนนี้ใหม่ทุกครั้ง

## ขั้นที่ 3: เปิด API ที่ต้องใช้

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

รอจนจบ ถ้าบอกว่าบางตัวเปิดอยู่แล้ว = ปกติ ไปต่อได้

## ขั้นที่ 4: สร้าง Firestore

```bash
gcloud firestore databases create \
  --database="(default)" \
  --location="$REGION"
```

ถ้าขึ้นว่ามี database อยู่แล้ว = ข้ามได้

ไม่ต้องสร้าง collection เอง — bot จะสร้างให้ตอนทำงานครั้งแรก:

```text
shannon_demon_state / SHANNON_DEMON_DNA_SMR   ← state ปัจจุบัน
shannon_demon_trades / (สร้างอัตโนมัติ)        ← ประวัติเทรด
```

---

# ส่วนที่ 3: เชื่อม GitHub ให้ deploy อัตโนมัติ (ทำครั้งเดียว)

ขั้นตอนนี้ทำใน**หน้าเว็บ** Google Cloud Console

## ขั้นที่ 5: Connect repository

ไปที่ **Cloud Run → Services** แล้วทำตามนี้:

| ลำดับ | ตั้งค่า | ใส่ค่า |
|---|---|---|
| 1 | กด | **Connect repository** หรือ **Continuously deploy from a repository** |
| 2 | Provider | **Cloud Build** → **GitHub** (กด **Authenticate** ถ้ายังไม่เคยเชื่อม) |
| 3 | Repository | `firstnattapon/webull` |
| 4 | Branch | `^main$` |
| 5 | Build type | **Buildpacks** |
| 6 | Build context directory | `/` |
| 7 | Entrypoint | เว้นว่าง |
| 8 | Function target | `rebalance_trigger` |

⚠️ **Build context ต้องเป็น `/`** เพราะ `main.py` กับ `requirements.txt` อยู่ที่ root ของ repo — ถ้าใส่อย่างอื่น (เช่น `webull-main`) build จะหาไฟล์ไม่เจอและล้มทุกครั้ง

ถ้าหา repo ไม่เจอ ให้กด **Manage connected repositories** แล้วอนุญาต Cloud Build GitHub App ให้เข้าถึง `firstnattapon/webull`

กด **Save** เพื่อสร้าง Cloud Build trigger ขั้นตอนนี้ทำครั้งเดียว หลังจากนั้นทุก push เข้า `main` จะ build และ deploy revision ใหม่อัตโนมัติ

## ขั้นที่ 6: ตั้งค่า service แล้วกด Create

```text
Service name:    shannon-demon-bot
Region:          asia-southeast1
Authentication:  Require authentication
Runtime:          Python 3.13
Concurrency:      1
Max instances:    1
```

แท็บ environment variables **ยังไม่ต้องใส่** — เดี๋ยวใช้ Cloud Shell ใส่ในขั้นถัดไป

กด **Create** แล้วรอ build ครั้งแรก (ประมาณ 2-5 นาที) ดูสถานะได้ที่:

```text
Cloud Build → History          ← build เขียวหรือแดง
Cloud Run → shannon-demon-bot  ← service ขึ้น Active หรือยัง
```

✅ สำเร็จเมื่อ: build เขียว และ service มีเครื่องหมายถูกสีเขียว (Active)

ตรวจว่า auto deploy พร้อมใช้งาน:

1. ไปที่ **Cloud Build → Triggers**
2. ต้องเห็น trigger ของ `shannon-demon-bot` และสถานะเปิดใช้งาน
3. Event ต้องเป็น push และ Branch ต้องเป็น `^main$`
4. ไปที่ **Cloud Run → shannon-demon-bot** ต้องเห็น Source repository เป็น `firstnattapon/webull`
5. ต่อไปทุกครั้งที่ push เข้า `main` ให้ดู build ที่ **Cloud Build → History** แล้วรอ revision ใหม่ขึ้น Active

❌ ถ้าแดง: ไปดู [ส่วนที่ 6: แก้ปัญหา](#ส่วนที่-6-แก้ปัญหา-build-ไม่ผ่าน) ด้านล่าง

---

# ส่วนที่ 4: ใส่ค่า config ให้ bot

## ขั้นที่ 7: ใส่ environment variables

กลับมาที่ Cloud Shell รันทีละคำสั่ง

**คำสั่งที่ 1** — ค่า strategy ทั้งหมด (copy ได้เลย ไม่ต้องแก้):

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},STRATEGY_ID=SHANNON_DEMON_DNA,SYMBOL=SMR,FIX_C=1500,P0=9.00,DIFF=30,DNA_CODE=bypass:100,START_TIMESTAMP=0,SCHEDULE_SLOT_SECONDS=300,FIRESTORE_STATE_COLLECTION=shannon_demon_state,FIRESTORE_TRADE_COLLECTION=shannon_demon_trades,FIRESTORE_STATE_DOCUMENT=SHANNON_DEMON_DNA_SMR,WEBULL_ENV=uat,WEBULL_API_VERSION=v3,WEBULL_REGION=th,WEBULL_SUPPORT_TRADING_SESSION=CORE,WEBULL_PREVIEW_ORDERS=true" \
  --concurrency=1 \
  --max-instances=1
```

**คำสั่งที่ 2** — Webull credentials (แก้ `...` เป็นค่าจริงก่อนรัน):

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="WEBULL_APP_KEY=...,WEBULL_APP_SECRET=...,WEBULL_ACCOUNT_ID=..."
```

ตรวจว่าใส่ครบ:

```bash
gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format="yaml(spec.template.spec.containers[0].env)"
```

⚠️ เช็คว่า `GCP_PROJECT_ID` เป็น Project ID **จริง** ไม่ใช่คำว่า `YOUR_PROJECT_ID` ค้างอยู่

## ขั้นที่ 8: ให้สิทธิ์ bot ใช้ Firestore

```bash
RUN_SA=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format="value(spec.template.spec.serviceAccountName)")

if [ -z "$RUN_SA" ]; then
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
  RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/datastore.user"
```

## ขั้นที่ 9: ทดสอบว่า bot สุขภาพดี

```bash
URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format="value(status.url)")

curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/health"
```

✅ ต้องเห็น `"status":"HEALTHY"`

❌ ถ้าเห็น `UNHEALTHY` ให้อ่านส่วน `checks` ในคำตอบ — มันจะบอกเลยว่า env var ตัวไหนขาดหรือผิด แก้ด้วยคำสั่งในขั้นที่ 7 แล้ว curl ใหม่

---

# ส่วนที่ 5: ตั้งเวลาให้ bot ทำงานเอง

## ขั้นที่ 10: สร้าง Cloud Scheduler ยิงทุก 5 นาที

```bash
gcloud iam service-accounts create scheduler-invoker \
  --display-name="Scheduler Invoker"

SCHEDULER_SA="scheduler-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http shannon-demon-every-5m \
  --location="$REGION" \
  --schedule="*/5 * * * *" \
  --time-zone="Asia/Bangkok" \
  --uri="$URL" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="$URL"
```

## ขั้นที่ 11: ยิงทดสอบทันที

```bash
gcloud scheduler jobs run shannon-demon-every-5m --location="$REGION"

gcloud run services logs read "$SERVICE_NAME" --region="$REGION" --limit=50
```

สถานะที่จะเจอใน logs:

| Status | ความหมาย | ต้องทำอะไร |
|---|---|---|
| `PASS_MARKET_CLOSED` | ตลาดสหรัฐปิดอยู่ | ปกติ ไม่ต้องทำอะไร |
| `PASS_WAITING_TO_START` | ยังไม่ถึงเวลา `START_TIMESTAMP` | ปกติ |
| `PASS_DNA_ZERO` | DNA รอบนี้เป็น 0 เลยข้าม | ปกติ |
| `PASS_THRESHOLD` | ราคายังไม่ขยับพอ (อยู่ในช่วง `DIFF`) | ปกติ |
| `PASS_DUPLICATE_TICK` | ถูกเรียกซ้ำใน slot เดิม (เช่น Force run) เลยไม่กิน DNA step ซ้ำ | ปกติ (ต้องตั้ง `SCHEDULE_SLOT_SECONDS`) |
| `OK` | ส่ง order แล้ว 🎉 | เช็คใน Webull |
| `BROKER_ERROR` | Webull ตอบ error | เช็ค credentials / ดู log |
| `ERROR` | config หรือระบบมีปัญหา | ดู log แล้วแก้ env var |

🎉 **จบแล้ว! bot ทำงานอัตโนมัติแล้ว** ที่เหลือด้านล่างคือการใช้งานประจำวันกับการแก้ปัญหา

---

# การใช้งานประจำวัน

**แก้ code** → push เข้า `main` → deploy เองอัตโนมัติ ไม่ต้องทำอะไรเพิ่ม

**แก้ค่า strategy** (ไม่ต้องแตะ code):

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="SYMBOL=SMR,FIX_C=1500,P0=9.00,DIFF=30"
```

**หยุด bot ชั่วคราว / เปิดกลับ:**

```bash
gcloud scheduler jobs pause shannon-demon-every-5m --location="$REGION"
gcloud scheduler jobs resume shannon-demon-every-5m --location="$REGION"
```

**ดู logs:**

```bash
gcloud run services logs read "$SERVICE_NAME" --region="$REGION" --limit=50
```

**ดู state / ประวัติเทรดใน Firestore:** เปิดหน้า Console → **Firestore → Data** → collection `shannon_demon_state` และ `shannon_demon_trades`

**Reset step กลับ 0:** ลบ document `shannon_demon_state / SHANNON_DEMON_DNA_SMR` ในหน้า Firestore

**เปลี่ยนจากทดสอบเป็นของจริง** (คิดให้ดีก่อน!):

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="WEBULL_ENV=prod,WEBULL_REGION=th,WEBULL_API_VERSION=v3,WEBULL_PREVIEW_ORDERS=true,WEBULL_APP_KEY=...,WEBULL_APP_SECRET=...,WEBULL_ACCOUNT_ID=..."
```

Production ต้องใช้ credentials ของ Production และ endpoint ที่ระบบเลือกต้องเป็น `api.webull.co.th` ส่วน UAT ใช้ `th-api.uat.webullbroker.com`

---

# ส่วนที่ 6: แก้ปัญหา build ไม่ผ่าน

อาการ: service ขึ้น **"Building and deploying from repository (see logs)"** ค้าง ไม่ Active ซักที

แปลว่า build ยังไม่เคยสำเร็จ — เช็คได้จาก service YAML ถ้า image ยังเป็น `gcr.io/cloudrun/placeholder` = ยังไม่มี image จริง

**ขั้นแรกเสมอ: อ่าน build log**

```bash
gcloud builds list --region=global --limit=5
gcloud builds log BUILD_ID
```

หรือหน้าเว็บ: **Cloud Build → History** → กด build สีแดงล่าสุด → อ่าน error บรรทัดท้าย ๆ

**ตารางอาการที่เจอบ่อย** (จากเหตุการณ์จริงของ repo นี้):

| Error ใน log | สาเหตุ | วิธีแก้ |
|---|---|---|
| `invalid Python version specified: failed to resolve version matching: 3.12 against [3.14.x ... 3.13.0]` | `.python-version` pin เวอร์ชันที่ builder ไม่มีแล้ว (มีแค่ 3.13+) | แก้ `.python-version` เป็น `3.13` |
| `Could not find a version that satisfies the requirement numpy==1.26.x` | numpy 1.26 ไม่รองรับ Python 3.13 | ใช้ `numpy==2.3.1` ใน `requirements.txt` |
| `requirements.txt not found` | Build context directory ผิด | แก้ trigger ให้ context เป็น `/` (Cloud Build → Triggers → Edit) |
| `unable to detect entrypoint` / `no web process` | ไม่ได้ใส่ Function target และไม่มี `Procfile` | เช็คว่า `Procfile` ยังอยู่ใน repo หรือแก้ trigger ให้ Function target = `rebalance_trigger` |
| build เขียวแต่ revision ล้ม `startup probe failed` | container ไม่ listen port 8080 | เช็ค `Procfile` และ Function target ตามข้อบน |
| `Permission denied` ตอน deploy | Cloud Build ไม่มีสิทธิ์ deploy | ให้ role `roles/run.admin` + `roles/iam.serviceAccountUser` แก่ Cloud Build service account |

หลังแก้แล้ว: push commit อะไรก็ได้เข้า `main` หรือกด **Run** ที่ Cloud Build → Triggers เพื่อ build ใหม่

**bot deploy ผ่านแล้วแต่ตอบ `ERROR`:** ส่วนใหญ่คือ `GCP_PROJECT_ID` ยังเป็นค่า placeholder — แก้ด้วย:

```bash
gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="GCP_PROJECT_ID=${PROJECT_ID}"
```

---

# Checklist สรุป

ไล่เช็คทีละข้อ:

- [ ] สร้าง project + เปิด Billing แล้ว
- [ ] เปิด API ครบ 7 ตัวแล้ว (ขั้นที่ 3)
- [ ] สร้าง Firestore `(default)` แล้ว
- [ ] Connect repository: repo `firstnattapon/webull`, branch `^main$`, Buildpacks, context `/`, target `rebalance_trigger`
- [ ] Cloud Build trigger เปิดใช้งานและจับ push เข้า `^main$`
- [ ] Cloud Build เขียว + service `shannon-demon-bot` ขึ้น Active
- [ ] ใส่ env vars ครบ (ขั้นที่ 7) และ `GCP_PROJECT_ID` เป็นค่าจริง
- [ ] Cloud Run service account มี `roles/datastore.user`
- [ ] `curl $URL/health` ได้ `HEALTHY`
- [ ] Scheduler สร้างแล้ว + สั่ง run แล้วเห็น logs

---

# ⚠️ ความปลอดภัย

- `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` ที่ใส่เป็น env var **มองเห็นได้**ในหน้า Console และ service YAML — **ห้ามแชร์ screenshot หรือ YAML ของ service ให้ใคร**
- ถ้าเผลอแชร์ key ไปแล้ว ให้ rotate key ใหม่ในหน้า Webull OpenAPI ทันที แล้วอัปเดต env var
- เริ่มด้วย `WEBULL_ENV=uat` เสมอ และจำไว้ว่า `WEBULL_PREVIEW_ORDERS=true` ยังเรียก `place_order` ต่อ

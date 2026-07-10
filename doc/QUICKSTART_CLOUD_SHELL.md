# Quick Start ด้วย Google Cloud Shell

ใช้คำสั่งชุดนี้สำหรับ Webull Thailand UAT และ Cloud Run ปัจจุบัน

## 1. กำหนดค่าหลัก

เปิด Google Cloud Shell แล้ววางทีละชุด:

```bash
PROJECT_ID="YOUR_PROJECT_ID"
REGION="asia-southeast1"
SERVICE="shannon-demon-bot"

gcloud config set project "$PROJECT_ID"
```

## 2. เปิด API และสร้าง Firestore

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  cloudscheduler.googleapis.com \
  iamcredentials.googleapis.com

gcloud firestore databases create \
  --database="(default)" \
  --location="$REGION" \
  --type=firestore-native
```

ถ้ามี Firestore `(default)` อยู่แล้วและคำสั่งแจ้งว่า exists ให้ข้ามได้

## 3. Deploy จาก GitHub

วิธีง่ายที่สุดคือเชื่อม repo ที่หน้า **Cloud Run > Connect repository**:

```text
Repository: firstnattapon/webull
Branch: ^main$
Buildpacks
Build context: /
Function target: rebalance_trigger
Service: shannon-demon-bot
Region: asia-southeast1
Require authentication
```

เมื่อ service ขึ้น Active แล้ว ให้กลับมา Cloud Shell

## 4. ใส่ค่าระบบและ Webull UAT

แทนค่า `YOUR_...` ก่อนรัน:

```bash
WEBULL_ACCOUNT_ID="YOUR_UAT_ACCOUNT_ID"
WEBULL_APP_KEY="YOUR_UAT_APP_KEY"
WEBULL_APP_SECRET="YOUR_UAT_APP_SECRET"

gcloud run services update "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --update-env-vars="GCP_PROJECT_ID=$PROJECT_ID,STRATEGY_ID=SHANNON_DEMON_DNA,SYMBOL=SMR,FIX_C=1500,P0=9,DIFF=30,DNA_CODE=bypass:100,START_TIMESTAMP=0,FIRESTORE_STATE_COLLECTION=shannon_demon_state,FIRESTORE_TRADE_COLLECTION=shannon_demon_trades,FIRESTORE_STATE_DOCUMENT=SHANNON_DEMON_DNA_SMR,WEBULL_ENV=uat,WEBULL_API_VERSION=v3,WEBULL_REGION=th,WEBULL_SUPPORT_TRADING_SESSION=CORE,WEBULL_PREVIEW_ORDERS=true,WEBULL_ACCOUNT_ID=$WEBULL_ACCOUNT_ID,WEBULL_APP_KEY=$WEBULL_APP_KEY,WEBULL_APP_SECRET=$WEBULL_APP_SECRET" \
  --concurrency=1 \
  --max-instances=1
```

> Account ID ต้องใส่ในเครื่องหมายคำพูด เพื่อไม่ให้เลขจำนวนมากถูกปัดค่า

> คำสั่งนี้เก็บ credentials เป็น environment variables ซึ่งมองเห็นได้ใน service configuration ห้ามแชร์ screenshot หรือ YAML ควรใช้ Secret Manager สำหรับ Production

## 5. ให้สิทธิ์ Firestore

```bash
RUN_SA=$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(spec.template.spec.serviceAccountName)")

if [ -z "$RUN_SA" ]; then
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
    --format="value(projectNumber)")
  RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/datastore.user"
```

## 6. ตรวจค่าที่ deploy

คำสั่งนี้ไม่แสดง App Key หรือ App Secret:

```bash
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format=json | jq '{
    revision: .status.latestReadyRevisionName,
    concurrency: .spec.template.spec.containerConcurrency,
    maxScale: .spec.template.metadata.annotations["autoscaling.knative.dev/maxScale"],
    selectedEnv: [
      .spec.template.spec.containers[0].env[]
      | select(
          .name == "WEBULL_ENV"
          or .name == "WEBULL_API_VERSION"
          or .name == "WEBULL_REGION"
          or .name == "WEBULL_ACCOUNT_ID"
        )
    ]
  }'
```

ค่าที่ต้องเห็น:

```text
concurrency: 1
maxScale: 1
WEBULL_ENV: uat
WEBULL_API_VERSION: v3
WEBULL_REGION: th
```

## 7. ทดสอบ `/health`

```bash
SERVICE_URL=$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)")

curl -sS \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$SERVICE_URL/health"
```

ต้องได้ `"status":"HEALTHY"`

หากเห็น `Regional Access Boundary ... Gaia id not found` ให้ตรวจ:

```bash
gcloud auth list
gcloud config get-value account
```

คำเตือนนี้เป็นเรื่องบัญชี Google Cloud Shell ไม่ใช่ Webull หาก `/health` ตอบกลับได้ตามปกติสามารถตรวจส่วนอื่นต่อได้

> อย่าเปลี่ยน `/health` เป็น `/` เพื่อทดสอบ เพราะ URL หลักอาจเริ่มบอตและส่งคำสั่งซื้อ

## 8. สร้าง Scheduler เมื่อพร้อม

```bash
gcloud iam service-accounts create scheduler-invoker \
  --project="$PROJECT_ID" \
  --display-name="Scheduler Invoker"

SCHEDULER_SA="scheduler-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http shannon-demon-every-5m \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --schedule="*/5 * * * *" \
  --time-zone="Asia/Bangkok" \
  --uri="$SERVICE_URL" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="$SERVICE_URL"
```

หยุดไว้ก่อนจนกว่าจะพร้อมเทรด:

```bash
gcloud scheduler jobs pause shannon-demon-every-5m \
  --project="$PROJECT_ID" \
  --location="$REGION"
```

เปิดกลับเมื่อพร้อม:

```bash
gcloud scheduler jobs resume shannon-demon-every-5m \
  --project="$PROJECT_ID" \
  --location="$REGION"
```

## 9. ดู Logs

```bash
gcloud run services logs read "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --limit=50
```

สถานะ `PASS_MARKET_CLOSED`, `PASS_DNA_ZERO` และ `PASS_THRESHOLD` เป็นการข้ามรอบตามเงื่อนไข ไม่ใช่ระบบพัง

## เปลี่ยนเป็น Production

ใช้ Production credentials เท่านั้น:

```bash
gcloud run services update "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --update-env-vars="WEBULL_ENV=prod,WEBULL_REGION=th,WEBULL_API_VERSION=v3,WEBULL_ACCOUNT_ID=YOUR_PRODUCTION_ACCOUNT_ID,WEBULL_APP_KEY=YOUR_PRODUCTION_APP_KEY,WEBULL_APP_SECRET=YOUR_PRODUCTION_APP_SECRET"
```

Production endpoint ต้องเป็น `api.webull.co.th`

## คำเตือน

- `WEBULL_PREVIEW_ORDERS=true` คือ Preview ก่อน แล้ว `place_order` ต่อ ไม่ใช่ Preview-only
- การ Force run Scheduler หรือเรียก Service URL หลักอาจส่ง order
- เริ่มจาก UAT และ Pause Scheduler จนกว่าจะตรวจค่าครบ
- ห้าม commit credentials ลง GitHub


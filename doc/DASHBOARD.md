# Dashboard 101: อ่าน state และ trade log จาก Firestore

`dashboard.py` เป็นเว็บ Streamlit อ่านอย่างเดียว (read-only) ไม่แตะ bot หรือสั่งเทรดใด ๆ

## 1. เตรียม service account key

ต้องมี key ของ service account ที่มีสิทธิ์ `roles/datastore.viewer` (หรือมากกว่า) บน project ที่ bot เขียนข้อมูลจริง

⚠️ **ห้ามวาง private key ในแชทหรือที่สาธารณะ** ถ้า key เคยหลุดไปแล้ว (เช่น แปะในแชท) ให้ไป `IAM & Admin → Service Accounts → [ชื่อ SA] → Keys` แล้วลบ key เก่า สร้างใหม่แทนทันที

## 2. ตั้งค่า secrets ในเครื่อง

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

เปิด `.streamlit/secrets.toml` แล้ววางค่าจาก JSON key file ของ service account ลงในช่อง `[firebase_service_account]` (ไฟล์นี้อยู่ใน `.gitignore` แล้ว จะไม่ถูก push ขึ้น GitHub)

## 3. ติดตั้งและรัน

```bash
pip install -r requirements-dashboard.txt
streamlit run dashboard.py
```

เปิดเบราว์เซอร์ตาม URL ที่ terminal แสดง (ปกติ `http://localhost:8501`)

## 4. ตั้งค่าใน sidebar

| ช่อง | ค่า default | ใส่อะไร |
|---|---|---|
| Project ID | project ของ key ที่ใช้ | Project ID ที่ Firestore เก็บข้อมูลจริง (เช็คจาก `GCP_PROJECT_ID` ที่ตั้งไว้ตอน deploy bot) |
| State collection | `shannon_demon_state` | ต้องตรงกับ `FIRESTORE_STATE_COLLECTION` ของ bot |
| State document | `SHANNON_DEMON_DNA_SMR` | ต้องตรงกับ `FIRESTORE_STATE_DOCUMENT` ของ bot |
| Trade collection | `shannon_demon_trades` | ต้องตรงกับ `FIRESTORE_TRADE_COLLECTION` ของ bot |

## ⚠️ ระวังเรื่อง project ไม่ตรงกัน

service account key แต่ละตัวผูกกับ **project เดียว** (ดูได้จากฟิลด์ `project_id` ใน key) ถ้า key นี้เป็นของ project หนึ่ง แต่ bot เขียนข้อมูลอยู่อีก project หนึ่ง จะอ่านไม่ได้จนกว่าจะ:

1. ไปหน้า **IAM** ของ project ที่มีข้อมูลจริง
2. เพิ่ม service account (client_email ในไฟล์ secrets) เป็น member พร้อม role `roles/datastore.viewer`
3. ใส่ Project ID ของ project ที่มีข้อมูลจริงในช่อง "Project ID" บน sidebar (ไม่ใช่ project ของ key)

## Deploy ขึ้นเว็บ (ถ้าต้องการ)

ใช้ [Streamlit Community Cloud](https://streamlit.io/cloud) ได้ฟรี — deploy จาก repo นี้ ตั้ง main file เป็น `dashboard.py` แล้ววาง secrets เดียวกันในหน้า **App settings → Secrets** ของ Streamlit Cloud (ไม่ต้อง commit ไฟล์ secrets ลง git)

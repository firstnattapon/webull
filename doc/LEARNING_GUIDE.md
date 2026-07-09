# 📚 Learning Guide — Shannon Demon DNA Bot (v3) ฉบับง่าย

> อ่าน 5 นาที รู้หลักการทั้งหมด — ถ้าอยากลงมือ deploy เลย ข้ามไปอ่าน [QUICKSTART.md](QUICKSTART.md)

---

## Bot นี้ทำอะไร (30 วินาที)

Cloud Scheduler ยิง HTTP มาที่ Cloud Function **ทุก 5 นาที** → Bot ตอบคำถาม 3 ข้อ:

1. **รอบนี้ต้องเทรดไหม?** → ดู DNA sequence (1 = เทรด, 0 = ข้าม)
2. **ต้องซื้อหรือขาย?** → คำนวณ Shannon Demon (rebalance กลับสู่มูลค่าคงที่)
3. **ส่ง order** → ผ่าน Webull API แล้วบันทึกผลลง Firestore

---

## หลักการออกแบบ 3 ข้อ

| หลักการ | แปลว่า |
|---|---|
| **Simple** | 1 ไฟล์ = 1 หน้าที่ ไม่มีโค้ดซ้ำ |
| **Stable** | มี retry, transaction กันเทรดซ้ำ, log เขียนพลาดก็ไม่ล้ม bot |
| **Fast** | เช็คของถูกก่อนของแพง + cache ทุกอย่างข้าม warm start |

---

## ไฟล์ 7 ไฟล์ ใครทำอะไร

```
main.py          🧠 สมอง — รับ HTTP, เดินผ่าน 5 gates, มี /health endpoint
config.py        ⚙️ เสมียน — อ่าน env vars + secrets (cache ไว้, ไม่ leak secret ลง log)
broker.py        🔌 ทูต — คุยกับ Webull (retry 3 ครั้ง, ดึงราคา+position พร้อมกัน)
state.py         💾 เลขา — Firestore: จอง step แบบ transaction + เขียน trade log
strategy.py      🎯 นักวิเคราะห์ — คำนวณ BUY / SELL / PASS
dna_engine.py    🧬 นักพันธุศาสตร์ — แปลง DNA string → array ของ 0/1
market_utils.py  🕐 นาฬิกา — ตลาดสหรัฐเปิดไหม (จ-ศ 09:30-16:00 ET)
```

---

## หัวใจที่ 1: Early-Exit Chain — เช็คของถูกก่อนของแพง

ทุก request เดินผ่าน 5 ประตู เรียงจาก **ฟรี → แพง** เจอเงื่อนไขไม่ผ่านตรงไหน จบตรงนั้นทันที:

| Gate | เช็คอะไร | ต้นทุน | ถ้าไม่ผ่าน ตอบ |
|---|---|---|---|
| 1 | ถึงเวลาเริ่มยัง? | 0ms (คณิตล้วน) | `PASS_WAITING_TO_START` |
| 2 | ตลาดเปิดไหม? | 0ms (คณิตล้วน) | `PASS_MARKET_CLOSED` |
| 3 | DNA หมดหรือยัง? | ~50ms (Firestore 1 ครั้ง) | `TIMELINE_ENDED` |
| 4 | DNA รอบนี้ = 1 ไหม? | 0ms (ดู array) | `PASS_DNA_ZERO` |
| 5 | เทรด! | ~500ms (Webull API) | `OK` / `PASS_THRESHOLD` |

**ผล:** วันเสาร์-อาทิตย์ bot ตอบใน ~1ms โดยไม่แตะ network เลย

---

## หัวใจที่ 2: Shannon Demon — rebalance สู่มูลค่าคงที่

ตั้งเป้าว่าจะถือหุ้นมูลค่า `FIX_C` ดอลลาร์ตลอดเวลา (เช่น $1,500):

```
value_now = จำนวนหุ้น × ราคาล่าสุด

value_now < FIX_C - DIFF  →  BUY  (ถือน้อยไป ซื้อเพิ่ม)
value_now > FIX_C + DIFF  →  SELL (ถือเยอะไป ขายออก)
อยู่ในช่วง ±DIFF          →  PASS (ไม่ทำอะไร)

จำนวนหุ้นที่ส่ง order = |FIX_C - value_now| / ราคาล่าสุด
```

**ตัวอย่าง:** FIX_C=1500, DIFF=60, ถือ 100 หุ้น ราคา $17
→ value_now = 1700 > 1560 → **SELL** จำนวน 200/17 ≈ 11.76 หุ้น

หลักคิด: ราคาลงถูกบังคับให้ซื้อถูก ราคาขึ้นถูกบังคับให้ขายแพง — กำไรจากความผันผวน (volatility harvesting)

---

## หัวใจที่ 3: DNA — ตารางเวลาว่ารอบไหนเทรด

DNA คือ array ของ 0/1 เช่น `[1,0,1,1,0,...]` — แต่ละ trigger กิน 1 ช่อง (step):
step ชี้เลข **1** = เดินต่อไปเทรด, ชี้เลข **0** = ข้ามรอบนี้, step เดินหน้าอย่างเดียวจนหมด array

ตั้งค่าได้ 3 แบบผ่าน `DNA_CODE`:

| รูปแบบ | ตัวอย่าง | ความหมาย |
|---|---|---|
| Encoded | `26021034...` | ถอดรหัสด้วย seed+mutation ได้ pattern 0/1 (จาก backtest) |
| Bypass | `bypass:100` | เทรดทุกรอบ 100 ครั้ง |
| Array | `[1,100]` | เหมือน bypass:100 |

---

## หัวใจที่ 4: ของใหม่ใน v3 — Stable ขึ้นอีกขั้น

1. **Transaction กันเทรดซ้ำ** — เดิมถ้า 2 instance อ่าน step พร้อมกัน อาจเทรด step เดียวกัน 2 ครั้ง ตอนนี้ `reserve_step()` จอง step ใน Firestore transaction → แต่ละ instance ได้ step ไม่ซ้ำกันเสมอ
2. **`/health` endpoint** — เช็ค config ทั้งหมด + metrics โดยไม่แตะ broker (ดูวิธีใช้ใน Quick Start)
3. **Secret ไม่ leak** — credentials ถูกตัดออกจาก repr/log อัตโนมัติ
4. **Trade log ไม่ block** — เขียนบน background thread + flush ก่อนตอบ HTTP เสมอ
5. **ราคา 0 หรือติดลบ** — ตอบ 502 ทันที ไม่หลุดไปคำนวณผิด ๆ

---

## Error ตอบยังไง

| สถานการณ์ | status | HTTP |
|---|---|---|
| Webull มีปัญหา (API ล่ม, ราคาเพี้ยน) | `BROKER_ERROR` | 502 |
| Firestore / bug ภายใน | `ERROR` | 500 |
| ทุกอย่างปกติ (รวมทุก PASS) | ตาม gate | 200 |

จำง่าย ๆ: **502 = โทษคนอื่น (upstream), 500 = โทษตัวเอง**

---

## เร็วแค่ไหน

| สถานการณ์ | เวลา |
|---|---|
| Cold start + เทรด | ~1.2s |
| Warm start + เทรด (ทุก connection ถูก cache) | ~300ms |
| Early exit (ตลาดปิด / DNA=0) | ~1-50ms |

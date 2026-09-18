# Cambridge One Auto-Solver 🎓

> 🤖 เครื่องมือช่วยทำแบบฝึกหัดอัตโนมัติบน Cambridge One (Digital Workbook)  
> เขียนด้วย Python + Playwright + AsyncIO

---

## ⚠️ สถานะโปรเจกต์: กำลังพัฒนา (Work in Progress)

โปรเจกต์นี้ยังอยู่ในระหว่างการพัฒนาและปรับปรุงอย่างต่อเนื่อง  
มีบางฟีเจอร์ที่ทำงานได้ดีแล้ว และบางฟีเจอร์ที่ยังต้องแก้ไขเพิ่มเติม  
หากพบปัญหาหรืออยากเสนอแนะ สามารถเปิด Issue ได้เลยครับ

---

## 📖 เกี่ยวกับโปรแกรม

**Cambridge One Auto-Solver** เป็นบอทที่ออกแบบมาเพื่อช่วยผู้ใช้งานในการทำแบบฝึกหัดในแพลตฟอร์ม [Cambridge One](https://www.cambridgeone.org) โดยอัตโนมัติ โปรแกรมจะ:

1. **Login** เข้าสู่ระบบด้วยบัญชี Cambridge One (พร้อมระบบบันทึกรหัสผ่าน)
2. **ดึงข้อมูลหลักสูตร** (Digital Workbook) ทั้งหมดของผู้ใช้
3. **ดึงข้อมูลบทเรียน (Units)** และแบบฝึกหัด (Exercises) ในแต่ละบท
4. **ดึงเฉลย** จาก `data.js` ที่ Cambridge One ส่งมาในหน้าเว็บ
5. **เติมคำตอบอัตโนมัติ** ตามประเภทของแบบฝึกหัด:
   - Multiple Choice (Radio)
   - Multiple Choice (Checkbox)
   - Dropdown
   - Text Entry
   - Drag & Drop / Click-to-fill
6. **กด Check** และตรวจสอบผลลัพธ์
7. **ส่งงาน** และไปยังข้อถัดไปโดยอัตโนมัติ
8. **บันทึกคำตอบ** ลงฐานข้อมูล `answers_db.json` เพื่อใช้ซ้ำในครั้งต่อไป

---

## ✨ ฟีเจอร์หลัก

| ฟีเจอร์ | สถานะ |
|---|---|
| 🔐 Login + บันทึกรหัสผ่าน (hash) | ✅ ทำงานได้ |
| 📚 ดึงรายการคอร์ส / Units / Exercises | ✅ ทำงานได้ |
| 🧠 ดึงเฉลยจาก `data.js` | ✅ ทำงานได้ |
| 🎯 เติมคำตอบ Multiple Choice (Radio) | ✅ ทำงานได้ |
| ☑️ เติมคำตอบ Multiple Choice (Checkbox) | ✅ ทำงานได้ |
| 📝 เติมคำตอบ Text Entry | ✅ ทำงานได้ |
| 🔽 เติมคำตอบ Dropdown | ✅ ทำงานได้ |
| 🖱️ เติมคำตอบ Drag & Drop (Click-to-fill) | ⚠️ กำลังแก้ไข |
| 🗣️ จัดการ Speaking Activity (Skip) | ✅ ทำงานได้ |
| 💾 ฐานข้อมูลคำตอบ (Cache) | ✅ ทำงานได้ |
| 🎬 จัดการ Video True/False | ✅ ทำงานได้ |
| 🔁 โหมดทำทุกบท (Auto All Units) | ✅ ทำงานได้ |
| 📊 แสดงคะแนนหลัง Check | ✅ ทำงานได้ |
| 📝 บันทึก Log ทุก Session | ✅ ทำงานได้ |

---

## 🐛 Known Issues (ปัญหาที่กำลังแก้ไข)

### 1. ⚠️ Drag & Drop / Click-to-fill ยังทำงานไม่สมบูรณ์

**อาการ:**  
ในแบบฝึกหัดประเภท "Choose the correct answers" ที่มีคำศัพท์อยู่ด้านล่างและช่องว่างในประโยค โปรแกรมสามารถมองเห็นคำตอบที่ถูกต้องได้ แต่ **ไม่สามารถคลิกหรือลากคำศัพท์ไปยังช่องว่างได้**

**สาเหตุที่คาดว่าเป็น:**

- โครงสร้าง HTML ของ Cambridge One ใช้ JavaScript Library (คาดว่าเป็น jQuery UI Draggable/Droppable)
- ปุ่ม `<button>` ใน HTML เป็นเพียง Accessibility element ไม่ได้ผูก Event ไว้
- ตัวที่ทำให้เกิดการลากจริงคือ `div.drag_element` ที่ต้องใช้ `mousedown` → `mousemove` → `mouseup`
- การใช้ `element.click()` หรือ JavaScript DragEvent ไม่เพียงพอต่อการ Trigger Library

**สิ่งที่กำลังทำ:**

- ตรวจสอบ Network Request ขณะลากด้วยมือ (ดูว่ามี AJAX POST หรือไม่)
- วิเคราะห์โครงสร้าง DOM ของ target หลัง drop ว่ามีการย้าย element อย่างไร
- ปรับใช้ `page.mouse.down()` + `page.mouse.move()` + `page.mouse.up()` ที่พิกัดจริง

**สถานะ:** 🔴 ยังไม่แก้ได้ — ต้องการข้อมูลเพิ่มเติมจากผู้ใช้

---

### 2. ⚠️ Multi-Page MC บางครั้งตรวจสอบหน้าถัดไปไม่เจอ

**อาการ:**  
ในแบบฝึกหัดที่มีหลายหน้า (Multi-Page) บางครั้งโปรแกรมกด Check แล้วรอหน้าเปลี่ยน แต่หน้าไม่เปลี่ยน หรือตรวจสอบไม่เจอว่าหน้าเปลี่ยนแล้ว

**สิ่งที่กำลังทำ:**

- ปรับ `wait_for_next_question()` ให้ตรวจสอบหลายเงื่อนไข
- เพิ่ม fallback ในการตรวจสอบ URL change

**สถานะ:** 🟡 แก้ไขบางส่วนแล้ว — ยังต้องทดสอบเพิ่ม

---

### 3. ⚠️ Dropdown บางครั้งหาช่องไม่เจอ

**อาการ:**  
ในแบบฝึกหัดที่มี Dropdown หลายช่อง บางครั้งโปรแกรมหา `wrapper` ไม่เจอ หรือเลือกผิดช่อง

**สิ่งที่กำลังทำ:**

- ปรับ logic การหา `response_id` และ fallback ให้แม่นยำขึ้น

**สถานะ:** 🟡 แก้ไขบางส่วนแล้ว

---

## 🛠️ การติดตั้ง

### ความต้องการของระบบ

- **Python 3.9+**
- **Google Chrome** (สำหรับ Playwright)
- ระบบปฏิบัติการ: Windows / macOS / Linux

### ขั้นตอนการติดตั้ง

```bash
# 1. Clone repository
git clone https://github.com/Zentrary/cambridge-one-auto-solver.git
cd cambridge-one-auto-solver

# 2. สร้าง Virtual Environment (แนะนำ)
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

# 3. ติดตั้ง Dependencies
pip install -r requirements.txt

# 4. ติดตั้ง Playwright Browsers
playwright install chromium
```

### ไฟล์ `requirements.txt`

```text
playwright>=1.40.0
requests>=2.31.0
colorama>=0.4.6
pyttsx3>=2.90
```

---

## 🚀 วิธีใช้งาน

```bash
python main.py
```

จากนั้นทำตามขั้นตอนในโปรแกรม:

1. เลือกบัญชีที่ต้องการใช้ (หรือเพิ่มบัญชีใหม่)
2. รอโปรแกรม Login อัตโนมัติ (ถ้ามี session เดิมจะข้ามขั้นตอนนี้)
3. เลือกคอร์สที่ต้องการ
4. เลือกบทเรียน (Unit)
5. เลือกโหมดการทำงาน:
   - `[1]` แสดงเฉลย
   - `[2]` ทำแบบฝึกหัดอัตโนมัติ
   - `[3]` เลือกทำเฉพาะข้อ
   - `[4]` ดูฐานข้อมูลคำตอบ

---

## 📁 โครงสร้างโปรเจกต์

```text
cambridge-one-auto-solver/
├── main.py                    # ไฟล์หลักของโปรแกรม
├── extractor.py               # โมดูลดึงข้อมูล
├── requirements.txt           # Dependencies
├── README.md                  # ไฟล์นี้
├── .gitignore                 # ไฟล์ที่ไม่ต้อง commit
├── answers_db.json            # ฐานข้อมูลคำตอบ (สร้างอัตโนมัติ)
├── cambridge_accounts.json    # บัญชีที่บันทึกไว้ (สร้างอัตโนมัติ)
├── cambridge_credentials.json # Credentials (สร้างอัตโนมัติ)
├── logs/                      # Log ทุก Session (สร้างอัตโนมัติ)
│   └── session_YYYYMMDD_HHMMSS.log
└── chrome_data/               # Chrome Profile แยกตามบัญชี (สร้างอัตโนมัติ)
    └── <hash_of_email>/
```

---

## 🔒 ความปลอดภัย

- รหัสผ่านถูก Hash ด้วย **SHA-256** ก่อนบันทึก (ไม่ได้เก็บ plain text)
- Session ถูกเก็บใน **Chrome Profile แยก** ตามบัญชี
- ไม่มีการส่งข้อมูลออกไปยังเซิร์ฟเวอร์ภายนอก
- ทุกอย่างทำงานบนเครื่องของคุณเอง

---

## 🗺️ Roadmap

- [x] Login + Session management
- [x] ดึงเฉลยจาก `data.js`
- [x] เติมคำตอบ MC / Checkbox / Text Entry / Dropdown
- [ ] **แก้ไข Drag & Drop / Click-to-fill** ⬅️ กำลังทำ
- [ ] ปรับปรุง Multi-Page MC
- [ ] รองรับแบบฝึกหัดประเภท Listening
- [ ] GUI (Tkinter / PyQt)
- [ ] Export ผลลัพธ์เป็นรายงาน PDF

---

## 🤝 การมีส่วนร่วม

หากคุณพบปัญหาหรืออยากช่วยแก้ไข:

1. Fork repository
2. สร้าง Branch ใหม่ (`git checkout -b feature/fix-drag-drop`)
3. Commit การเปลี่ยนแปลง
4. Push ขึ้น Branch
5. เปิด Pull Request

---

## 📞 ติดต่อ / รายงานปัญหา

หากพบปัญหาที่ไม่ใช่ Known Issues หรืออยากช่วยให้ข้อมูลเพิ่มเติม สามารถเปิด [Issue](https://github.com/Zentrary/cambridge-one-auto-solver/issues) ได้เลยครับ

โดยเฉพาะปัญหา **Drag & Drop** ผมต้องการข้อมูลเหล่านี้:

- HTML ของ `div.drop_area.gap_match_gap_view` ตอนว่าง และตอนมีคำตอบ
- Network Request ขณะลากด้วยมือ
- Console Log ขณะลากด้วยมือ

---

## 📜 License

MIT License — ใช้งานได้อย่างอิสระ แต่ผู้ใช้ต้องรับผิดชอบความเสี่ยงเอง

---

<div align="center">

**Made with ❤️ by [z3nTr4ry](https://github.com/Zentrary)**

⭐ ถ้าชอบโปรเจกต์นี้ อย่าลืมกด Star ให้ด้วยนะครับ ⭐

</div>

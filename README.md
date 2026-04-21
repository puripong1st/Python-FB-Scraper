# 📘 Facebook News Scraper & Discord Notifier

โปรแกรม Desktop Automation สำหรับติดตามโพสต์ Facebook ตาม Keywords
และส่งแจ้งเตือนเข้า Discord แบบ Real-time

---

## 🔧 Requirements

- Python 3.10 หรือใหม่กว่า
- Google Chrome (เวอร์ชันล่าสุด)
- ChromeDriver จะถูกดาวน์โหลดอัตโนมัติโดย `undetected-chromedriver`

---

## 📦 การติดตั้ง

1. **Clone หรือ Download โปรเจกต์นี้**

2. **สร้าง Virtual Environment (แนะนำ)**
   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # macOS/Linux
   source venv/bin/activate
   ```

3. **ติดตั้ง Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

---

## ▶️ วิธีรันโปรแกรม

```bash
python main.py
```

---

## 📋 คู่มือใช้งาน GUI

### Section 1 — Facebook Credentials
- กรอก **Email** และ **Password** ของบัญชี Facebook ที่ต้องการใช้ Scrape
- รหัสผ่านจะถูกปิดบังด้วย ●●● เพื่อความปลอดภัย

### Section 2 — Target Settings
- **URL เพจเป้าหมาย**: กรอก Facebook Page URL แต่ละบรรทัด = 1 เพจ
  ```
  https://www.facebook.com/BBCnewsThai
  https://www.facebook.com/voathai
  ```
- **Keywords**: คั่นด้วยลูกน้ำ `,` หากเว้นว่างจะดึงทุกโพสต์
  ```
  ข่าว, ด่วน, สำคัญ, เตือน
  ```

### Section 3 — Timeframe & Loop
- **ดึงโพสต์ย้อนหลัง**: จำนวนชั่วโมง (เช่น `24` = ดึงโพสต์ย้อนหลัง 24 ชม.)
- **วนลูปทุก**: ระยะเวลาระหว่าง Cycle เป็นนาที (เช่น `30` = ทำซ้ำทุก 30 นาที)

### Section 4 — Discord Webhook
- วาง **Webhook URL** จาก Discord Server ของคุณ
  ```
  https://discord.com/api/webhooks/XXXXXXXX/YYYYYYYY
  ```

### Section 5 — Controls
| ปุ่ม | หน้าที่ |
|------|---------|
| ▶ Start | เริ่มทำงาน |
| ⏹ Stop | หยุดทำงาน |
| ▶▶ Resume | กดหลังจากแก้ Checkpoint/2FA/CAPTCHA บน Browser แล้ว |

---

## 🚨 การจัดการ Checkpoint / 2FA / CAPTCHA

เมื่อ Facebook บล็อก Bot:
1. โปรแกรมจะ **หยุดทำงานอัตโนมัติ** และส่งแจ้งเตือนเข้า Discord
2. **เปิด Browser** ที่โปรแกรมเปิดค้างไว้ แล้วแก้ไข Checkpoint/2FA ด้วยตัวเอง
3. กลับมาที่หน้าโปรแกรมแล้วกดปุ่ม **Resume**
4. โปรแกรมจะกลับมาทำงานต่อจากจุดที่หยุด

---

## 🗄️ ไฟล์ที่โปรแกรมสร้าง

| ไฟล์ | คำอธิบาย |
|------|---------|
| `scraper_data.db` | SQLite database เก็บ Post ID ที่ส่งไปแล้ว (ป้องกันซ้ำ) |
| `fb_cookies.pkl` | Session cookies สำหรับล็อกอินโดยไม่ต้องกรอกรหัสใหม่ |

---

## ⚠️ คำเตือนและข้อควรระวัง

- **ใช้ในขอบเขตของกฎหมายและ Terms of Service ของ Facebook**
- แนะนำให้ใช้บัญชีที่สร้างขึ้นมาเพื่องานนี้โดยเฉพาะ ไม่ใช่บัญชีส่วนตัว
- Facebook อาจตรวจจับและบล็อก Bot ได้เสมอ ให้ตั้ง Loop interval อย่างน้อย 15-30 นาที
- `undetected-chromedriver` ลดความเสี่ยงถูก detect แต่ไม่ได้ป้องกัน 100%

---

## 🏗️ โครงสร้างโค้ด

```
main.py
├── DatabaseManager     — จัดการ SQLite (seen_posts table)
├── DiscordNotifier     — ส่ง Webhook notifications
├── FacebookScraper     — Selenium automation + scraping logic
└── ScraperApp          — CustomTkinter GUI + threading
```

---

## 📡 รูปแบบการแจ้งเตือน Discord

| Event | ข้อความ |
|-------|---------|
| เริ่มทำงาน | 🟢 เริ่มระบบ Scraper + เวลา |
| พบโพสต์ใหม่ | Rich Embed พร้อมลิงก์ + เนื้อหา |
| จบรอบ | ✅ สรุปรอบ + เวลาที่ใช้ + รอบถัดไปใน X นาที |
| ติด Obstacle | 🚨 @everyone แจ้งเตือน + ประเภทอุปสรรค |
| หยุดทำงาน | 🔴 แจ้งว่าหยุดแล้ว |

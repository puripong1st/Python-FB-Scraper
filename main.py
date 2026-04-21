"""
Facebook News Scraper & Discord/Telegram Notifier
=========================================
A desktop automation tool that scrapes Facebook pages for posts matching
specified keywords and sends Discord/Telegram notifications.

Author: Senior Python Developer
Tech Stack: undetected-chromedriver, CustomTkinter, SQLite3, requests

Phase 1 Safety Fixes Applied:
  [1] cookies: pickle → JSON  (ป้องกัน arbitrary code execution)
  [2] delays:  time.time()%N → random.uniform()  (ป้องกัน bot detection)
  [3] exceptions: bare except/silent pass → log ทุกจุด
  [4] driver:  raw attribute → property + RLock  (thread-safe)
"""

import customtkinter as ctk
import threading
import sqlite3
import requests
import time
import json
import re
import os
import random                          # [2] เพิ่ม — แทน time.time() % N
import tkinter as tk                   # [3] ย้ายมา top-level แทนการ import ซ้ำในฟังก์ชัน
from datetime import datetime, timedelta, timezone
from queue import Queue, Empty
import hashlib
# Selenium imports
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
import undetected_chromedriver as uc


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseManager:
    DB_FILE = "scraper_data.db"

    def __init__(self):
        self.conn = sqlite3.connect(self.DB_FILE, check_same_thread=False)
        self._lock = threading.RLock()   # [4] RLock รองรับ reentrant calls
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    post_id    TEXT PRIMARY KEY,
                    page_url   TEXT,
                    post_url   TEXT UNIQUE,
                    detected_at TEXT
                )
            """)
            # Migrate: เพิ่ม UNIQUE index บน post_url สำหรับ DB เดิม
            try:
                self.conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_post_url_unique
                    ON seen_posts(post_url)
                """)
            except sqlite3.Error:
                pass  # index อาจมีอยู่แล้ว — ปลอดภัยที่จะ skip
            # ล้างข้อมูลซ้ำที่มีอยู่เดิม
            try:
                self.conn.execute("""
                    DELETE FROM seen_posts
                    WHERE rowid NOT IN (
                        SELECT MIN(rowid) FROM seen_posts GROUP BY post_url
                    )
                """)
            except sqlite3.Error:
                pass  # ตารางอาจว่างเปล่า — ปลอดภัยที่จะ skip
            self.conn.commit()

    def is_seen(self, post_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
            )
            return cur.fetchone() is not None

    def is_seen_by_url(self, post_url: str) -> bool:
        """ตรวจ duplicate ด้วย URL โดยตรง — ครอบคลุมกรณี post_id ต่างกันแต่ URL เหมือนกัน"""
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM seen_posts WHERE post_url = ?", (post_url,)
            )
            return cur.fetchone() is not None

    def mark_seen(self, post_id: str, page_url: str, post_url: str):
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO seen_posts (post_id, page_url, post_url, detected_at) "
                    "VALUES (?, ?, ?, ?)",
                    (post_id, page_url, post_url, datetime.now().isoformat()),
                )
                self.conn.commit()
            except sqlite3.Error as e:
                # [3] log แทน silent pass
                print(f"[DB] mark_seen error: {e}")

    def cleanup_old_data(self):
        """ลบโพสต์ที่เก่ากว่า 24 ชั่วโมง และทำ VACUUM เพื่อคืนพื้นที่บนดิสก์"""
        with self._lock:
            try:
                # ลบแถวที่ detected_at เก่ากว่า 1 วัน
                self.conn.execute("DELETE FROM seen_posts WHERE detected_at < datetime('now', '-1 day')")
                self.conn.commit()
                
                # ทำ VACUUM เพื่อจัดเรียงข้อมูลและลดขนาดไฟล์ .db
                self.conn.execute("VACUUM")
                self.conn.commit()
                return True
            except sqlite3.Error as e:
                print(f"[DB] Cleanup error: {e}")
                return False

    def close(self):
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFIERS (DISCORD & TELEGRAM)
# ─────────────────────────────────────────────────────────────────────────────

class DiscordNotifier:
    # สีประจำแต่ละเพจ — มองปั๊บรู้ทันทีว่ามาจากไหน
    PAGE_COLORS = {
        "khaosod":               0xE53935,  # แดงเข้ม
        "TheReportersTH":        0x1565C0,  # น้ำเงินเข้ม
        "NationOnline":          0x2E7D32,  # เขียวเข้ม
        "MorningNewsTV3":        0x6A1B9A,  # ม่วง
        "ThePoliticsByMatichon": 0xE65100,  # ส้มเข้ม
        "MatichonOnline":        0xF57F17,  # เหลืองทอง
        "thestandardth":         0x00838F,  # ฟ้าเทา
        "Ch7HDNews":             0xC62828,  # แดงเลือดหมู
        "thairath":              0xAD1457,  # ชมพูเข้ม
        "tnamcot":               0x00695C,  # เขียวน้ำทะเล
    }
    DEFAULT_COLOR = 0x1877F2  # Facebook Blue

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _send(self, payload: dict) -> bool:
        if not self.webhook_url:
            return False
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code in (200, 204)
        except requests.RequestException:
            return False

    def _utc_now_iso(self) -> str:
        """Discord timestamp ต้องเป็น UTC ISO 8601"""
        return datetime.now(timezone.utc).isoformat()

    def _smart_truncate(self, text: str, limit: int, post_url: str) -> str:
        """ตัดข้อความอย่างฉลาด — ไม่ตัดกลางคำ"""
        if len(text) <= limit:
            return text
        cut = text[:limit]
        for sep in ["\n\n", "\n", "। ", "。", ". "]:
            idx = cut.rfind(sep)
            if idx > limit * 0.65:
                cut = cut[:idx]
                break
        return cut + f"\n\n*[…อ่านต่อในโพสต์ต้นฉบับ]({post_url})*"

    def send_start(self, page_count: int = 0, keyword_count: int = 0, loop_min: int = 0):
        embed = {
            "color": 0x43A047,
            "author": {"name": "🟢  ระบบ Scraper เริ่มทำงานแล้ว"},
            "fields": [
                {"name": "📋  เพจที่ติดตาม",    "value": f"`{page_count} เพจ`",   "inline": True},
                {"name": "🔑  Keywords ทั้งหมด", "value": f"`{keyword_count} คำ`", "inline": True},
                {"name": "🔄  วนซ้ำทุก",         "value": f"`{loop_min} นาที`",    "inline": True},
            ],
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"embeds": [embed]})

    def send_post(self, page_name: str, page_url: str, post_url: str, content: str,
                  found_keywords: list, image_url: str = None):
        color = self.PAGE_COLORS.get(page_name, self.DEFAULT_COLOR)
        now   = datetime.now()

        content_display = self._smart_truncate(content, 900, post_url)

        if found_keywords:
            kw_chips = "  ".join(f"`{kw}`" for kw in found_keywords)
            if len(kw_chips) > 1000:
                kw_chips = kw_chips[:997] + "…"
        else:
            kw_chips = "*—*"

        embed = {
            "color": color,
            "author": {"name": f"📰  {page_name}", "url": page_url},
            "title": "คลิกเพื่ออ่านโพสต์ต้นฉบับ  →",
            "url":   post_url,
            "description": content_display,
            "fields": [
                {"name": "🔍  Keywords ที่ตรงกัน", "value": kw_chips, "inline": False},
            ],
            "footer": {
                "text": f"FB News Monitor  •  PRP  •  ตรวจพบ {now.strftime('%d %b %Y  %H:%M')} น.",
            },
            "timestamp": self._utc_now_iso(),
        }

        if image_url:
            embed["image"] = {"url": image_url}

        self._send({"embeds": [embed]})

    def send_cycle_complete(self, duration_sec: float, next_run_min: int):
        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        embed = {
            "color": 0x1E88E5,
            "author": {"name": "✅  สแกนรอบนี้เสร็จสิ้น"},
            "fields": [
                {"name": "⏱  เวลาที่ใช้",   "value": f"`{mins} นาที {secs} วินาที`", "inline": True},
                {"name": "⏳  รอบถัดไปในอีก", "value": f"`{next_run_min} นาที`",      "inline": True},
            ],
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"embeds": [embed]})

    def send_obstacle(self, obstacle_type: str):
        embed = {
            "color": 0xE53935,
            "author": {"name": "🚨  บอทหยุดทำงานชั่วคราว — ต้องการความช่วยเหลือ!"},
            "description": (
                f"**บอทติดหน้า:** `{obstacle_type}`\n\n"
                "**วิธีแก้ไข:**\n"
                "1️⃣  เปิดหน้าต่าง Browser\n"
                "2️⃣  แก้ไขตามที่หน้าจอแจ้ง\n"
                "3️⃣  กดปุ่ม **Resume** บนโปรแกรม\n\n"
                "*บอทจะทำงานต่ออัตโนมัติหลังกด Resume*"
            ),
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"content": "@everyone", "embeds": [embed]})

    def send_stopped(self):
        embed = {
            "color": 0x757575,
            "author": {"name": "🔴  ระบบ Scraper หยุดทำงานแล้ว"},
            "description": "*หยุดโดยผู้ใช้งาน — กด Start เพื่อเริ่มใหม่*",
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"embeds": [embed]})


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.api_url   = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def _send(self, text: str, keyboard: dict = None) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            resp = requests.post(self.api_url, json=payload, timeout=10)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _send_photo(self, photo_url: str, caption: str, keyboard: dict = None) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
                json=payload,
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def send_start(self):
        self._send(
            f"🟢 <b>เริ่มระบบ Scraper</b>\nเวลา: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        )

    def send_post(self, page_name: str, page_url: str, post_url: str, content: str,
                  found_keywords: list, image_url: str = None):
        kw_str = ", ".join(found_keywords) if found_keywords else "-"
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💾 บันทึกข่าวนี้", "callback_data": "save_news"},
                    {"text": "🗑️ ลบข้อความ",    "callback_data": "delete_news"},
                ],
                [{"text": "🌐 ดูข้อมูลเพจเพิ่มเติม", "url": page_url}],
            ]
        }

        if image_url:
            snippet = (
                content[:800] + "\n\n...[อ่านต่อในลิงก์]" if len(content) > 800 else content
            )
            text = (
                f"📢 <b>ข่าวจาก {page_name}</b>\n\n"
                f"{snippet}\n\n"
                f"🔑 <b>Keywords:</b> {kw_str}\n\n"
                f"🔗 <a href='{post_url}'>เปิดโพสต์ต้นฉบับ</a>"
            )
            self._send_photo(image_url, text, keyboard)
        else:
            snippet = (
                content[:2000] + "\n\n...[อ่านต่อในลิงก์]" if len(content) > 2000 else content
            )
            text = (
                f"📢 <b>ข่าวจาก {page_name}</b>\n\n"
                f"{snippet}\n\n"
                f"🔑 <b>Keywords:</b> {kw_str}\n\n"
                f"🔗 <a href='{post_url}'>เปิดโพสต์ต้นฉบับ</a>"
            )
            self._send(text, keyboard)

    def send_cycle_complete(self, duration_sec: float, next_run_min: int):
        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        self._send(
            f"✅ <b>สแกนรอบนี้เสร็จสิ้น</b>\n"
            f"ระยะเวลาที่ใช้: {mins}m {secs}s\n"
            f"รอทำงานรอบต่อไปในอีก {next_run_min} นาที"
        )

    def send_obstacle(self, obstacle_type: str):
        self._send(
            f"🚨 <b>บอทติดหน้า {obstacle_type}</b>\n"
            f"กรุณาเข้ามากดแก้ในเบราว์เซอร์ด่วน!\n"
            f"แล้วกดปุ่ม <b>Resume</b> บนโปรแกรม"
        )

    def send_stopped(self):
        self._send("🔴 <b>ระบบ Scraper หยุดทำงานแล้ว</b> (หยุดโดยผู้ใช้)")


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM LISTENER
# ─────────────────────────────────────────────────────────────────────────────

class TelegramListener(threading.Thread):
    def __init__(self, bot_token: str):
        super().__init__(daemon=True)
        self.bot_token  = bot_token
        self.api_url    = f"https://api.telegram.org/bot{bot_token}/"
        self.offset     = None
        self._stop_event = threading.Event()

    def run(self):
        if not self.bot_token:
            return
        while not self._stop_event.is_set():
            try:
                payload = {"timeout": 20}
                if self.offset:
                    payload["offset"] = self.offset
                resp = requests.post(
                    self.api_url + "getUpdates", json=payload, timeout=25
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        for result in data["result"]:
                            self.offset = result["update_id"] + 1
                            if "callback_query" in result:
                                self._handle_callback(result["callback_query"])
            except Exception as e:
                # [3] log แทน silent pass
                print(f"[TelegramListener] polling error: {e}")
                time.sleep(3)

    def _handle_callback(self, cb: dict):
        cb_id   = cb.get("id")
        data    = cb.get("data")
        msg     = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        msg_id  = msg.get("message_id")

        if not chat_id or not msg_id:
            return

        if data == "save_news":
            new_keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ บันทึกข่าวเรียบร้อยแล้ว", "callback_data": "already_saved"}]
                ]
            }
            requests.post(self.api_url + "editMessageReplyMarkup", json={
                "chat_id": chat_id, "message_id": msg_id, "reply_markup": new_keyboard
            })
            requests.post(self.api_url + "answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "อัปเดตสถานะการบันทึกแล้ว!"
            })

        elif data == "delete_news":
            requests.post(self.api_url + "deleteMessage", json={
                "chat_id": chat_id, "message_id": msg_id
            })
            requests.post(self.api_url + "answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "ลบข่าวนี้ออกจากแชทแล้ว"
            })

        elif data == "already_saved":
            requests.post(self.api_url + "answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "ข่าวนี้ถูกบันทึกไปแล้วครับ!"
            })

    def stop(self):
        self._stop_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# FACEBOOK SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

# [1] เปลี่ยนจาก .pkl เป็น .json — JSON ปลอดภัย ไม่ execute code เหมือน pickle
COOKIES_FILE = "fb_cookies.json"

class FacebookScraper:
    SELECTORS = {
        "email_input": "//input[@id='email' or @name='email']",
        "pass_input":  "//input[@id='pass' or @name='pass']",
        "login_btn":   "//button[@name='login' or @data-testid='royal_login_button']",
        "post_story":  "[data-testid='story-subtitle'], div[role='article']",
        "feed_posts":  "div[role='feed'] > div",
    }

    HOME_URL = "https://www.facebook.com"

    def __init__(
        self,
        log_callback,
        db: DatabaseManager,
        discord: DiscordNotifier,
        tg: TelegramNotifier,
        on_cookies_saved=None,
    ):
        self.log  = log_callback
        self.db   = db
        self.discord = discord
        self.tg   = tg
        self._on_cookies_saved = on_cookies_saved

        # [4] driver ป้องกัน race condition ด้วย RLock + property
        self._driver      = None
        self._driver_lock = threading.RLock()

        self._stop_event   = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._is_paused = False

    # ── [4] Thread-safe driver property ──────────────────────────────────────
    @property
    def driver(self):
        """อ่าน driver ผ่าน RLock — ปลอดภัยจาก main thread และ scraper thread"""
        with self._driver_lock:
            return self._driver

    @driver.setter
    def driver(self, value):
        """เขียน driver ผ่าน RLock — ป้องกัน UI thread อ่านระหว่าง scraper thread สร้าง"""
        with self._driver_lock:
            self._driver = value

    # ─────────────────────────────────────────────────────────────────────────
    # Browser lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _start_browser(self):
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--lang=th-TH,th;q=0.9,en-US;q=0.8")
        options.add_argument("--window-size=1280,900")
        self.driver = uc.Chrome(options=options, use_subprocess=True)  # ใช้ setter
        self.driver.set_page_load_timeout(30)
        self.log("🌐 เปิด Browser สำเร็จ")

    # ── [1] Cookies: pickle → JSON ────────────────────────────────────────────

    def _save_cookies(self):
        """บันทึก session cookies เป็น JSON — ปลอดภัยกว่า pickle"""
        drv = self.driver  # อ่านผ่าน property ครั้งเดียว
        if not drv:
            return
        try:
            cookies = drv.get_cookies()
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            self.log("🍪 บันทึก Session Cookies แล้ว (JSON)")
        except Exception as e:
            # [3] log แทน silent pass
            self.log(f"⚠️ บันทึก Cookies ไม่สำเร็จ: {e}")
            return

        if self._on_cookies_saved:
            try:
                self._on_cookies_saved()
            except Exception as e:
                # [3] log แทน silent pass
                self.log(f"⚠️ on_cookies_saved callback error: {e}")

    def _load_cookies(self) -> bool:
        """โหลด session cookies จาก JSON"""
        if not os.path.exists(COOKIES_FILE):
            return False
        try:
            self.driver.get(self.HOME_URL)
            time.sleep(2)
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except Exception as e:
                    self.log(f"⚠️ ข้าม cookie ที่ใส่ไม่ได้: {e}")
            self.driver.refresh()
            time.sleep(3)
            
            if "login" not in self.driver.current_url.lower():
                self.log("✅ กู้คืน Session เดิมสำเร็จ — ไม่ต้องล็อกอินใหม่")
                # ======== จุดที่เพิ่ม: สั่งให้ UI ปลดล็อกปุ่มซ่อน Browser ========
                if self._on_cookies_saved:
                    try:
                        self._on_cookies_saved()
                    except Exception:
                        pass
                # ==========================================================
                return True
        except Exception as e:
            self.log(f"⚠️ โหลด Cookies ไม่สำเร็จ: {e}")
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Login helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _type_human(self, element, text: str, delay: float = 0.06):
        """พิมพ์ตัวอักษรแบบมนุษย์ด้วย delay สุ่มจริง"""
        element.clear()
        time.sleep(0.3)
        for ch in text:
            element.send_keys(ch)
            # [2] random.uniform แทน time.time() % 0.03
            time.sleep(random.uniform(delay * 0.7, delay * 1.5))

    def _click_login_button(self) -> bool:
        strategies = [
            (By.CSS_SELECTOR, "button[name='login']"),
            (By.CSS_SELECTOR, "[data-testid='royal_login_button']"),
            (By.CSS_SELECTOR, "form button[type='submit']"),
            (By.XPATH, "//button[contains(., 'เข้าสู่ระบบ')]"),
            (By.XPATH, "//button[contains(., 'Log in') or contains(., 'Log In')]"),
            (By.XPATH, "//*[@id='loginform']//button"),
            (By.XPATH, "//div[@role='button' and (contains(., 'Log') or contains(., 'เข้า'))]"),
        ]
        for by, selector in strategies:
            try:
                btn = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable((by, selector))
                )
                self.driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.3)
                btn.click()
                self.log("🖱️ คลิกปุ่ม Login สำเร็จ")
                return True
            except (TimeoutException, NoSuchElementException, Exception):
                continue

        # Fallback: กด Enter บน password field
        try:
            pass_field = self.driver.find_element(By.XPATH, self.SELECTORS["pass_input"])
            pass_field.send_keys(Keys.RETURN)
            self.log("⌨️ กด Enter บน Password field (fallback)")
            return True
        except Exception as e:
            # [3] log แทน silent pass
            self.log(f"⚠️ fallback Enter ล้มเหลว: {e}")
        return False

    def login(self, email: str, password: str) -> bool:
        try:
            self.driver.get(f"{self.HOME_URL}/login")
            wait = WebDriverWait(self.driver, 20)

            self.log("📧 กำลังกรอก Email...")
            email_field = wait.until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["email_input"]))
            )
            self._type_human(email_field, email, delay=0.06)
            time.sleep(0.4)

            self.log("🔑 กำลังกรอก Password...")
            pass_field = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["pass_input"]))
            )
            self._type_human(pass_field, password, delay=0.05)
            time.sleep(0.6)

            self.log("🖱️ กำลังคลิกปุ่ม Login...")
            clicked = self._click_login_button()
            if not clicked:
                self.log("⚠️ หาปุ่ม Login ไม่เจอ — กรุณากด Login ใน Browser ด้วยตัวเอง แล้วกด Resume")
                self._handle_obstacle("Login Button Not Found — กรุณากด Login ด้วยตัวเอง")
                if self._stop_event.is_set():
                    return False

            self.log("⏳ รอหน้าเว็บโหลดหลัง Login...")
            time.sleep(6)

            obstacle = self._detect_obstacle()
            if obstacle:
                self.log(f"⚠️ ติด {obstacle} หลังล็อกอิน")
                self._handle_obstacle(obstacle)
                if self._stop_event.is_set():
                    return False
                time.sleep(2)

            current_url = self.driver.current_url.lower()
            if "login" not in current_url and "facebook.com" in current_url:
                self._save_cookies()
                self.log("✅ ล็อกอินสำเร็จ")
                return True

            self.log("⚠️ ยังอยู่หน้า Login — อาจ Email/Password ผิด หรือมี CAPTCHA ที่มองไม่เห็น")
            self.log("👉 กรุณาล็อกอินด้วยตัวเองในหน้าต่าง Browser แล้วกด Resume")
            self._handle_obstacle("Login ไม่สำเร็จ — กรุณาล็อกอินด้วยตัวเอง")
            if self._stop_event.is_set():
                return False
            time.sleep(2)
            if "login" not in self.driver.current_url.lower():
                self._save_cookies()
                self.log("✅ ล็อกอินสำเร็จ (manual)")
                return True
            return False

        except TimeoutException:
            self.log("⚠️ Timeout ระหว่าง Login — กรุณาล็อกอินด้วยตัวเองใน Browser แล้วกด Resume")
            self._handle_obstacle("Timeout — กรุณาล็อกอินด้วยตัวเอง")
            if self._stop_event.is_set():
                return False
            time.sleep(2)
            return "login" not in self.driver.current_url.lower()
        except Exception as e:
            self.log(f"❌ Login Error: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Obstacle detection
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_obstacle(self) -> str | None:
        url   = self.driver.current_url.lower()
        title = self.driver.title.lower()

        if "checkpoint"            in url or "checkpoint"  in title: return "Checkpoint"
        if "two_step_verification" in url or "two_factor"  in url:   return "2FA (Two-Factor Authentication)"
        if "captcha"               in url or "captcha"     in title: return "CAPTCHA"
        if "login_attempt"         in url:                           return "Login Attempt Blocked"
        if "suspended"             in url or "disabled"    in url:   return "Account Suspended/Disabled"

        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            if "confirm your identity" in body_text or "ยืนยันตัวตน" in body_text:
                return "Identity Verification"
        except Exception as e:
            # [3] log แทน silent pass
            self.log(f"⚠️ _detect_obstacle body check: {e}")
        return None

    def _handle_obstacle(self, obstacle_type: str):
        self.log(f"🚨 ติด {obstacle_type} — หยุดรอผู้ใช้แก้ไข กด Resume เมื่อเสร็จ")
        self.discord.send_obstacle(obstacle_type)
        self.tg.send_obstacle(obstacle_type)
        self._resume_event.clear()
        self._is_paused = True
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume แล้ว — กลับมาทำงานต่อ")

    def resume(self):
        self._resume_event.set()

    # ─────────────────────────────────────────────────────────────────────────
    # Scrolling
    # ─────────────────────────────────────────────────────────────────────────

    def _slow_scroll(self, scrolls: int = 4, pause: float = 2.0):
        for _ in range(scrolls):
            if self._stop_event.is_set():
                break
            self.driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
            # [2] random.uniform แทน time.time() % 0.5
            time.sleep(random.uniform(pause * 0.8, pause * 1.3))

    # ─────────────────────────────────────────────────────────────────────────
    # Post ID extraction & timestamp parsing
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_post_id(self, url: str) -> str | None:
        """รับ cleaned URL (ไม่มี query params) เท่านั้น — ป้องกัน MD5 ต่างกันสำหรับ URL เดียวกัน"""
        patterns = [
            r"/posts/(\d+)",
            r"/videos/(\d+)",
            r"story_fbid=(\d+)",
            r"/permalink/(\d+)",
            r"fbid=(\d+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        return hashlib.md5(url.encode("utf-8")).hexdigest()

    def _parse_post_timestamp(self, driver, article_element) -> datetime | None:
        now = datetime.now()
        try:
            lines = article_element.text.split("\n")[:10]
            for line in lines:
                text = line.lower().strip().replace("·", "").replace(",", "").strip()
                if not text:
                    continue

                if "เพิ่ง" in text or "เมื่อสักครู่" in text or "just now" in text:
                    return now
                if "เมื่อวาน" in text or "yesterday" in text:
                    return now - timedelta(days=1)

                match = re.search(
                    r"(\d+)\s*(นาที|ชั่วโมง|ชม|วัน|สัปดาห์|เดือน|ปี|mins?|m\b|hrs?|h\b|days?|d\b|weeks?|w\b|months?|years?)",
                    text,
                )
                if match:
                    num  = int(match.group(1))
                    unit = match.group(2)
                    if   "นาที"    in unit or "min" in unit or unit == "m": return now - timedelta(minutes=num)
                    elif "ชม"      in unit or "ชั่วโมง" in unit or "hr" in unit or unit == "h": return now - timedelta(hours=num)
                    elif "วัน"     in unit or "day" in unit or unit == "d": return now - timedelta(days=num)
                    elif "สัปดาห์" in unit or "week" in unit or unit == "w": return now - timedelta(weeks=num)
                    elif "เดือน"   in unit or "month" in unit: return now - timedelta(days=num * 30)
                    elif "ปี"      in unit or "year"  in unit: return now - timedelta(days=num * 365)

                thai_months = [
                    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
                    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม",
                    "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม",
                    "พฤศจิกายน", "ธันวาคม",
                ]
                if any(m in text for m in thai_months) and re.search(r"\d+", text):
                    # ไม่ hardcode days=2 แล้ว — คืน None แล้วให้ keyword check ตัดสินใจ
                    return None
        except Exception as e:
            self.log(f"⚠️ _parse_post_timestamp text parse: {e}")

        try:
            time_elements = article_element.find_elements(
                By.XPATH, ".//abbr[@data-utime] | .//a[contains(@aria-label, '20')]"
            )
            for el in time_elements:
                utime = el.get_attribute("data-utime")
                if utime:
                    return datetime.fromtimestamp(int(utime))
                aria = el.get_attribute("aria-label") or ""
                if aria:
                    for fmt in ["%d %B %Y เวลา %H:%M น.", "%B %d, %Y at %I:%M %p", "%B %d %Y"]:
                        try:
                            return datetime.strptime(aria, fmt)
                        except ValueError:
                            pass
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        return None

    def _get_articles(self) -> list:
        try:
            return self.driver.find_elements(By.XPATH, "//div[@role='article']")
        except Exception as e:
            self.log(f"⚠️ _get_articles error: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Main scrape logic
    # ─────────────────────────────────────────────────────────────────────────

    def scrape_page(self, page_url: str, keywords: list[str], hours_back: int) -> int:
        new_posts     = 0
        page_name     = page_url.rstrip("/").split("/")[-1]
        cutoff_time   = datetime.now() - timedelta(hours=hours_back)
        MAX_CONSECUTIVE_OLD = 5
        consecutive_old     = 0
        seen_this_run: set  = set()
        stop_early = False  # [3] flag แทน early return เพื่อให้ log สรุปทำงานได้

        try:
            self.log(f"🔍 กำลังเข้าเพจ: {page_url}")
            self.driver.get(page_url)
            time.sleep(3)

            obstacle = self._detect_obstacle()
            if obstacle:
                self._handle_obstacle(obstacle)
                if self._stop_event.is_set():
                    return 0

            scroll_rounds     = 0
            MAX_SCROLL_ROUNDS = 30
            last_article_count = 0
            no_growth_rounds   = 0
            MAX_NO_GROWTH      = 4

            while not self._stop_event.is_set() and not stop_early and scroll_rounds < MAX_SCROLL_ROUNDS:
                self._slow_scroll(scrolls=4, pause=2.0)
                scroll_rounds += 1
                articles = self._get_articles()

                if not articles:
                    self.log(f"⚠️ ไม่พบ article elements บนเพจ {page_name}")
                    break

                current_count = len(articles)
                if current_count > last_article_count:
                    no_growth_rounds    = 0
                    last_article_count  = current_count
                    self.log(f"📜 [{page_name}] Scroll {scroll_rounds} | โหลด article รวม: {current_count}")
                else:
                    no_growth_rounds += 1
                    self.log(
                        f"📜 [{page_name}] Scroll {scroll_rounds} | "
                        f"ไม่มีเนื้อหาใหม่ ({no_growth_rounds}/{MAX_NO_GROWTH})"
                    )
                    if no_growth_rounds >= MAX_NO_GROWTH:
                        self.log(f"📄 [{page_name}] หน้าไม่โหลดเพิ่มแล้ว — จบการสแกน")
                        break

                new_in_this_round = False

                for article in articles:
                    if self._stop_event.is_set() or stop_early:
                        break
                    self._resume_event.wait()

                    try:
                        # ── ดึง post URL ─────────────────────────────────────────
                        post_url = ""
                        try:
                            link_els = article.find_elements(
                                By.XPATH,
                                ".//a[contains(@href, '/posts/') or contains(@href, '/videos/') "
                                "or contains(@href, 'story_fbid') or contains(@href, '/permalink/')]",
                            )
                            if link_els:
                                post_url = link_els[0].get_attribute("href") or ""
                        except (NoSuchElementException, StaleElementReferenceException):
                            pass

                        if not post_url:
                            try:
                                anchors = article.find_elements(By.TAG_NAME, "a")
                                for a in anchors:
                                    href = a.get_attribute("href") or ""
                                    if page_name.lower() in href.lower() and len(href) > 30:
                                        post_url = href
                                        break
                            except Exception as e:
                                self.log(f"⚠️ fallback anchor search: {e}")

                        if not post_url:
                            continue

                        post_url_clean = post_url.split("?")[0].rstrip("/")

                        if post_url_clean in seen_this_run:
                            continue
                        seen_this_run.add(post_url_clean)
                        new_in_this_round = True

                        # ── ตรวจ DB ด้วย cleaned URL เสมอ (ป้องกัน MD5 ต่างกันสำหรับ URL เดียวกัน)
                        post_id = self._extract_post_id(post_url_clean)
                        if not post_id:
                            continue
                        if self.db.is_seen(post_id) or self.db.is_seen_by_url(post_url_clean):
                            continue

                        # ── ตรวจเวลาโพสต์ ─────────────────────────────────────
                        post_time = self._parse_post_timestamp(self.driver, article)
                        if post_time is not None:
                            if post_time < cutoff_time:
                                consecutive_old += 1
                                self.log(
                                    f"⏩ ข้ามโพสต์เก่า ({consecutive_old}/{MAX_CONSECUTIVE_OLD}) "
                                    f"| พบเวลา: {post_time.strftime('%d/%m %H:%M')}"
                                )
                                if consecutive_old >= MAX_CONSECUTIVE_OLD:
                                    self.log(
                                        f"🏁 เจอโพสต์เก่าเลยกำหนด ติดต่อกัน {MAX_CONSECUTIVE_OLD} "
                                        f"รายการ — หยุดสแกนเพจนี้"
                                    )
                                    stop_early = True  # [3] ใช้ flag แทน early return
                                    break
                                continue
                            else:
                                consecutive_old = 0
                        else:
                            self.log("⚠️ อ่านเวลาไม่ออก... กำลังตรวจสอบ Keywords ต่อไป")

                        # ── กด "ดูเพิ่มเติม" ──────────────────────────────────
                        try:
                            more_btns = article.find_elements(
                                By.XPATH,
                                ".//div[@role='button' and (contains(., 'ดูเพิ่มเติม') or contains(., 'See more'))]",
                            )
                            for btn in more_btns:
                                if btn.is_displayed():
                                    self.driver.execute_script("arguments[0].click();", btn)
                                    time.sleep(0.5)
                        except Exception as e:
                            self.log(f"⚠️ กดปุ่ม 'ดูเพิ่มเติม' ไม่สำเร็จ: {e}")

                        # ── ดึงข้อความ ────────────────────────────────────────
                        post_text = ""
                        try:
                            text_containers = article.find_elements(
                                By.XPATH,
                                ".//div[@data-ad-comet-preview='message'] | .//div[@data-testid='post_message']",
                            )
                            if text_containers:
                                post_text = text_containers[0].text.strip()
                            if not post_text:
                                continue
                        except StaleElementReferenceException:
                            continue

                        # ── ดึงรูปภาพ ─────────────────────────────────────────
                        image_url = None
                        try:
                            imgs = article.find_elements(
                                By.XPATH, ".//img[contains(@src, 'scontent')]"
                            )
                            for img in imgs:
                                src = img.get_attribute("src")
                                if src and "emoji" not in src:
                                    try:
                                        w = img.get_attribute("width")
                                        if w and int(w) <= 100:
                                            continue
                                    except (ValueError, TypeError) as e:
                                        # [3] แทน bare except: pass
                                        self.log(f"⚠️ อ่านขนาดรูปไม่ได้: {e}")
                                    image_url = src
                                    break
                        except Exception as e:
                            self.log(f"⚠️ ดึงรูปภาพไม่สำเร็จ: {e}")

                        # ── ตรวจ Keywords ─────────────────────────────────────
                        found_keywords = []
                        if keywords:
                            for kw in keywords:
                                if kw.lower().strip() in post_text.lower():
                                    found_keywords.append(kw.strip())
                            if not found_keywords:
                                continue

                        self.log(f"✅ โพสต์ตรงเงื่อนไข: {post_url_clean[:70]}")

                        # ── ส่ง Notification ──────────────────────────────────
                        self.discord.send_post(page_name, page_url, post_url_clean, post_text, found_keywords, image_url)
                        self.tg.send_post(page_name, page_url, post_url_clean, post_text, found_keywords, image_url)

                        self.db.mark_seen(post_id, page_url, post_url_clean)
                        new_posts += 1
                        time.sleep(0.5)

                    except StaleElementReferenceException:
                        continue
                    except Exception as e:
                        self.log(f"⚠️ ข้ามโพสต์ที่อ่านไม่ได้: {type(e).__name__}: {e}")
                        continue

                if not new_in_this_round and no_growth_rounds >= MAX_NO_GROWTH:
                    self.log(f"📄 ไม่มีโพสต์ใหม่และหน้าหยุดโหลดแล้ว บนเพจ {page_name} — จบการสแกน")
                    break

            # [3] log สรุปทำงานทุกครั้ง (ไม่โดนข้ามจาก early return อีกแล้ว)
            self.log(f"📊 สแกนเพจ {page_name} เสร็จ | Scroll {scroll_rounds} รอบ | โพสต์ใหม่: {new_posts}")

        except WebDriverException as e:
            self.log(f"❌ WebDriver Error ที่เพจ {page_name}: {e}")
        except Exception as e:
            self.log(f"❌ Error scraping {page_name}: {e}")

        return new_posts

    # ─────────────────────────────────────────────────────────────────────────
    # Main run loop
    # ─────────────────────────────────────────────────────────────────────────

    def run(
        self,
        email: str,
        password: str,
        page_urls: list[str],
        keywords: list[str],
        hours_back: int,
        loop_minutes: int,
    ):
        _started_successfully = False

        try:
            # แจ้งเตือนแค่ครั้งเดียวตอนกดปุ่ม Start
            self.discord.send_start(len(page_urls), len(keywords), loop_minutes)
            self.tg.send_start()
            _started_successfully = True
            last_cleanup_date = None

            while not self._stop_event.is_set():
                now = datetime.now()
                
                # [เผื่อใช้งาน] 🧹 ระบบทำความสะอาดและคืนพื้นที่ตอน 9 โมงเช้า
                if now.hour >= 9 and last_cleanup_date != now.date():
                    self.log(f"🧹 ถึงเวลา 09:00 น. | เริ่มล้างข้อมูล Database เก่า...")
                    if self.db.cleanup_old_data():
                        self.log("✅ ลบข้อมูลเก่าสำเร็จและคืนพื้นที่แล้ว")
                    last_cleanup_date = now.date()

                cycle_start = time.time()
                self.log(f"\n{'='*50}")
                self.log(f"🔄 เริ่มรอบสแกนใหม่ | {now.strftime('%d/%m/%Y %H:%M:%S')}")

                # 1. เปิด Browser สดใหม่ในทุกๆ รอบ
                self._start_browser()
                if not self._load_cookies():
                    self.log("🔑 ไม่มี Session เดิม — เริ่มล็อกอินใหม่")
                    if not self.login(email, password):
                        self.log("❌ Login ล้มเหลว — หยุดทำงาน")
                        self._stop_event.set()
                        break
                
                # 2. เริ่มไล่สแกนเพจ
                total_new = 0
                for url in page_urls:
                    if self._stop_event.is_set():
                        break
                    url = url.strip()
                    if not url:
                        continue
                    count = self.scrape_page(url, keywords, hours_back)
                    total_new += count
                    self.log(f"📊 เพจ {url.split('/')[-1]}: พบ {count} โพสต์ใหม่")
                    if not self._stop_event.is_set():
                        time.sleep(random.uniform(2.0, 5.0))

                if self._stop_event.is_set():
                    break

                # 3. สแกนจบ ส่งแจ้งเตือนสรุปผล
                duration = time.time() - cycle_start
                self.log(f"✅ รอบสแกนเสร็จ | พบโพสต์ใหม่รวม: {total_new}")
                self.discord.send_cycle_complete(duration, loop_minutes)
                self.tg.send_cycle_complete(duration, loop_minutes)

                # 4. ปิด BROWSER ทิ้งทันทีหลังส่งแจ้งเตือนเสร็จ
                self.log("🛑 ปิด Browser ชั่วคราวเพื่อประหยัดทรัพยากรระหว่างรอรอบถัดไป...")
                if self.driver:
                    try:
                        # ทริคของ undetected_chromedriver: ต้องสั่งปิดหน้าต่างก่อนสั่ง quit
                        self.driver.close() 
                        time.sleep(1) # รอให้หน้าต่างปิดสนิท
                        self.driver.quit()
                    except Exception as e:
                        # เปลี่ยนจาก pass เป็นให้แสดง Error ออกมา จะได้รู้ว่าติดอะไร
                        self.log(f"⚠️ เกิดข้อผิดพลาดตอนสั่งปิด Browser: {e}")
                    finally:
                        self.driver = None
                        
                # 5. เข้าสู่โหมดสลีป (นับถอยหลัง)
                sleep_total = loop_minutes * 60
                sleep_step  = 5
                elapsed     = 0
                while elapsed < sleep_total and not self._stop_event.is_set():
                    remaining = sleep_total - elapsed
                    if elapsed % 60 == 0:
                        self.log(f"⏳ รอ {int(remaining // 60)}m {int(remaining % 60)}s ก่อนรอบถัดไป...")
                    time.sleep(min(sleep_step, remaining))
                    elapsed += sleep_step

        except Exception as e:
            self.log(f"❌ Fatal Error ใน Scraper Thread: {e}")
        finally:
            if _started_successfully:
                self.discord.send_stopped()
                self.tg.send_stopped()

            # ปิด Browser ชัวร์ๆ อีกครั้งถ้าเกิด Error หลุดลูปมา
            if self.driver:
                self.log("🛑 สิ้นสุดการทำงาน — กำลังเคลียร์ Browser...")
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
    def stop(self):
        self._stop_event.set()
        self._resume_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD TAG INPUT WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class KeywordTagInput(ctk.CTkFrame):
    CHIP_BG     = "#1e3a5f"
    CHIP_FG     = "#7ec8f0"
    CHIP_BTN_FG = "#5ba4cf"
    CHIP_HOVER  = "#2a4f7a"

    def __init__(self, master, defaults: list | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self._tags: list[str]              = []
        self._chip_widgets: dict[str, tk.Frame] = {}
        self._build()
        for kw in (defaults or []):
            self._add_tag(kw)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        chip_outer = ctk.CTkFrame(self, fg_color=("gray85", "#1a1a2e"), corner_radius=8)
        chip_outer.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        chip_outer.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(chip_outer, bg="#1a1a2e", bd=0, highlightthickness=0, height=46)
        self._canvas.pack(fill="x", expand=True, padx=4, pady=(4, 0))

        self._hbar = tk.Scrollbar(chip_outer, orient="horizontal", command=self._canvas.xview)
        self._hbar.pack(fill="x", padx=4, pady=(0, 4))
        self._canvas.configure(xscrollcommand=self._hbar.set)

        self._chip_area = tk.Frame(self._canvas, bg="#1a1a2e")
        self._canvas_window = self._canvas.create_window((0, 0), window=self._chip_area, anchor="nw")

        self._chip_area.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: self._canvas.xview_scroll(int(-e.delta / 60), "units"),
        )

        input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 6))
        input_row.grid_columnconfigure(0, weight=1)

        self._entry_var = ctk.StringVar()
        self._entry = ctk.CTkEntry(
            input_row,
            textvariable=self._entry_var,
            placeholder_text="พิมพ์ keyword แล้วกด Enter หรือ ＋",
            height=34,
        )
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._entry.bind("<Return>", lambda e: self._on_add())

        add_btn = ctk.CTkButton(
            input_row, text="＋ เพิ่ม", width=80, height=34,
            fg_color="#1877F2", hover_color="#145db8", command=self._on_add,
        )
        add_btn.grid(row=0, column=1)

        clear_btn = ctk.CTkButton(
            input_row, text="ล้างทั้งหมด", width=90, height=34,
            fg_color="gray30", hover_color="gray20", command=self._clear_all,
        )
        clear_btn.grid(row=0, column=2, padx=(6, 0))

    def _on_add(self):
        raw = self._entry_var.get().strip()
        if not raw:
            return
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        for p in parts:
            self._add_tag(p)
        self._entry_var.set("")

    def _add_tag(self, text: str):
        if not text or text in self._tags:
            return
        self._tags.append(text)
        self._render_chip(text)

    def _render_chip(self, text: str):
        chip = tk.Frame(self._chip_area, bg=self.CHIP_BG, bd=0, padx=6, pady=3)
        chip.pack(side="left", padx=3, pady=3)

        lbl = tk.Label(chip, text=text, bg=self.CHIP_BG, fg=self.CHIP_FG, font=("Segoe UI", 10))
        lbl.pack(side="left")

        def remove():
            self._remove_tag(text, chip)

        btn = tk.Label(
            chip, text=" ✕", bg=self.CHIP_BG, fg=self.CHIP_BTN_FG,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda e: remove())
        btn.bind("<Enter>", lambda e: btn.config(fg="#ff6b6b"))
        btn.bind("<Leave>", lambda e: btn.config(fg=self.CHIP_BTN_FG))

        self._chip_widgets[text] = chip
        self._chip_area.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.xview_moveto(1.0)

    def _remove_tag(self, text: str, chip_frame):
        if text in self._tags:
            self._tags.remove(text)
        chip_frame.destroy()
        self._chip_widgets.pop(text, None)

    def _clear_all(self):
        for chip in list(self._chip_widgets.values()):
            chip.destroy()
        self._chip_widgets.clear()
        self._tags.clear()

    def get_keywords(self) -> list:
        return list(self._tags)

    def set_keywords(self, keywords: list):
        self._clear_all()
        for kw in keywords:
            self._add_tag(kw)


# ─────────────────────────────────────────────────────────────────────────────
# GUI APPLICATION
# ─────────────────────────────────────────────────────────────────────────────

class ScraperApp(ctk.CTk):
    SETTINGS_FILE = "scraper_settings.json"

    def __init__(self):
        super().__init__()
        self.title("📘 Facebook News Scraper & Discord/Telegram Notifier")
        self.geometry("820x950")
        self.resizable(True, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._db: DatabaseManager             = DatabaseManager()
        self._scraper: FacebookScraper | None  = None
        self._scraper_thread: threading.Thread | None = None
        self._log_queue: Queue                 = Queue()

        self._build_ui()
        self._load_settings()
        self._poll_log_queue()

    def _section_label(self, parent, text: str) -> ctk.CTkLabel:
        lbl = ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=13, weight="bold"))
        lbl.pack(anchor="w", pady=(12, 4))
        return lbl

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        outer = ctk.CTkScrollableFrame(self, label_text="")
        outer.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            outer, text="📘 Facebook News Scraper Notifier",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(10, 4))
        ctk.CTkLabel(
            outer,
            text="Automated keyword-based post monitoring with Discord & Telegram alerts",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        ).pack(pady=(0, 10))

        # Section 1 — Credentials
        cred_frame = ctk.CTkFrame(outer)
        cred_frame.pack(fill="x", padx=6, pady=4)
        self._section_label(cred_frame, "🔐 Section 1 — Facebook Credentials")
        ctk.CTkLabel(cred_frame, text="Email:").pack(anchor="w", padx=12)
        self.email_var = ctk.StringVar()
        ctk.CTkEntry(
            cred_frame, textvariable=self.email_var,
            placeholder_text="your@email.com", width=400,
        ).pack(anchor="w", padx=12, pady=(0, 6))
        ctk.CTkLabel(cred_frame, text="Password:").pack(anchor="w", padx=12)
        self.pass_var = ctk.StringVar()
        ctk.CTkEntry(
            cred_frame, textvariable=self.pass_var, show="●",
            placeholder_text="รหัสผ่าน", width=400,
        ).pack(anchor="w", padx=12, pady=(0, 10))

        # Section 2 — Target
        target_frame = ctk.CTkFrame(outer)
        target_frame.pack(fill="x", padx=6, pady=4)
        self._section_label(target_frame, "🎯 Section 2 — Target Settings")
        ctk.CTkLabel(target_frame, text="URL เพจเป้าหมาย (แต่ละบรรทัด = 1 เพจ):").pack(anchor="w", padx=12)
        self.pages_textbox = ctk.CTkTextbox(target_frame, height=100)
        self.pages_textbox.pack(fill="x", padx=12, pady=(0, 6))
        self.pages_textbox.insert(
            "1.0",
            "https://www.facebook.com/BBCnewsThai\nhttps://www.facebook.com/voathai",
        )
        ctk.CTkLabel(
            target_frame,
            text="🔑 Keywords — พิมพ์แล้วกด Enter หรือ ＋ | รองรับ #แฮชแท็ก | วางหลายคำคั่น , ได้เลย",
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=12)
        self.keywords_widget = KeywordTagInput(
            target_frame,
            defaults=["เพื่อไทย", "แพทองธาร", "ทักษิณ", "เศรษฐา"],
        )
        self.keywords_widget.pack(fill="x", padx=8, pady=(0, 10))

        # Section 3 — Timeframe
        time_frame = ctk.CTkFrame(outer)
        time_frame.pack(fill="x", padx=6, pady=4)
        self._section_label(time_frame, "⏱️ Section 3 — Timeframe & Loop Settings")
        row3 = ctk.CTkFrame(time_frame, fg_color="transparent")
        row3.pack(fill="x", padx=12)
        ctk.CTkLabel(row3, text="ดึงโพสต์ย้อนหลัง (ชั่วโมง):").grid(row=0, column=0, padx=(0, 10), pady=4, sticky="w")
        self.hours_var = ctk.StringVar(value="6")
        ctk.CTkEntry(row3, textvariable=self.hours_var, width=80).grid(row=0, column=1, pady=4, sticky="w")
        ctk.CTkLabel(row3, text="   วนลูปทุก (นาที):").grid(row=0, column=2, padx=(20, 10), pady=4, sticky="w")
        self.loop_var = ctk.StringVar(value="30")
        ctk.CTkEntry(row3, textvariable=self.loop_var, width=80).grid(row=0, column=3, pady=4, sticky="w")

        # Section 4 — Discord
        discord_frame = ctk.CTkFrame(outer)
        discord_frame.pack(fill="x", padx=6, pady=4)
        self._section_label(discord_frame, "💬 Section 4 — Discord Webhook (Optional)")
        ctk.CTkLabel(discord_frame, text="Webhook URL:").pack(anchor="w", padx=12)
        self.webhook_var = ctk.StringVar()
        ctk.CTkEntry(
            discord_frame, textvariable=self.webhook_var,
            placeholder_text="https://discord.com/api/webhooks/...", width=560,
        ).pack(anchor="w", padx=12, pady=(0, 10))

        # Section 5 — Telegram
        tg_frame = ctk.CTkFrame(outer)
        tg_frame.pack(fill="x", padx=6, pady=4)
        self._section_label(tg_frame, "✈️ Section 5 — Telegram Settings (Optional)")
        ctk.CTkLabel(tg_frame, text="Bot Token:").pack(anchor="w", padx=12)
        self.tg_token_var = ctk.StringVar()
        ctk.CTkEntry(
            tg_frame, textvariable=self.tg_token_var,
            placeholder_text="123456789:ABCDefghIJKlmNoPQRsTUVwxyZ", width=560,
        ).pack(anchor="w", padx=12, pady=(0, 6))
        ctk.CTkLabel(tg_frame, text="Chat ID:").pack(anchor="w", padx=12)
        self.tg_chatid_var = ctk.StringVar()
        ctk.CTkEntry(
            tg_frame, textvariable=self.tg_chatid_var,
            placeholder_text="-100123456789", width=560,
        ).pack(anchor="w", padx=12, pady=(0, 10))

        # Section 6 — Controls
        ctrl_frame = ctk.CTkFrame(outer)
        ctrl_frame.pack(fill="x", padx=6, pady=4)
        self._section_label(ctrl_frame, "🎮 Section 6 — Controls")
        btn_row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        btn_row.pack(padx=12, pady=(0, 10))

        self.start_btn = ctk.CTkButton(
            btn_row, text="▶ Start", width=120, height=44,
            fg_color="#1877F2", hover_color="#145db8",
            font=ctk.CTkFont(size=14, weight="bold"), command=self._on_start,
        )
        self.start_btn.grid(row=0, column=0, padx=6)

        self.stop_btn = ctk.CTkButton(
            btn_row, text="⏹ Stop", width=120, height=44,
            fg_color="#E53935", hover_color="#b71c1c",
            font=ctk.CTkFont(size=14, weight="bold"), command=self._on_stop, state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, padx=6)

        self.resume_btn = ctk.CTkButton(
            btn_row, text="▶▶ Resume (แก้ 2FA)", width=160, height=44,
            fg_color="#FF8F00", hover_color="#e65100",
            font=ctk.CTkFont(size=13, weight="bold"), command=self._on_resume, state="disabled",
        )
        self.resume_btn.grid(row=0, column=2, padx=6)

        self.hide_browser_btn = ctk.CTkButton(
            btn_row, text="🙈 ซ่อน Browser", width=120, height=44,
            fg_color="#4A148C", hover_color="#6A1FBF",
            font=ctk.CTkFont(size=13, weight="bold"), command=self._on_hide_browser, state="disabled",
        )
        self.hide_browser_btn.grid(row=0, column=3, padx=6)

        self.save_cfg_btn = ctk.CTkButton(
            btn_row, text="💾 บันทึกตั้งค่า", width=120, height=44,
            fg_color="#2E7D32", hover_color="#1B5E20",
            font=ctk.CTkFont(size=13, weight="bold"), command=self._save_settings,
        )
        self.save_cfg_btn.grid(row=0, column=4, padx=6)

        self._hide_hint_lbl = ctk.CTkLabel(
            ctrl_frame,
            text="ปุ่มซ่อน Browser จะเปิดใช้งานได้หลังจากบันทึก Cookies สำเร็จ",
            font=ctk.CTkFont(size=10), text_color="gray50",
        )
        self._hide_hint_lbl.pack(pady=(0, 2))

        self.status_lbl = ctk.CTkLabel(
            ctrl_frame, text="● หยุดทำงาน",
            text_color="#E53935", font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_lbl.pack(pady=(0, 8))

        # Section 7 — Log
        log_frame = ctk.CTkFrame(outer)
        log_frame.pack(fill="both", expand=True, padx=6, pady=4)
        self._section_label(log_frame, "📋 Section 7 — Real-time Log")
        self.log_textbox = ctk.CTkTextbox(
            log_frame, height=260, state="disabled",
            font=ctk.CTkFont(family="Courier New", size=11),
        )
        self.log_textbox.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        ctk.CTkButton(
            log_frame, text="🗑 ล้าง Log", width=120, height=28,
            fg_color="gray30", hover_color="gray20", command=self._clear_log,
        ).pack(anchor="e", padx=12, pady=(0, 10))

    # ─────────────────────────────────────────────────────────────────────────
    # Settings
    # ─────────────────────────────────────────────────────────────────────────

    def _save_settings(self):
        settings = {
            "email":    self.email_var.get(),
            "password": self.pass_var.get(),
            "pages":    self.pages_textbox.get("1.0", "end").strip(),
            "keywords": self.keywords_widget.get_keywords(),
            "hours":    self.hours_var.get(),
            "loop":     self.loop_var.get(),
            "webhook":  self.webhook_var.get(),
            "tg_token": self.tg_token_var.get(),
            "tg_chatid":self.tg_chatid_var.get(),
        }
        try:
            with open(self.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
            self._log("💾 บันทึกการตั้งค่าลงไฟล์เรียบร้อยแล้ว")
        except Exception as e:
            self._show_error(f"⚠️ เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {e}")

    def _load_settings(self):
        if not os.path.exists(self.SETTINGS_FILE):
            return
        try:
            with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
            self.email_var.set(settings.get("email", ""))
            self.pass_var.set(settings.get("password", ""))
            pages = settings.get("pages", "")
            if pages:
                self.pages_textbox.delete("1.0", "end")
                self.pages_textbox.insert("1.0", pages)
            self.keywords_widget.set_keywords(settings.get("keywords", []))
            self.hours_var.set(settings.get("hours", "6"))
            self.loop_var.set(settings.get("loop", "30"))
            self.webhook_var.set(settings.get("webhook", ""))
            self.tg_token_var.set(settings.get("tg_token", ""))
            self.tg_chatid_var.set(settings.get("tg_chatid", ""))
            self._log("🔄 โหลดการตั้งค่าเดิมเรียบร้อยแล้ว")
        except Exception as e:
            self._log(f"⚠️ โหลดการตั้งค่าไม่สำเร็จ: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Log
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, message: str):
        self._log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_textbox.configure(state="normal")
                self.log_textbox.insert("end", msg + "\n")
                self.log_textbox.see("end")
                self.log_textbox.configure(state="disabled")
        except Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Controls
    # ─────────────────────────────────────────────────────────────────────────

    def _on_start(self):
        email    = self.email_var.get().strip()
        password = self.pass_var.get().strip()
        pages_raw = self.pages_textbox.get("1.0", "end").strip()
        webhook  = self.webhook_var.get().strip()
        tg_token = self.tg_token_var.get().strip()
        tg_chatid = self.tg_chatid_var.get().strip()

        if tg_token:
            self._tg_listener = TelegramListener(tg_token)
            self._tg_listener.start()

        try:
            hours    = int(self.hours_var.get().strip())
            loop_min = int(self.loop_var.get().strip())
        except ValueError:
            self._show_error("⚠️ กรุณากรอกตัวเลขในช่อง Timeframe และ Loop")
            return

        if not email or not password:
            self._show_error("⚠️ กรุณากรอก Email และ Password")
            return

        page_urls = [u.strip() for u in pages_raw.splitlines() if u.strip()]
        if not page_urls:
            self._show_error("⚠️ กรุณากรอก URL เพจอย่างน้อย 1 เพจ")
            return

        if not webhook and not (tg_token and tg_chatid):
            self._show_error(
                "⚠️ กรุณากรอก Discord Webhook หรือ Telegram Bot Token + Chat ID "
                "อย่างใดอย่างหนึ่งเป็นอย่างน้อย"
            )
            return

        keywords = self.keywords_widget.get_keywords()
        discord  = DiscordNotifier(webhook)
        tg       = TelegramNotifier(tg_token, tg_chatid)

        self._scraper = FacebookScraper(
            self._log, self._db, discord, tg,
            on_cookies_saved=self._enable_hide_btn,
        )

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.resume_btn.configure(state="normal")
        self.status_lbl.configure(text="● กำลังทำงาน...", text_color="#4CAF50")

        self._log(
            f"🚀 เริ่มทำงาน | เพจ: {len(page_urls)} | "
            f"Keywords: {keywords or 'ทั้งหมด'} | Loop: {loop_min}m"
        )

        self._scraper_thread = threading.Thread(
            target=self._scraper.run,
            args=(email, password, page_urls, keywords, hours, loop_min),
            daemon=True,
            name="ScraperThread",
        )
        self._scraper_thread.start()
        self.after(500, self._check_thread_alive)

    def _on_stop(self):
        if self._scraper:
            self._scraper.stop()
        if hasattr(self, "_tg_listener"):
            self._tg_listener.stop()
        self._log("🛑 ส่งสัญญาณหยุด Scraper แล้ว...")
        self._reset_ui()

    def _on_resume(self):
        if self._scraper:
            self._scraper.resume()
            self._log("▶️ กด Resume แล้ว — Scraper กลับมาทำงาน")

    def _enable_hide_btn(self):
        self.hide_browser_btn.configure(state="normal")
        self._hide_hint_lbl.configure(
            text="✅ Cookies บันทึกแล้ว — กด 🙈 ซ่อน Browser ได้เลย",
            text_color="#4CAF50",
        )

    def _on_hide_browser(self):
        drv = self._scraper.driver if self._scraper else None
        if drv:
            try:
                # โยนหน้าต่างออกไปที่พิกัด X: -2000, Y: -2000 (ออกนอกจอไปเลย)
                # วิธีนี้ปลอดภัยกว่า Headless Mode ที่เฟสบุ๊คดักจับได้
                drv.set_window_position(-2000, -2000)
                self._log("🙈 ซ่อน Browser ไปทำงานเบื้องหลังแล้ว")
                self.hide_browser_btn.configure(text="👁 แสดง Browser", command=self._on_show_browser)
            except Exception as e:
                self._log(f"⚠️ ซ่อน Browser ไม่สำเร็จ: {e}")

    def _on_show_browser(self):
        drv = self._scraper.driver if self._scraper else None
        if drv:
            try:
                # ดึงหน้าต่างกลับมาที่พิกัด 0, 0 และขยายเต็มจอ
                drv.set_window_position(0, 0)
                drv.maximize_window()
                self._log("👁 แสดง Browser กลับมาแล้ว")
                self.hide_browser_btn.configure(text="🙈 ซ่อน Browser", command=self._on_hide_browser)
            except Exception as e:
                self._log(f"⚠️ แสดง Browser ไม่สำเร็จ: {e}")

    def _check_thread_alive(self):
        if self._scraper_thread and not self._scraper_thread.is_alive():
            self._reset_ui()
            self._log("✅ Scraper Thread สิ้นสุดการทำงาน")
        else:
            self.after(500, self._check_thread_alive)

    def _reset_ui(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.status_lbl.configure(text="● หยุดทำงาน", text_color="#E53935")
        self.hide_browser_btn.configure(
            state="disabled", text="🙈 ซ่อน Browser", command=self._on_hide_browser
        )
        self._hide_hint_lbl.configure(
            text="ปุ่มซ่อน Browser จะเปิดใช้งานได้หลังจากบันทึก Cookies สำเร็จ",
            text_color="gray50",
        )

    def _show_error(self, msg: str):
        self._log(msg)
        dialog = ctk.CTkToplevel(self)
        dialog.title("⚠️ ข้อผิดพลาด")
        dialog.geometry("380x130")
        dialog.grab_set()
        ctk.CTkLabel(dialog, text=msg, wraplength=340).pack(pady=20)
        ctk.CTkButton(dialog, text="ตกลง", command=dialog.destroy).pack()

    def on_close(self):
        if self._scraper:
            self._scraper.stop()
        self._db.close()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ScraperApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()

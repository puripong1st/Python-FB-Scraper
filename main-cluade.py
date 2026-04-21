"""
Facebook News Scraper & Discord Notifier
=========================================
A desktop automation tool that scrapes Facebook pages for posts matching
specified keywords and sends Discord webhook notifications.

Author: Senior Python Developer
Tech Stack: undetected-chromedriver, CustomTkinter, SQLite3, requests
"""

import customtkinter as ctk
import threading
import sqlite3
import requests
import time
import json
import re
import os
import pickle
from datetime import datetime, timedelta
from queue import Queue, Empty

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
    """
    Handles all SQLite operations for storing seen Post IDs
    to prevent duplicate Discord notifications.
    """

    DB_FILE = "scraper_data.db"

    def __init__(self):
        self.conn = sqlite3.connect(self.DB_FILE, check_same_thread=False)
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self):
        """Create the seen_posts table if it doesn't exist."""
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    post_id TEXT PRIMARY KEY,
                    page_url TEXT,
                    post_url TEXT,
                    detected_at TEXT
                )
            """)
            self.conn.commit()

    def is_seen(self, post_id: str) -> bool:
        """Return True if post_id already exists in the database."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
            )
            return cur.fetchone() is not None

    def mark_seen(self, post_id: str, page_url: str, post_url: str):
        """Insert a new post_id into the database."""
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO seen_posts (post_id, page_url, post_url, detected_at) VALUES (?, ?, ?, ?)",
                    (post_id, page_url, post_url, datetime.now().isoformat()),
                )
                self.conn.commit()
            except sqlite3.Error as e:
                pass  # Ignore duplicate key errors

    def close(self):
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# DISCORD NOTIFIER
# ─────────────────────────────────────────────────────────────────────────────

class DiscordNotifier:
    """
    Sends formatted webhook messages to a Discord channel.
    Supports text messages and rich embeds.
    """

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _send(self, payload: dict) -> bool:
        """POST a JSON payload to Discord webhook. Returns True on success."""
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

    def send_start(self):
        """Notify Discord that the scraper has started."""
        payload = {
            "content": f"🟢 **เริ่มระบบ Scraper** | เวลา: `{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}`"
        }
        self._send(payload)

    def send_post(self, page_name: str, post_url: str, content: str):
        """Send a rich embed for a matched Facebook post."""
        snippet = content[:300] + "..." if len(content) > 300 else content
        embed = {
            "title": f"📢 โพสต์ใหม่จาก {page_name}",
            "description": snippet,
            "url": post_url,
            "color": 0x1877F2,  # Facebook blue
            "fields": [
                {"name": "🔗 ลิงก์โพสต์", "value": post_url, "inline": False},
                {"name": "📄 เพจต้นทาง", "value": page_name, "inline": True},
                {
                    "name": "🕐 เวลาตรวจพบ",
                    "value": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "inline": True,
                },
            ],
            "footer": {"text": "Facebook Scraper Bot"},
        }
        payload = {"embeds": [embed]}
        self._send(payload)

    def send_cycle_complete(self, duration_sec: float, next_run_min: int):
        """Notify Discord when a full scraping cycle is done."""
        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        payload = {
            "content": (
                f"✅ **สแกนรอบนี้เสร็จสิ้น** | "
                f"ระยะเวลาที่ใช้: `{mins}m {secs}s` | "
                f"รอวนลูปทำงานรอบต่อไปในอีก `{next_run_min} นาที`"
            )
        }
        self._send(payload)

    def send_obstacle(self, obstacle_type: str):
        """Alert Discord that manual intervention is needed."""
        payload = {
            "content": (
                f"🚨 @everyone **บอทติดหน้า {obstacle_type}** | "
                f"กรุณาเข้ามากดแก้ในหน้าต่างเบราว์เซอร์ด่วน! "
                f"แล้วกดปุ่ม **Resume** บนโปรแกรม"
            )
        }
        self._send(payload)

    def send_stopped(self):
        """Notify Discord that the bot was stopped by the user."""
        payload = {"content": "🔴 **ระบบ Scraper หยุดทำงานแล้ว** (หยุดโดยผู้ใช้)"}
        self._send(payload)


# ─────────────────────────────────────────────────────────────────────────────
# FACEBOOK SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

COOKIES_FILE = "fb_cookies.pkl"


class FacebookScraper:
    """
    Core scraping engine. Uses undetected-chromedriver to bypass Facebook's
    bot detection. Handles login, obstacle detection, and post extraction.
    """

    # XPath / CSS selectors — these may need updating if Facebook changes its HTML
    SELECTORS = {
        "email_input": "//input[@id='email' or @name='email']",
        "pass_input": "//input[@id='pass' or @name='pass']",
        "login_btn": "//button[@name='login' or @data-testid='royal_login_button']",
        "post_story": "[data-testid='story-subtitle'], div[role='article']",
        "feed_posts": "div[role='feed'] > div",
    }

    CHECKPOINT_URLS = ["checkpoint", "login_attempt", "two_step_verification", "captcha"]
    HOME_URL = "https://www.facebook.com"

    def __init__(self, log_callback, db: DatabaseManager, discord: DiscordNotifier,
                 on_cookies_saved=None):
        self.log = log_callback        # Function to write to GUI log
        self.db = db
        self.discord = discord
        self.driver = None
        self._on_cookies_saved = on_cookies_saved  # Callback → enable Hide Browser btn

        # Threading control
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()  # Not paused by default
        self._is_paused = False

    # ── Browser ──────────────────────────────────────────────────────────────

    def _start_browser(self):
        """Launch undetected Chrome with custom options to mimic a real user."""
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--lang=th-TH,th;q=0.9,en-US;q=0.8")
        options.add_argument("--window-size=1280,900")
        # Keep browser open even after Python quits (safer UX for manual intervention)
        options.add_experimental_option = None  # Not supported in uc, skip
        self.driver = uc.Chrome(options=options, use_subprocess=True)
        self.driver.set_page_load_timeout(30)
        self.log("🌐 เปิด Browser สำเร็จ")

    def _save_cookies(self):
        if self.driver:
            with open(COOKIES_FILE, "wb") as f:
                pickle.dump(self.driver.get_cookies(), f)
            self.log("🍪 บันทึก Session Cookies แล้ว")
            # Notify GUI so the Hide Browser button can be enabled
            if self._on_cookies_saved:
                try:
                    self._on_cookies_saved()
                except Exception:
                    pass

    def _load_cookies(self) -> bool:
        """Try to restore a previous session. Returns True if cookies loaded."""
        if not os.path.exists(COOKIES_FILE):
            return False
        try:
            self.driver.get(self.HOME_URL)
            time.sleep(2)
            with open(COOKIES_FILE, "rb") as f:
                cookies = pickle.load(f)
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass
            self.driver.refresh()
            time.sleep(3)
            if "login" not in self.driver.current_url.lower():
                self.log("✅ กู้คืน Session เดิมสำเร็จ — ไม่ต้องล็อกอินใหม่")
                return True
        except Exception as e:
            self.log(f"⚠️ โหลด Cookies ไม่สำเร็จ: {e}")
        return False

    # ── Login ─────────────────────────────────────────────────────────────────

    def _type_human(self, element, text: str, delay: float = 0.06):
        """Type text character-by-character with slight random delay (human-like)."""
        element.clear()
        time.sleep(0.3)
        for ch in text:
            element.send_keys(ch)
            time.sleep(delay + (time.time() % 0.03))

    def _click_login_button(self) -> bool:
        """
        Try multiple strategies to click the Facebook login button.
        Returns True if a click was performed.

        Facebook frequently changes its HTML structure. We try:
          1. button[name='login']
          2. button[type='submit'] inside the login form
          3. Any <button> containing the text 'เข้าสู่ระบบ' or 'Log in'
          4. Fallback: press Enter on the password field
        """
        strategies = [
            # Strategy 1: name attribute (most reliable historically)
            (By.CSS_SELECTOR, "button[name='login']"),
            # Strategy 2: data-testid (older FB versions)
            (By.CSS_SELECTOR, "[data-testid='royal_login_button']"),
            # Strategy 3: type=submit inside a form that has email/pass
            (By.CSS_SELECTOR, "form button[type='submit']"),
            # Strategy 4: XPath text match for Thai UI
            (By.XPATH, "//button[contains(., 'เข้าสู่ระบบ')]"),
            # Strategy 5: XPath text match for English UI
            (By.XPATH, "//button[contains(., 'Log in') or contains(., 'Log In')]"),
            # Strategy 6: Any button inside #loginform
            (By.XPATH, "//*[@id='loginform']//button"),
            # Strategy 7: div acting as a button with login text
            (By.XPATH, "//div[@role='button' and (contains(., 'Log') or contains(., 'เข้า'))]"),
        ]

        for by, selector in strategies:
            try:
                btn = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable((by, selector))
                )
                # Scroll into view to ensure it's visible
                self.driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.3)
                btn.click()
                self.log(f"🖱️ คลิกปุ่ม Login สำเร็จ ({selector[:40]})")
                return True
            except (TimeoutException, NoSuchElementException, Exception):
                continue

        # Last resort: press Enter on the password field
        try:
            pass_field = self.driver.find_element(By.XPATH, self.SELECTORS["pass_input"])
            pass_field.send_keys(Keys.RETURN)
            self.log("⌨️ กด Enter บน Password field (fallback)")
            return True
        except Exception:
            pass

        return False

    def login(self, email: str, password: str) -> bool:
        """
        Perform automated Facebook login.
        Returns True if login appears successful.
        On failure, pauses and waits for the user to manually fix things
        and press Resume — the browser stays open.
        """
        try:
            self.driver.get(f"{self.HOME_URL}/login")
            wait = WebDriverWait(self.driver, 20)

            # ── Fill Email ────────────────────────────────────────────────────
            self.log("📧 กำลังกรอก Email...")
            email_field = wait.until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["email_input"]))
            )
            self._type_human(email_field, email, delay=0.06)
            time.sleep(0.4)

            # ── Fill Password ─────────────────────────────────────────────────
            self.log("🔑 กำลังกรอก Password...")
            pass_field = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["pass_input"]))
            )
            self._type_human(pass_field, password, delay=0.05)
            time.sleep(0.6)

            # ── Click Login ───────────────────────────────────────────────────
            self.log("🖱️ กำลังคลิกปุ่ม Login...")
            clicked = self._click_login_button()
            if not clicked:
                # Cannot find button at all — pause for manual intervention
                self.log("⚠️ หาปุ่ม Login ไม่เจอ — กรุณากด Login ใน Browser ด้วยตัวเอง แล้วกด Resume")
                self._handle_obstacle("Login Button Not Found — กรุณากด Login ด้วยตัวเอง")
                if self._stop_event.is_set():
                    return False

            # ── Wait for redirect ─────────────────────────────────────────────
            self.log("⏳ รอหน้าเว็บโหลดหลัง Login...")
            time.sleep(6)

            # ── Check for post-login obstacles ────────────────────────────────
            obstacle = self._detect_obstacle()
            if obstacle:
                self.log(f"⚠️ ติด {obstacle} หลังล็อกอิน")
                self._handle_obstacle(obstacle)
                if self._stop_event.is_set():
                    return False
                # After Resume: re-check URL
                time.sleep(2)

            # ── Verify success ────────────────────────────────────────────────
            current_url = self.driver.current_url.lower()
            if "login" not in current_url and "facebook.com" in current_url:
                self._save_cookies()
                self.log(f"✅ ล็อกอินสำเร็จ | URL: {self.driver.current_url[:60]}")
                return True

            # Login didn't redirect away from login page
            # → Pause and let user fix it manually instead of quitting browser
            self.log("⚠️ ยังอยู่หน้า Login — อาจ Email/Password ผิด หรือมี CAPTCHA ที่มองไม่เห็น")
            self.log("👉 กรุณาล็อกอินด้วยตัวเองในหน้าต่าง Browser แล้วกด Resume")
            self._handle_obstacle("Login ไม่สำเร็จ — กรุณาล็อกอินด้วยตัวเอง")
            if self._stop_event.is_set():
                return False
            # After resume — user should have logged in manually
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

    # ── Obstacle Detection ────────────────────────────────────────────────────

    def _detect_obstacle(self) -> str | None:
        """
        Inspect the current URL and page title to detect CAPTCHA,
        2FA, Checkpoint, or other blocking pages.
        Returns a string describing the obstacle, or None if clear.
        """
        url = self.driver.current_url.lower()
        title = self.driver.title.lower()

        if "checkpoint" in url or "checkpoint" in title:
            return "Checkpoint"
        if "two_step_verification" in url or "two_factor" in url:
            return "2FA (Two-Factor Authentication)"
        if "captcha" in url or "captcha" in title:
            return "CAPTCHA"
        if "login_attempt" in url:
            return "Login Attempt Blocked"
        if "suspended" in url or "disabled" in url:
            return "Account Suspended/Disabled"

        # Check page source for common obstacle text
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            if "confirm your identity" in body_text or "ยืนยันตัวตน" in body_text:
                return "Identity Verification"
        except Exception:
            pass

        return None

    def _handle_obstacle(self, obstacle_type: str):
        """
        Pause the scraper thread and notify Discord + GUI.
        Waits until the user clicks Resume.
        """
        self.log(f"🚨 ติด {obstacle_type} — หยุดรอผู้ใช้แก้ไข กด Resume เมื่อเสร็จ")
        self.discord.send_obstacle(obstacle_type)
        self._resume_event.clear()  # Pause this thread
        self._is_paused = True
        # Block here until resume_event is set by GUI's Resume button
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume แล้ว — กลับมาทำงานต่อ")

    def resume(self):
        """Called by the GUI Resume button to unpause the scraper."""
        self._resume_event.set()

    # ── Scraping ──────────────────────────────────────────────────────────────

    def _slow_scroll(self, scrolls: int = 5, pause: float = 1.8):
        """
        Scroll down the page slowly in multiple steps to trigger lazy loading.
        Human-like behavior reduces bot-detection risk.
        """
        for _ in range(scrolls):
            if self._stop_event.is_set():
                break
            self.driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
            time.sleep(pause + (time.time() % 0.5))  # Add small random jitter

    def _extract_post_id(self, url: str) -> str | None:
        """Extract a unique post identifier from a Facebook post URL."""
        # Pattern: /posts/XXXXXXX or /videos/XXXXXXX or ?story_fbid=XXXXXXX
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
        # Fallback: hash the URL
        return str(hash(url))

    def _parse_post_timestamp(self, driver, article_element) -> datetime | None:
        """
        Try to find the post's timestamp element and parse it.
        Facebook uses <abbr> or <a> with aria-label containing date text.
        """
        try:
            # Facebook typically puts timestamp in an <abbr> tag or aria-label
            time_elements = article_element.find_elements(
                By.XPATH, ".//abbr[@data-utime] | .//a[contains(@aria-label, '2024') or contains(@aria-label, '2025') or contains(@aria-label, '2026')]"
            )
            for el in time_elements:
                # data-utime is a Unix timestamp
                utime = el.get_attribute("data-utime")
                if utime:
                    return datetime.fromtimestamp(int(utime))
                # Try aria-label text parsing
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
        """Re-query all article elements from the current page (avoids stale refs)."""
        try:
            return self.driver.find_elements(By.XPATH, "//div[@role='article']")
        except Exception:
            return []

    def scrape_page(
        self,
        page_url: str,
        keywords: list[str],
        hours_back: int,
    ) -> int:
        """
        Scrape a single Facebook page for matching posts within the timeframe.

        Strategy:
          - Scroll incrementally, re-querying articles each batch.
          - Process every article; NEVER break the loop early on a single timestamp.
          - Only stop scrolling when MAX_CONSECUTIVE_OLD posts in a row are past cutoff.
          - `continue` (skip) old posts — never `break` the whole loop prematurely.
        """
        new_posts = 0
        page_name = page_url.rstrip("/").split("/")[-1]
        cutoff_time = datetime.now() - timedelta(hours=hours_back)

        # How many consecutive OLD posts before we give up scrolling
        MAX_CONSECUTIVE_OLD = 5
        consecutive_old = 0

        # Track URLs processed this run so re-querying DOM doesn't double-process
        seen_this_run: set = set()

        try:
            self.log(f"🔍 กำลังเข้าเพจ: {page_url}")
            self.driver.get(page_url)
            time.sleep(3)

            # Check for obstacles on this page
            obstacle = self._detect_obstacle()
            if obstacle:
                self._handle_obstacle(obstacle)
                if self._stop_event.is_set():
                    return 0

            # ── Incremental scroll-and-process loop ───────────────────────────
            # Each round: scroll 3× → re-query ALL articles → process new ones.
            # This avoids collecting a stale snapshot upfront.
            scroll_rounds = 0
            MAX_SCROLL_ROUNDS = 20  # Safety cap

            while not self._stop_event.is_set() and scroll_rounds < MAX_SCROLL_ROUNDS:
                self._slow_scroll(scrolls=3, pause=1.8)
                scroll_rounds += 1

                articles = self._get_articles()
                if not articles:
                    self.log(f"⚠️ ไม่พบ article elements บนเพจ {page_name}")
                    break

                new_in_this_round = False  # Did we find any previously unseen article?

                for article in articles:
                    if self._stop_event.is_set():
                        break

                    self._resume_event.wait()

                    try:
                        # ── Extract post URL ──────────────────────────────────
                        post_url = ""
                        try:
                            link_els = article.find_elements(
                                By.XPATH,
                                ".//a[contains(@href, '/posts/') or "
                                "contains(@href, '/videos/') or "
                                "contains(@href, 'story_fbid') or "
                                "contains(@href, '/permalink/')]"
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
                            except Exception:
                                pass

                        if not post_url:
                            continue

                        post_url_clean = post_url.split("?")[0].rstrip("/")

                        # Skip already-processed articles this run
                        if post_url_clean in seen_this_run:
                            continue
                        seen_this_run.add(post_url_clean)
                        new_in_this_round = True

                        # ── Extract Post ID ───────────────────────────────────
                        post_id = self._extract_post_id(post_url)
                        if not post_id:
                            continue

                        # ── Deduplication (DB) ────────────────────────────────
                        if self.db.is_seen(post_id):
                            continue

                        # ── Extract text ──────────────────────────────────────
                        post_text = ""
                        try:
                            text_containers = article.find_elements(
                                By.XPATH,
                                ".//div[@data-ad-comet-preview='message'] | "
                                ".//div[@data-testid='post_message'] | "
                                ".//div[contains(@class, 'xdj266r')]"
                            )
                            for tc in text_containers:
                                t = tc.text.strip()
                                if t:
                                    post_text += t + " "
                            post_text = post_text.strip()
                            if not post_text:
                                post_text = article.text.strip()[:1000]
                        except StaleElementReferenceException:
                            continue

                        # ── Timestamp check ───────────────────────────────────
                        # IMPORTANT: Use continue (not break) for old posts.
                        # Track consecutive old posts to know when to stop scrolling.
                        post_time = self._parse_post_timestamp(self.driver, article)
                        if post_time is not None:
                            if post_time < cutoff_time:
                                consecutive_old += 1
                                self.log(
                                    f"⏩ ข้ามโพสต์เก่า ({consecutive_old}/{MAX_CONSECUTIVE_OLD})"
                                )
                                if consecutive_old >= MAX_CONSECUTIVE_OLD:
                                    self.log(
                                        f"🏁 เจอโพสต์เก่าติดต่อกัน {MAX_CONSECUTIVE_OLD} รายการ "
                                        f"— หยุดสแกนเพจนี้"
                                    )
                                    return new_posts
                                continue  # Skip this old post, keep checking others
                            else:
                                consecutive_old = 0  # Fresh post — reset counter

                        # ── Keyword filter ────────────────────────────────────
                        if keywords:
                            if not any(kw.lower().strip() in post_text.lower() for kw in keywords):
                                continue

                        # ── Send & record ─────────────────────────────────────
                        self.log(f"✅ โพสต์ตรงเงื่อนไข: {post_url_clean[:70]}")
                        self.discord.send_post(page_name, post_url_clean, post_text)
                        self.db.mark_seen(post_id, page_url, post_url_clean)
                        new_posts += 1
                        time.sleep(0.5)

                    except StaleElementReferenceException:
                        continue
                    except Exception as e:
                        self.log(f"⚠️ ข้ามโพสต์ที่อ่านไม่ได้: {type(e).__name__}")
                        continue

                # If this scroll round added zero new articles, feed has ended
                if not new_in_this_round:
                    self.log(f"📄 ไม่มีโพสต์ใหม่ให้โหลดแล้วบนเพจ {page_name} — จบการสแกน")
                    break

            self.log(
                f"📊 สแกนเพจ {page_name} เสร็จ | "
                f"Scroll {scroll_rounds} รอบ | โพสต์ใหม่: {new_posts}"
            )

        except WebDriverException as e:
            self.log(f"❌ WebDriver Error ที่เพจ {page_name}: {e}")
        except Exception as e:
            self.log(f"❌ Error scraping {page_name}: {e}")

        return new_posts

    # ── Main Run Loop ─────────────────────────────────────────────────────────

    def run(
        self,
        email: str,
        password: str,
        page_urls: list[str],
        keywords: list[str],
        hours_back: int,
        loop_minutes: int,
    ):
        """
        Main entry point for the scraper thread.
        Handles browser startup, login, scraping loop, and sleep between cycles.
        """
        try:
            self._start_browser()

            # Try to restore session; if fails, do fresh login
            if not self._load_cookies():
                self.log("🔑 ไม่มี Session เดิม — เริ่มล็อกอินใหม่")
                if not self.login(email, password):
                    # login() already handled pause/resume — if we reach here it
                    # truly failed and user pressed Stop, so just exit cleanly.
                    self.log("❌ Login ล้มเหลว — หยุดทำงาน (Browser ยังเปิดอยู่)")
                    self._stop_event.set()
                    return

            self.discord.send_start()

            while not self._stop_event.is_set():
                cycle_start = time.time()
                self.log(f"\n{'='*50}")
                self.log(f"🔄 เริ่มรอบสแกนใหม่ | {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")

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
                    # Random pause between pages (2-5 seconds)
                    if not self._stop_event.is_set():
                        time.sleep(2 + (time.time() % 3))

                if self._stop_event.is_set():
                    break

                duration = time.time() - cycle_start
                self.log(f"✅ รอบสแกนเสร็จ | พบโพสต์ใหม่รวม: {total_new}")
                self.discord.send_cycle_complete(duration, loop_minutes)

                # ── Sleep loop ────────────────────────────────────────────────
                # Use small sleep increments to remain responsive to stop signals
                sleep_total = loop_minutes * 60
                sleep_step = 5  # Check stop every 5 seconds
                elapsed = 0
                while elapsed < sleep_total and not self._stop_event.is_set():
                    remaining = sleep_total - elapsed
                    if elapsed % 60 == 0:  # Log every minute
                        self.log(f"⏳ รอ {int(remaining // 60)}m {int(remaining % 60)}s ก่อนรอบถัดไป...")
                    time.sleep(min(sleep_step, remaining))
                    elapsed += sleep_step

        except Exception as e:
            self.log(f"❌ Fatal Error ใน Scraper Thread: {e}")
        finally:
            self.discord.send_stopped()
            # Only close the browser if the user explicitly pressed Stop.
            # If we ended due to a login error, keep browser open so the
            # user can inspect what happened.
            if self._stop_event.is_set():
                self.log("🛑 Scraper Thread สิ้นสุด — กำลังปิด Browser...")
                try:
                    if self.driver:
                        self.driver.quit()
                except Exception:
                    pass
            else:
                self.log("🛑 Scraper Thread สิ้นสุด — Browser ยังเปิดอยู่ (ตรวจสอบได้)")

    def stop(self):
        """Signal the scraper thread to stop gracefully."""
        self._stop_event.set()
        self._resume_event.set()  # Unblock if paused



# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD TAG INPUT WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class KeywordTagInput(ctk.CTkFrame):
    """
    A wide, rectangular tag/chip input widget.
    - Tags are displayed as removable chips in a wrapping flow layout.
    - Press Enter or click ＋ to add a new keyword.
    - Click ✕ on a chip to remove it.
    - get_keywords() returns the current list as List[str].
    """

    # Chip colours (dark-theme friendly)
    CHIP_BG      = "#1e3a5f"   # deep navy
    CHIP_FG      = "#7ec8f0"   # sky-blue text
    CHIP_BTN_FG  = "#5ba4cf"
    CHIP_HOVER   = "#2a4f7a"

    def __init__(self, master, defaults: list | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self._tags: list[str] = []
        self._chip_widgets: dict[str, ctk.CTkFrame] = {}   # tag → frame

        self._build()
        for kw in (defaults or []):
            self._add_tag(kw)

    def _build(self):
        """Build the chip area (horizontal-scrollable canvas) + input row."""
        import tkinter as tk

        self.grid_columnconfigure(0, weight=1)

        # ── Chip area: Canvas + horizontal scrollbar ──────────────────────────
        # Canvas lets us keep all chips on ONE row and scroll sideways.
        chip_outer = ctk.CTkFrame(self, fg_color=("gray85", "#1a1a2e"), corner_radius=8)
        chip_outer.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        chip_outer.grid_columnconfigure(0, weight=1)

        # Canvas — fixed height so the widget stays compact
        self._canvas = tk.Canvas(
            chip_outer,
            bg="#1a1a2e", bd=0, highlightthickness=0,
            height=46,
        )
        self._canvas.pack(fill="x", expand=True, padx=4, pady=(4, 0))

        # Horizontal scrollbar (thin, styled dark)
        self._hbar = tk.Scrollbar(
            chip_outer, orient="horizontal",
            command=self._canvas.xview,
        )
        self._hbar.pack(fill="x", padx=4, pady=(0, 4))
        self._canvas.configure(xscrollcommand=self._hbar.set)

        # Inner frame placed inside canvas — chips pack left inside it
        self._chip_area = tk.Frame(self._canvas, bg="#1a1a2e")
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._chip_area, anchor="nw"
        )

        # Resize canvas scroll region whenever chips are added/removed
        self._chip_area.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")
            )
        )
        # Mouse-wheel horizontal scroll
        self._canvas.bind("<MouseWheel>",
            lambda e: self._canvas.xview_scroll(int(-e.delta / 60), "units"))
        self._canvas.bind("<Button-4>",
            lambda e: self._canvas.xview_scroll(-1, "units"))
        self._canvas.bind("<Button-5>",
            lambda e: self._canvas.xview_scroll(1, "units"))

        # ── Input row ─────────────────────────────────────────────────────────
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
            input_row,
            text="＋ เพิ่ม",
            width=80, height=34,
            fg_color="#1877F2", hover_color="#145db8",
            command=self._on_add,
        )
        add_btn.grid(row=0, column=1)

        clear_btn = ctk.CTkButton(
            input_row,
            text="ล้างทั้งหมด",
            width=90, height=34,
            fg_color="gray30", hover_color="gray20",
            command=self._clear_all,
        )
        clear_btn.grid(row=0, column=2, padx=(6, 0))

    def _on_add(self):
        raw = self._entry_var.get().strip()
        if not raw:
            return
        # Allow comma-separated paste: "ข่าว, ด่วน, สำคัญ"
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
        """Create a chip Frame inside _chip_area with pack(side=LEFT, wrap)."""
        import tkinter as tk

        chip = tk.Frame(
            self._chip_area,
            bg=self.CHIP_BG,
            bd=0,
            padx=6, pady=3,
        )
        chip.pack(side="left", padx=3, pady=3)

        lbl = tk.Label(
            chip, text=text,
            bg=self.CHIP_BG, fg=self.CHIP_FG,
            font=("Segoe UI", 10),
        )
        lbl.pack(side="left")

        def remove():
            self._remove_tag(text, chip)

        btn = tk.Label(
            chip, text=" ✕",
            bg=self.CHIP_BG, fg=self.CHIP_BTN_FG,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda e: remove())
        btn.bind("<Enter>", lambda e: btn.config(fg="#ff6b6b"))
        btn.bind("<Leave>", lambda e: btn.config(fg=self.CHIP_BTN_FG))

        self._chip_widgets[text] = chip
        # Auto-scroll right so new chip is always visible
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
    """
    Main GUI window built with CustomTkinter.
    All heavy work runs in a background thread to keep the UI responsive.
    """

    def __init__(self):
        super().__init__()

        # ── Window setup ──────────────────────────────────────────────────────
        self.title("📘 Facebook News Scraper & Discord Notifier")
        self.geometry("820x880")
        self.resizable(True, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._db = DatabaseManager()
        self._scraper: FacebookScraper | None = None
        self._scraper_thread: threading.Thread | None = None
        self._log_queue: Queue = Queue()

        self._build_ui()
        self._poll_log_queue()  # Start polling log updates

    # ── UI Construction ───────────────────────────────────────────────────────

    def _section_label(self, parent, text: str) -> ctk.CTkLabel:
        """Helper to create a bold section header label."""
        lbl = ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=13, weight="bold"))
        lbl.pack(anchor="w", pady=(12, 4))
        return lbl

    def _build_ui(self):
        """Construct all GUI widgets."""
        # Outer scrollable frame so it works on smaller screens
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        outer = ctk.CTkScrollableFrame(self, label_text="")
        outer.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = ctk.CTkLabel(
            outer,
            text="📘 Facebook News Scraper & Discord Notifier",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        title_lbl.pack(pady=(10, 4))

        subtitle_lbl = ctk.CTkLabel(
            outer,
            text="Automated keyword-based post monitoring with Discord alerts",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        subtitle_lbl.pack(pady=(0, 10))

        # ── Section 1: Facebook Credentials ──────────────────────────────────
        cred_frame = ctk.CTkFrame(outer)
        cred_frame.pack(fill="x", padx=6, pady=4)

        self._section_label(cred_frame, "🔐 Section 1 — Facebook Credentials")

        ctk.CTkLabel(cred_frame, text="Email:").pack(anchor="w", padx=12)
        self.email_var = ctk.StringVar()
        self.email_entry = ctk.CTkEntry(
            cred_frame, textvariable=self.email_var, placeholder_text="your@email.com", width=400
        )
        self.email_entry.pack(anchor="w", padx=12, pady=(0, 6))

        ctk.CTkLabel(cred_frame, text="Password:").pack(anchor="w", padx=12)
        self.pass_var = ctk.StringVar()
        self.pass_entry = ctk.CTkEntry(
            cred_frame, textvariable=self.pass_var, show="●", placeholder_text="รหัสผ่าน", width=400
        )
        self.pass_entry.pack(anchor="w", padx=12, pady=(0, 10))

        # ── Section 2: Target Settings ────────────────────────────────────────
        target_frame = ctk.CTkFrame(outer)
        target_frame.pack(fill="x", padx=6, pady=4)

        self._section_label(target_frame, "🎯 Section 2 — Target Settings")

        ctk.CTkLabel(target_frame, text="URL เพจเป้าหมาย (แต่ละบรรทัด = 1 เพจ):").pack(anchor="w", padx=12)
        self.pages_textbox = ctk.CTkTextbox(target_frame, height=100)
        self.pages_textbox.pack(fill="x", padx=12, pady=(0, 6))
        self.pages_textbox.insert("1.0", "https://www.facebook.com/BBCnewsThai\nhttps://www.facebook.com/voathai")

        ctk.CTkLabel(
            target_frame,
            text="🔑 Keywords — พิมพ์แล้วกด Enter หรือ ＋ | รองรับ #แฮชแท็ก | วางหลายคำคั่น , ได้เลย",
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=12)
        self.keywords_widget = KeywordTagInput(
            target_frame,
            defaults=["เพื่อไทย","แพทองธาร","ทักษิณ","เศรษฐา","พรรคเพื่อไทย","อนุทิน","นายก","จุลพันธ์","#ข่าวการเมือง","#เพื่อไทย","อว","ยศชนัน","รองนายกรัฐมนตรี","นายยศชนัน","เชน","อ.เชน"],
        )
        self.keywords_widget.pack(fill="x", padx=8, pady=(0, 10))

        # ── Section 3: Timeframe & Loop ───────────────────────────────────────
        time_frame = ctk.CTkFrame(outer)
        time_frame.pack(fill="x", padx=6, pady=4)

        self._section_label(time_frame, "⏱️ Section 3 — Timeframe & Loop Settings")

        row3 = ctk.CTkFrame(time_frame, fg_color="transparent")
        row3.pack(fill="x", padx=12)

        ctk.CTkLabel(row3, text="ดึงโพสต์ย้อนหลัง (ชั่วโมง):").grid(row=0, column=0, padx=(0, 10), pady=4, sticky="w")
        self.hours_var = ctk.StringVar(value="6")
        self.hours_entry = ctk.CTkEntry(row3, textvariable=self.hours_var, width=80)
        self.hours_entry.grid(row=0, column=1, pady=4, sticky="w")

        ctk.CTkLabel(row3, text="   วนลูปทุก (นาที):").grid(row=0, column=2, padx=(20, 10), pady=4, sticky="w")
        self.loop_var = ctk.StringVar(value="30")
        self.loop_entry = ctk.CTkEntry(row3, textvariable=self.loop_var, width=80)
        self.loop_entry.grid(row=0, column=3, pady=4, sticky="w")

        # ── Section 4: Discord Webhook ────────────────────────────────────────
        discord_frame = ctk.CTkFrame(outer)
        discord_frame.pack(fill="x", padx=6, pady=4)

        self._section_label(discord_frame, "💬 Section 4 — Discord Webhook")

        ctk.CTkLabel(discord_frame, text="Webhook URL:").pack(anchor="w", padx=12)
        self.webhook_var = ctk.StringVar()
        self.webhook_entry = ctk.CTkEntry(
            discord_frame, textvariable=self.webhook_var,
            placeholder_text="https://discord.com/api/webhooks/...", width=560
        )
        self.webhook_entry.pack(anchor="w", padx=12, pady=(0, 10))

        # ── Section 5: Control Buttons ────────────────────────────────────────
        ctrl_frame = ctk.CTkFrame(outer)
        ctrl_frame.pack(fill="x", padx=6, pady=4)

        self._section_label(ctrl_frame, "🎮 Section 5 — Controls")

        btn_row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        btn_row.pack(padx=12, pady=(0, 10))

        self.start_btn = ctk.CTkButton(
            btn_row, text="▶ Start", width=160, height=44,
            fg_color="#1877F2", hover_color="#145db8",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._on_start,
        )
        self.start_btn.grid(row=0, column=0, padx=6)

        self.stop_btn = ctk.CTkButton(
            btn_row, text="⏹ Stop", width=160, height=44,
            fg_color="#E53935", hover_color="#b71c1c",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._on_stop,
            state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, padx=6)

        self.resume_btn = ctk.CTkButton(
            btn_row, text="▶▶ Resume (หลัง Checkpoint/2FA)",
            width=260, height=44,
            fg_color="#FF8F00", hover_color="#e65100",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_resume,
            state="disabled",
        )
        self.resume_btn.grid(row=0, column=2, padx=6)

        self.hide_browser_btn = ctk.CTkButton(
            btn_row,
            text="🙈 ซ่อน Browser",
            width=160, height=44,
            fg_color="#4A148C", hover_color="#6A1FBF",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_hide_browser,
            state="disabled",  # Enabled only after cookies saved
        )
        self.hide_browser_btn.grid(row=0, column=3, padx=6)

        # Tooltip-style hint label under the hide button
        self._hide_hint_lbl = ctk.CTkLabel(
            ctrl_frame,
            text="ปุ่มซ่อน Browser จะเปิดใช้งานได้หลังจากบันทึก Cookies สำเร็จ",
            font=ctk.CTkFont(size=10),
            text_color="gray50",
        )
        self._hide_hint_lbl.pack(pady=(0, 2))

        # Status indicator
        self.status_lbl = ctk.CTkLabel(
            ctrl_frame, text="● หยุดทำงาน",
            text_color="#E53935",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_lbl.pack(pady=(0, 8))

        # ── Section 6: Log Area ───────────────────────────────────────────────
        log_frame = ctk.CTkFrame(outer)
        log_frame.pack(fill="both", expand=True, padx=6, pady=4)

        self._section_label(log_frame, "📋 Section 6 — Real-time Log")

        self.log_textbox = ctk.CTkTextbox(
            log_frame, height=260, state="disabled",
            font=ctk.CTkFont(family="Courier New", size=11),
        )
        self.log_textbox.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        clear_btn = ctk.CTkButton(
            log_frame, text="🗑 ล้าง Log", width=120, height=28,
            fg_color="gray30", hover_color="gray20",
            command=self._clear_log,
        )
        clear_btn.pack(anchor="e", padx=12, pady=(0, 10))

    # ── Log handling ──────────────────────────────────────────────────────────

    def _log(self, message: str):
        """Thread-safe: queue a log message to be written in the GUI thread."""
        self._log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _poll_log_queue(self):
        """Drain the log queue and update the TextBox. Re-schedules itself every 100ms."""
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_textbox.configure(state="normal")
                self.log_textbox.insert("end", msg + "\n")
                self.log_textbox.see("end")  # Auto-scroll to bottom
                self.log_textbox.configure(state="disabled")
        except Empty:
            pass
        self.after(100, self._poll_log_queue)  # Schedule next poll

    def _clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    # ── Button Handlers ───────────────────────────────────────────────────────

    def _on_start(self):
        """Validate inputs and start the scraper thread."""
        email = self.email_var.get().strip()
        password = self.pass_var.get().strip()
        pages_raw = self.pages_textbox.get("1.0", "end").strip()
        keywords_raw = None  # Not used — replaced by tag widget
        webhook = self.webhook_var.get().strip()

        try:
            hours = int(self.hours_var.get().strip())
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

        keywords = self.keywords_widget.get_keywords()

        if not webhook:
            self._show_error("⚠️ กรุณากรอก Discord Webhook URL")
            return

        # Build helper objects
        discord = DiscordNotifier(webhook)
        self._scraper = FacebookScraper(self._log, self._db, discord, on_cookies_saved=self._enable_hide_btn)

        # Update UI state
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.resume_btn.configure(state="normal")
        self.status_lbl.configure(text="● กำลังทำงาน...", text_color="#4CAF50")

        self._log(f"🚀 เริ่มทำงาน | เพจ: {len(page_urls)} | Keywords: {keywords or 'ทั้งหมด'} | Loop: {loop_min}m")

        # Launch scraper in a daemon thread so it doesn't block app exit
        self._scraper_thread = threading.Thread(
            target=self._scraper.run,
            args=(email, password, page_urls, keywords, hours, loop_min),
            daemon=True,
            name="ScraperThread",
        )
        self._scraper_thread.start()

        # Watch for thread completion to reset UI
        self.after(500, self._check_thread_alive)

    def _on_stop(self):
        """Signal the scraper to stop and update UI."""
        if self._scraper:
            self._scraper.stop()
        self._log("🛑 ส่งสัญญาณหยุด Scraper แล้ว...")
        self._reset_ui()

    def _on_resume(self):
        """Resume the scraper after a manual checkpoint fix."""
        if self._scraper:
            self._scraper.resume()
            self._log("▶️ กด Resume แล้ว — Scraper กลับมาทำงาน")

    def _enable_hide_btn(self):
        """Called (via callback) when cookies are saved — unlock the hide button."""
        self.hide_browser_btn.configure(state="normal")
        self._hide_hint_lbl.configure(
            text="✅ Cookies บันทึกแล้ว — กด 🙈 ซ่อน Browser ได้เลย",
            text_color="#4CAF50",
        )

    def _on_hide_browser(self):
        """Minimise the Chrome window managed by the scraper."""
        if self._scraper and self._scraper.driver:
            try:
                self._scraper.driver.minimize_window()
                self._log("🙈 ซ่อน Browser แล้ว (Minimised)")
                self.hide_browser_btn.configure(
                    text="👁 แสดง Browser",
                    command=self._on_show_browser,
                )
            except Exception as e:
                self._log(f"⚠️ ซ่อน Browser ไม่สำเร็จ: {e}")

    def _on_show_browser(self):
        """Restore the Chrome window."""
        if self._scraper and self._scraper.driver:
            try:
                self._scraper.driver.maximize_window()
                self._log("👁 แสดง Browser แล้ว")
                self.hide_browser_btn.configure(
                    text="🙈 ซ่อน Browser",
                    command=self._on_hide_browser,
                )
            except Exception as e:
                self._log(f"⚠️ แสดง Browser ไม่สำเร็จ: {e}")

    def _check_thread_alive(self):
        """Poll every 500ms to detect when the scraper thread finishes."""
        if self._scraper_thread and not self._scraper_thread.is_alive():
            self._reset_ui()
            self._log("✅ Scraper Thread สิ้นสุดการทำงาน")
        else:
            self.after(500, self._check_thread_alive)

    def _reset_ui(self):
        """Return buttons to their default state."""
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.status_lbl.configure(text="● หยุดทำงาน", text_color="#E53935")
        # Reset hide browser button
        self.hide_browser_btn.configure(
            state="disabled", text="🙈 ซ่อน Browser",
            command=self._on_hide_browser,
        )
        self._hide_hint_lbl.configure(
            text="ปุ่มซ่อน Browser จะเปิดใช้งานได้หลังจากบันทึก Cookies สำเร็จ",
            text_color="gray50",
        )

    def _show_error(self, msg: str):
        """Log an error and show a popup dialog."""
        self._log(msg)
        dialog = ctk.CTkToplevel(self)
        dialog.title("⚠️ ข้อผิดพลาด")
        dialog.geometry("380x130")
        dialog.grab_set()
        ctk.CTkLabel(dialog, text=msg, wraplength=340).pack(pady=20)
        ctk.CTkButton(dialog, text="ตกลง", command=dialog.destroy).pack()

    # ── App lifecycle ─────────────────────────────────────────────────────────

    def on_close(self):
        """Clean up resources when the window is closed."""
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

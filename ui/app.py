"""
ui/app.py
━━━━━━━━━
ScraperApp — หน้าต่างหลัก CustomTkinter
ประกอบด้วย: Header, Status Bar, Settings Panel, Log Panel
"""

import threading
import time
import json
import os
from queue import Queue, Empty
from datetime import datetime

import customtkinter as ctk

from database import DatabaseManager
from notifiers import DiscordNotifier, TelegramNotifier, TelegramListener
from ai_analyzer import ClaudeAnalyzer
from sheets_manager import GoogleSheetsManager
from scraper import FacebookScraper
from ui.widgets import KeywordTagInput


class ScraperApp(ctk.CTk):
    SETTINGS_FILE = "scraper_settings.json"
    PAGES_FILE    = "scraper_pages.json"
    KEYWORDS_FILE = "scraper_keywords.json"

    def __init__(self):
        super().__init__()
        self.title("📘 Facebook News Scraper  |  Discord & Telegram Notifier")
        self.geometry("1420x860")
        self.minsize(1100, 650)
        self.resizable(True, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._db: DatabaseManager              = DatabaseManager()
        self._scraper: FacebookScraper | None  = None
        self._scraper_thread: threading.Thread | None = None
        self._log_queue: Queue                 = Queue()
        self._session_start_time: float | None = None
        self._session_posts_found: int         = 0

        self._build_ui()
        self._load_settings()
        self._load_pages()
        self._load_keywords()
        self._poll_log_queue()
        self._update_timer()

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _section_card(self, parent, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=("gray92", "#161b22"), corner_radius=8,
                            border_width=1, border_color="#30363d")
        card.pack(fill="x", padx=10, pady=8)
        hdr = ctk.CTkFrame(card, fg_color="transparent", height=30)
        hdr.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(
            hdr, text=title,
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#58a6ff",
        ).pack(side="left")
        content = ctk.CTkFrame(card, fg_color="transparent")
        content.pack(fill="x", padx=12, pady=(10, 12))
        return content

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color="#0d1f3c", height=56, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header, text="📘  Facebook News Scraper",
            font=ctk.CTkFont(size=16, weight="bold"), text_color="white",
        ).grid(row=0, column=0, padx=16, pady=10, sticky="w")

        ctk.CTkLabel(
            header, text="Discord & Telegram Notifier",
            font=ctk.CTkFont(size=11), text_color="#7d9ec0",
        ).grid(row=0, column=1, padx=4, sticky="w")

        btn_bar = ctk.CTkFrame(header, fg_color="transparent")
        btn_bar.grid(row=0, column=2, padx=10, sticky="e")

        self.start_btn = ctk.CTkButton(
            btn_bar, text="▶  Start", width=108, height=36,
            fg_color="#1877F2", hover_color="#145db8",
            font=ctk.CTkFont(size=13, weight="bold"), command=self._on_start,
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 5))

        self.stop_btn = ctk.CTkButton(
            btn_bar, text="⏹  Stop", width=108, height=36,
            fg_color="#c62828", hover_color="#8e0000",
            font=ctk.CTkFont(size=13, weight="bold"), command=self._on_stop, state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, padx=5)

        self.resume_btn = ctk.CTkButton(
            btn_bar, text="▶▶ Resume", width=140, height=36,
            fg_color="#e65100", hover_color="#bf360c",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._on_resume, state="disabled",
        )
        self.resume_btn.grid(row=0, column=2, padx=5)

        self.hide_browser_btn = ctk.CTkButton(
            btn_bar, text="🙈  ซ่อน Browser", width=130, height=36,
            fg_color="#4A148C", hover_color="#6A1FBF",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._on_hide_browser, state="disabled",
        )
        self.hide_browser_btn.grid(row=0, column=3, padx=5)

        self.save_cfg_btn = ctk.CTkButton(
            btn_bar, text="💾  บันทึก", width=100, height=36,
            fg_color="#1b5e20", hover_color="#2e7d32",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._save_settings,
        )
        self.save_cfg_btn.grid(row=0, column=4, padx=(5, 12))

        # ── Status Bar ────────────────────────────────────────────────────────
        status_bar = ctk.CTkFrame(self, fg_color="#161b22", height=36, corner_radius=0)
        status_bar.grid(row=1, column=0, sticky="ew")
        status_bar.grid_propagate(False)
        status_bar.grid_columnconfigure(1, weight=1)

        self.status_lbl = ctk.CTkLabel(
            status_bar, text="⬤  หยุดทำงาน",
            text_color="#E53935", font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_lbl.grid(row=0, column=0, padx=16, pady=6, sticky="w")

        stats_frame = ctk.CTkFrame(status_bar, fg_color="transparent")
        stats_frame.grid(row=0, column=1, padx=8, pady=4, sticky="w")

        self._timer_lbl = ctk.CTkLabel(
            stats_frame, text="⏱ 00:00:00",
            font=ctk.CTkFont(size=10), text_color="#7d9ec0",
        )
        self._timer_lbl.pack(side="left", padx=(0, 14))

        self._hide_hint_lbl = ctk.CTkLabel(
            stats_frame,
            text="💡 ปุ่มซ่อน Browser จะเปิดใช้ได้หลัง Cookies บันทึกสำเร็จ",
            font=ctk.CTkFont(size=10), text_color="gray50",
        )
        self._hide_hint_lbl.pack(side="left")

        # ── Main content ──────────────────────────────────────────────────────
        main = ctk.CTkFrame(self, fg_color="#0d1117", corner_radius=0)
        main.grid(row=2, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=0)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        # ── Left panel: Settings ──────────────────────────────────────────────
        left_scroll = ctk.CTkScrollableFrame(
            main, width=500,
            label_text="  ⚙️  การตั้งค่า",
            label_font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#0d1117",
        )
        left_scroll.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        left_scroll.grid_columnconfigure(0, weight=1)

        # Section 1: Credentials
        s1 = self._section_card(left_scroll, "🔐  Facebook Credentials")
        r1 = ctk.CTkFrame(s1, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(0, 12))
        r1.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(r1, text="Email",    font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))
        ctk.CTkLabel(r1, text="Password", font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 2))

        self.email_var = ctk.StringVar()
        ctk.CTkEntry(r1, textvariable=self.email_var, placeholder_text="your@email.com", height=36).grid(row=1, column=0, sticky="ew")

        self.pass_var = ctk.StringVar()
        ctk.CTkEntry(r1, textvariable=self.pass_var, show="●", placeholder_text="รหัสผ่าน", height=36).grid(row=1, column=1, sticky="ew", padx=(8, 0))

        # Section 2: Target Pages
        s2 = self._section_card(left_scroll, "🎯  Target Pages")
        s2_hdr = ctk.CTkFrame(s2, fg_color="transparent")
        s2_hdr.pack(fill="x", padx=12, pady=(0, 2))
        s2_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(s2_hdr, text="URL เพจเป้าหมาย  (แต่ละบรรทัด = 1 เพจ)",
                     font=ctk.CTkFont(size=11), text_color="gray70").grid(row=0, column=0, sticky="w")

        self._pages_file_lbl = ctk.CTkLabel(s2_hdr, text=f"📄 {self.PAGES_FILE}",
                                             font=ctk.CTkFont(size=9), text_color="#3d8b3d")
        self._pages_file_lbl.grid(row=0, column=1, sticky="e", padx=(4, 0))

        ctk.CTkButton(s2_hdr, text="💾 บันทึกเพจ", width=110, height=28,
                      fg_color="#1565C0", hover_color="#0d47a1",
                      font=ctk.CTkFont(size=11, weight="bold"),
                      command=self._save_pages).grid(row=0, column=2, sticky="e", padx=(6, 0))

        self.pages_textbox = ctk.CTkTextbox(s2, height=90, fg_color="#0d1117")
        self.pages_textbox.pack(fill="x", padx=12, pady=(4, 12))
        self.pages_textbox.insert("1.0", "https://www.facebook.com/BBCnewsThai\nhttps://www.facebook.com/voathai")

        # Section 3: Keywords
        s3 = self._section_card(left_scroll, "🔑  Keywords")
        s3_hdr = ctk.CTkFrame(s3, fg_color="transparent")
        s3_hdr.pack(fill="x", padx=12, pady=(0, 2))
        s3_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(s3_hdr, text="กด Enter หรือ ＋  |  รองรับ #แฮชแท็ก  |  วางหลายคำคั่น ,",
                     font=ctk.CTkFont(size=10), text_color="gray60").grid(row=0, column=0, sticky="w")

        self._kw_file_lbl = ctk.CTkLabel(s3_hdr, text=f"📄 {self.KEYWORDS_FILE}",
                                          font=ctk.CTkFont(size=9), text_color="#3d8b3d")
        self._kw_file_lbl.grid(row=0, column=1, sticky="e", padx=(4, 0))

        ctk.CTkButton(s3_hdr, text="💾 บันทึก Keywords", width=140, height=28,
                      fg_color="#1565C0", hover_color="#0d47a1",
                      font=ctk.CTkFont(size=11, weight="bold"),
                      command=self._save_keywords).grid(row=0, column=2, sticky="e", padx=(6, 0))

        self.keywords_widget = KeywordTagInput(s3, defaults=["เพื่อไทย", "แพทองธาร", "ทักษิณ", "เศรษฐา"])
        self.keywords_widget.pack(fill="x", padx=8, pady=(2, 12))

        # Section 4: Timeframe & Loop
        s4 = self._section_card(left_scroll, "⏱️  Timeframe & Loop")
        r4 = ctk.CTkFrame(s4, fg_color="transparent")
        r4.pack(fill="x", padx=12, pady=(0, 12))
        r4.grid_columnconfigure((1, 3), weight=0)

        ctk.CTkLabel(r4, text="ดึงย้อนหลัง (ชั่วโมง)", font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.hours_var = ctk.StringVar(value="6")
        ctk.CTkEntry(r4, textvariable=self.hours_var, width=72, height=36).grid(row=1, column=0, sticky="w")

        ctk.CTkLabel(r4, text="วนลูปทุก (นาที)", font=ctk.CTkFont(size=11)).grid(row=0, column=2, sticky="w", padx=(20, 0), pady=(0, 2))
        self.loop_var = ctk.StringVar(value="30")
        ctk.CTkEntry(r4, textvariable=self.loop_var, width=72, height=36).grid(row=1, column=2, sticky="w", padx=(20, 0))

        # Section 5: Discord
        s5 = self._section_card(left_scroll, "💬  Discord Webhook  (Optional)")
        s5_hdr = ctk.CTkFrame(s5, fg_color="transparent")
        s5_hdr.pack(fill="x", padx=12)
        s5_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(s5_hdr, text="Webhook URL", font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(s5_hdr, text="🧪 ทดสอบ", width=90, height=26,
                      fg_color="#37474F", hover_color="#546E7A",
                      font=ctk.CTkFont(size=10, weight="bold"),
                      command=self._test_discord).grid(row=0, column=1, sticky="e")
        self.webhook_var = ctk.StringVar()
        ctk.CTkEntry(s5, textvariable=self.webhook_var,
                     placeholder_text="https://discord.com/api/webhooks/...", height=36).pack(fill="x", padx=12, pady=(4, 12))

        # Section 6: Telegram
        s6 = self._section_card(left_scroll, "✈️  Telegram Bot  (Optional)")
        s6_hdr = ctk.CTkFrame(s6, fg_color="transparent")
        s6_hdr.pack(fill="x", padx=12)
        s6_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(s6_hdr, text="Bot Token & Chat ID", font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(s6_hdr, text="🧪 ทดสอบ", width=90, height=26,
                      fg_color="#37474F", hover_color="#546E7A",
                      font=ctk.CTkFont(size=10, weight="bold"),
                      command=self._test_telegram).grid(row=0, column=1, sticky="e")
        r6 = ctk.CTkFrame(s6, fg_color="transparent")
        r6.pack(fill="x", padx=12, pady=(4, 12))
        r6.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(r6, text="Bot Token", font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))
        ctk.CTkLabel(r6, text="Chat ID",   font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 2))
        self.tg_token_var = ctk.StringVar()
        ctk.CTkEntry(r6, textvariable=self.tg_token_var, placeholder_text="123456789:ABCdef...", height=36).grid(row=1, column=0, sticky="ew")
        self.tg_chatid_var = ctk.StringVar()
        ctk.CTkEntry(r6, textvariable=self.tg_chatid_var, placeholder_text="-100123456789", height=36).grid(row=1, column=1, sticky="ew", padx=(8, 0))

        # Section 7: AI & Sheets
        s7 = self._section_card(left_scroll, "🤖  AI Analysis & Google Sheets")
        r7_1 = ctk.CTkFrame(s7, fg_color="transparent")
        r7_1.pack(fill="x", padx=12, pady=(0, 6))
        r7_1.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(r7_1, text="Claude API Key",       font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(r7_1, text="ชื่อ Google Sheet",   font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.claude_key_var = ctk.StringVar()
        ctk.CTkEntry(r7_1, textvariable=self.claude_key_var, show="●", placeholder_text="sk-ant-...", height=36).grid(row=1, column=0, sticky="ew")
        self.sheet_name_var = ctk.StringVar()
        ctk.CTkEntry(r7_1, textvariable=self.sheet_name_var, placeholder_text="ชื่อไฟล์ใน Google Drive", height=36).grid(row=1, column=1, sticky="ew", padx=(8, 0))

        r7_2 = ctk.CTkFrame(s7, fg_color="transparent")
        r7_2.pack(fill="x", padx=12, pady=(0, 6))
        r7_2.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(r7_2, text="Path ไฟล์ Service Account (.json)", font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        self.sa_path_var = ctk.StringVar()
        ctk.CTkEntry(r7_2, textvariable=self.sa_path_var, placeholder_text="C:/path/to/credentials.json", height=36).grid(row=1, column=0, sticky="ew")

        ctk.CTkLabel(s7, text="AI Prompt (คำสั่งให้ Claude ทำงาน)", font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=12, pady=(6, 2))
        self.prompt_textbox = ctk.CTkTextbox(s7, height=180, fg_color="#1a1a2e", font=ctk.CTkFont(size=11))
        self.prompt_textbox.pack(fill="x", padx=12, pady=(0, 12))
        self.prompt_textbox.insert("1.0", (
            'คุณคือผู้ช่วยบรรณาธิการข่าวการเมืองไทย จงวิเคราะห์ข่าวต่อไปนี้ว่าเกี่ยวข้องกับ "พรรคเพื่อไทย" หรือไม่\n'
            'จงตอบกลับมาเป็นรูปแบบ JSON เท่านั้น:\n'
            '{\n'
            '  "is_target": true หรือ false,\n'
            '  "score": 1-10,\n'
            '  "persons": ["ชื่อบุคคล"],\n'
            '  "reason": "สรุปเหตุผลสั้นๆ"\n'
            '}'
        ))

        # ── Right panel: Log ──────────────────────────────────────────────────
        right = ctk.CTkFrame(main, fg_color="#161b22", corner_radius=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        log_hdr = ctk.CTkFrame(right, fg_color="#21262d", corner_radius=8)
        log_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        log_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_hdr, text="📋  Real-time Activity Log",
                     font=ctk.CTkFont(size=13, weight="bold"), text_color="#c9d1d9",
                     ).grid(row=0, column=0, padx=14, pady=10, sticky="w")

        ctk.CTkButton(log_hdr, text="🗑  ล้าง Log", width=96, height=30,
                      fg_color="#30363d", hover_color="#484f58",
                      font=ctk.CTkFont(size=11),
                      command=self._clear_log).grid(row=0, column=1, padx=10, pady=10)

        self.log_textbox = ctk.CTkTextbox(
            right, state="disabled",
            font=ctk.CTkFont(family="Courier New", size=11),
            fg_color="#0d1117", text_color="#c9d1d9",
            corner_radius=8,
        )
        self.log_textbox.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    # ── Settings persistence ──────────────────────────────────────────────────

    def _save_settings(self):
        settings = {
            "email":      self.email_var.get(),
            "password":   self.pass_var.get(),
            "hours":      self.hours_var.get(),
            "loop":       self.loop_var.get(),
            "webhook":    self.webhook_var.get(),
            "tg_token":   self.tg_token_var.get(),
            "tg_chatid":  self.tg_chatid_var.get(),
            "claude_key": self.claude_key_var.get(),
            "sheet_name": self.sheet_name_var.get(),
            "sa_path":    self.sa_path_var.get(),
            "ai_prompt":  self.prompt_textbox.get("1.0", "end").strip(),
        }
        try:
            with open(self.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
            self._log(f"💾 บันทึก Settings → {self.SETTINGS_FILE}")
        except Exception as e:
            self._show_error(f"⚠️ บันทึก Settings ไม่สำเร็จ: {e}")

    def _load_settings(self):
        if not os.path.exists(self.SETTINGS_FILE):
            return
        try:
            with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.email_var.set(s.get("email", ""))
            self.pass_var.set(s.get("password", ""))
            self.hours_var.set(s.get("hours", "6"))
            self.loop_var.set(s.get("loop", "30"))
            self.webhook_var.set(s.get("webhook", ""))
            self.tg_token_var.set(s.get("tg_token", ""))
            self.tg_chatid_var.set(s.get("tg_chatid", ""))
            self.claude_key_var.set(s.get("claude_key", ""))
            self.sheet_name_var.set(s.get("sheet_name", ""))
            self.sa_path_var.set(s.get("sa_path", ""))
            saved_prompt = s.get("ai_prompt", "")
            if saved_prompt:
                self.prompt_textbox.delete("1.0", "end")
                self.prompt_textbox.insert("1.0", saved_prompt)
            self._log(f"🔄 โหลด Settings ← {self.SETTINGS_FILE}")
        except Exception as e:
            self._log(f"⚠️ โหลด Settings ไม่สำเร็จ: {e}")

    def _save_pages(self):
        pages = [u.strip() for u in self.pages_textbox.get("1.0", "end").splitlines() if u.strip()]
        try:
            with open(self.PAGES_FILE, "w", encoding="utf-8") as f:
                json.dump({"pages": pages}, f, ensure_ascii=False, indent=4)
            self._log(f"💾 บันทึก {len(pages)} เพจ → {self.PAGES_FILE}")
            self._flash_saved(self._pages_file_lbl, f"✅ บันทึกแล้ว ({len(pages)} เพจ)")
        except Exception as e:
            self._show_error(f"⚠️ บันทึกเพจไม่สำเร็จ: {e}")

    def _load_pages(self):
        if not os.path.exists(self.PAGES_FILE):
            return
        try:
            with open(self.PAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            pages = data.get("pages", [])
            if pages:
                self.pages_textbox.delete("1.0", "end")
                self.pages_textbox.insert("1.0", "\n".join(pages))
            self._log(f"🔄 โหลด {len(pages)} เพจ ← {self.PAGES_FILE}")
        except Exception as e:
            self._log(f"⚠️ โหลดเพจไม่สำเร็จ: {e}")

    def _save_keywords(self):
        kws = self.keywords_widget.get_keywords()
        try:
            with open(self.KEYWORDS_FILE, "w", encoding="utf-8") as f:
                json.dump({"keywords": kws}, f, ensure_ascii=False, indent=4)
            self._log(f"💾 บันทึก {len(kws)} keywords → {self.KEYWORDS_FILE}")
            self._flash_saved(self._kw_file_lbl, f"✅ บันทึกแล้ว ({len(kws)} คำ)")
        except Exception as e:
            self._show_error(f"⚠️ บันทึก Keywords ไม่สำเร็จ: {e}")

    def _load_keywords(self):
        if not os.path.exists(self.KEYWORDS_FILE):
            return
        try:
            with open(self.KEYWORDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            kws = data.get("keywords", [])
            if kws:
                self.keywords_widget.set_keywords(kws)
            self._log(f"🔄 โหลด {len(kws)} keywords ← {self.KEYWORDS_FILE}")
        except Exception as e:
            self._log(f"⚠️ โหลด Keywords ไม่สำเร็จ: {e}")

    # ── Timer & feedback ──────────────────────────────────────────────────────

    def _update_timer(self):
        if self._session_start_time is not None:
            elapsed = int(time.time() - self._session_start_time)
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            s = elapsed % 60
            self._timer_lbl.configure(text=f"⏱ {h:02d}:{m:02d}:{s:02d}", text_color="#4CAF50")
        else:
            self._timer_lbl.configure(text="⏱ 00:00:00", text_color="#7d9ec0")
        self.after(1000, self._update_timer)

    def _flash_saved(self, label: ctk.CTkLabel, msg: str, duration_ms: int = 3000):
        original = label.cget("text")
        label.configure(text=msg, text_color="#4CAF50")
        self.after(duration_ms, lambda: label.configure(text=original, text_color="#3d8b3d"))

    # ── Test buttons ──────────────────────────────────────────────────────────

    def _test_discord(self):
        webhook = self.webhook_var.get().strip()
        if not webhook:
            self._show_error("⚠️ กรุณากรอก Webhook URL ก่อนทดสอบ")
            return
        def _do():
            try:
                d = DiscordNotifier(webhook)
                d.send_start(
                    page_count=len([u for u in self.pages_textbox.get("1.0", "end").splitlines() if u.strip()]),
                    keyword_count=len(self.keywords_widget.get_keywords()),
                    loop_min=int(self.loop_var.get() or "30"),
                    hours_back=int(self.hours_var.get() or "6"),
                )
                self._log("✅ ส่ง Test Discord สำเร็จ — ตรวจสอบใน Discord Channel")
            except Exception as e:
                self._log(f"❌ Test Discord ล้มเหลว: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _test_telegram(self):
        tg_token  = self.tg_token_var.get().strip()
        tg_chatid = self.tg_chatid_var.get().strip()
        if not tg_token or not tg_chatid:
            self._show_error("⚠️ กรุณากรอก Bot Token และ Chat ID ก่อนทดสอบ")
            return
        def _do():
            try:
                tg = TelegramNotifier(tg_token, tg_chatid)
                tg.send_start(
                    page_count=len([u for u in self.pages_textbox.get("1.0", "end").splitlines() if u.strip()]),
                    keyword_count=len(self.keywords_widget.get_keywords()),
                    loop_min=int(self.loop_var.get() or "30"),
                    hours_back=int(self.hours_var.get() or "6"),
                )
                self._log("✅ ส่ง Test Telegram สำเร็จ — ตรวจสอบใน Telegram Chat")
            except Exception as e:
                self._log(f"❌ Test Telegram ล้มเหลว: {e}")
        threading.Thread(target=_do, daemon=True).start()

    # ── Log ───────────────────────────────────────────────────────────────────

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

    # ── Controls ──────────────────────────────────────────────────────────────

    def _on_start(self):
        email     = self.email_var.get().strip()
        password  = self.pass_var.get().strip()
        pages_raw = self.pages_textbox.get("1.0", "end").strip()
        webhook   = self.webhook_var.get().strip()
        tg_token  = self.tg_token_var.get().strip()
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
            self._show_error("⚠️ กรุณากรอก Discord Webhook หรือ Telegram Bot Token + Chat ID อย่างใดอย่างหนึ่ง")
            return

        keywords = self.keywords_widget.get_keywords()
        discord  = DiscordNotifier(webhook)
        tg       = TelegramNotifier(tg_token, tg_chatid)

        claude_key  = self.claude_key_var.get().strip()
        ai_prompt   = self.prompt_textbox.get("1.0", "end").strip()
        sheet_name  = self.sheet_name_var.get().strip()
        sa_path     = self.sa_path_var.get().strip()

        ai_analyzer    = ClaudeAnalyzer(claude_key, ai_prompt, self._log) if claude_key else None
        sheets_manager = GoogleSheetsManager(sa_path, sheet_name, self._log) if sa_path and sheet_name else None

        self._scraper = FacebookScraper(
            self._log, self._db, discord, tg,
            ai_analyzer=ai_analyzer,
            sheets_manager=sheets_manager,
            on_cookies_saved=self._enable_hide_btn,
        )

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.resume_btn.configure(state="normal")
        self.status_lbl.configure(text="⬤  กำลังทำงาน...", text_color="#4CAF50")
        self._session_start_time = time.time()

        self._log(f"🚀 เริ่มทำงาน | เพจ: {len(page_urls)} | Keywords: {keywords or 'ทั้งหมด'} | Loop: {loop_min}m")

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
                drv.set_window_position(-2000, -2000)
                self._log("🙈 ซ่อน Browser ไปทำงานเบื้องหลังแล้ว")
                self.hide_browser_btn.configure(text="👁 แสดง Browser", command=self._on_show_browser)
            except Exception as e:
                self._log(f"⚠️ ซ่อน Browser ไม่สำเร็จ: {e}")

    def _on_show_browser(self):
        drv = self._scraper.driver if self._scraper else None
        if drv:
            try:
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
        self.status_lbl.configure(text="⬤  หยุดทำงาน", text_color="#E53935")
        self._session_start_time = None
        self.hide_browser_btn.configure(state="disabled", text="🙈 ซ่อน Browser", command=self._on_hide_browser)
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

"""
main.py
━━━━━━━
Entry point — รันโปรแกรมจากไฟล์นี้ไฟล์เดียว
"""
import os
import sys

def _fix_ssl_cert():
    """
    แก้ปัญหา SSL/certifi ผสมผสานรองรับทั้ง Source Code และ PyInstaller (.exe)
    """
    try:
        import certifi
        cert_path = certifi.where()

        # 1. เช็คก่อนว่าโปรแกรมรันผ่าน .exe (PyInstaller) หรือไม่
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # ถ้ารันเป็น .exe ให้ "ยอมรับ" และอ่านจากโฟลเดอร์ _MEIPASS 
            # (ต้องสั่ง build ด้วย --collect-data certifi)
            cert_path = os.path.join(sys._MEIPASS, 'certifi', 'cacert.pem')
            
        # 2. ถ้ารันแบบ Source Code ปกติ ค่อยใช้ลอจิกเดิมของคุณดักจับ
        else:
            if not os.path.isfile(cert_path) or "_MEI" in cert_path:
                import importlib.util
                spec = importlib.util.find_spec("certifi")
                if spec and spec.origin:
                    pkg_dir   = os.path.dirname(spec.origin)
                    real_cert = os.path.join(pkg_dir, "cacert.pem")
                    if os.path.isfile(real_cert):
                        cert_path = real_cert

        # ตรวจสอบขั้นสุดท้ายแล้วยัดค่ากลับเข้าระบบ
        if os.path.isfile(cert_path):
            os.environ["SSL_CERT_FILE"]    = cert_path
            os.environ["REQUESTS_CA_BUNDLE"] = cert_path
            # patch certifi.where() ให้คืน path ที่ถูกต้อง
            certifi.where = lambda: cert_path
            
    except Exception as e:
        print(f"[SSL] _fix_ssl_cert warning: {e}")

_fix_ssl_cert()

from ui.app import ScraperApp


if __name__ == "__main__":
    app = ScraperApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()

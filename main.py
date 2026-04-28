"""
main.py
━━━━━━━
Entry point — รันโปรแกรมจากไฟล์นี้ไฟล์เดียว
"""

import os
import sys


def _fix_ssl_cert():
    """
    แก้ปัญหา certifi ใน PyInstaller (_MEI...) หา cacert.pem ไม่เจอ
    ให้ชี้ไปที่ไฟล์จริงที่ certifi ติดตั้งไว้ใน Python environment
    """
    try:
        import certifi
        cert_path = certifi.where()

        # ถ้า path อยู่ใน temp folder ของ PyInstaller → ใช้ไม่ได้
        if not os.path.isfile(cert_path) or "_MEI" in cert_path:
            # หา cacert.pem จาก site-packages โดยตรง
            import importlib.util
            spec = importlib.util.find_spec("certifi")
            if spec and spec.origin:
                pkg_dir   = os.path.dirname(spec.origin)
                real_cert = os.path.join(pkg_dir, "cacert.pem")
                if os.path.isfile(real_cert):
                    cert_path = real_cert

        if os.path.isfile(cert_path) and "_MEI" not in cert_path:
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

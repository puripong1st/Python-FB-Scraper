"""
ssl_helper.py
━━━━━━━━━━━━━
Helper สำหรับดึง CA bundle path ที่ใช้งานได้จริง
รองรับทั้งโหมด Source Code และ PyInstaller exe
"""
import os


def get_ca_bundle() -> str | bool:
    """
    คืนค่า path ของ CA bundle ที่ใช้งานได้จริง
    - ถ้าหาเจอ → คืน path string
    - ถ้าหาไม่เจอ → คืน True (ให้ requests ใช้ system certs)
    
    ลำดับความสำคัญ:
    1. SSL_CERT_FILE env var (set โดย main.py _fix_ssl_cert)
    2. REQUESTS_CA_BUNDLE env var
    3. certifi.where() ที่ไม่ใช่ _MEI temp path
    4. True (system certs fallback)
    """
    # 1. Check env vars ก่อน (set โดย _fix_ssl_cert ใน main.py)
    for env_key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(env_key, "")
        if path and os.path.isfile(path) and "_MEI" not in path:
            return path

    # 2. ลอง certifi โดยตรง
    try:
        import certifi
        path = certifi.where()
        if path and os.path.isfile(path) and "_MEI" not in path:
            return path
    except Exception:
        pass

    # 3. Fallback: ให้ requests ใช้ system certs
    return True

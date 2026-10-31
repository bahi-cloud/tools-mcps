#!/usr/bin/env python3
"""
new_client.py — يولّد مفتاح API جديداً لمستخدم ويسجّل sha256(المفتاح) فقط في clients.json.

  python new_client.py alice            # يطبع المفتاح مرة واحدة + السطر المضاف
  python new_client.py alice --admin    # مستخدم admin (يستعمل run-code و composio_*)
  python new_client.py alice --write    # يضيفه إلى clients.json بجانب هذا الملف
  python new_client.py --disable alice  # يعطّل كل مفاتيح المستخدم (يتطلب --write)

المفتاح الخام يظهر هنا فقط — سلّمه للمستخدم (أو ضعه في backend الواجهة) ولا تحفظه في المستودع.
clients.json يحوي الـ hash فقط، فلا خطر من وجوده في مستودع قابل للقراءة.
إن فضّلت عدم رفع أي شيء للمستودع: ضع نفس البنية كسر HF باسم CLIENT_KEYS_JSON.
"""
import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

CLIENTS_FILE = Path(__file__).parent / "clients.json"


def load() -> dict:
    if CLIENTS_FILE.exists():
        return json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    return {"clients": {}}


def main() -> int:
    ap = argparse.ArgumentParser(description="توليد مفتاح مستخدم")
    ap.add_argument("user", help="اسم المستخدم (حروف/أرقام/شرطة)")
    ap.add_argument("--admin", action="store_true", help="صلاحية admin")
    ap.add_argument("--write", action="store_true", help="إضافة/تعديل clients.json")
    ap.add_argument("--disable", action="store_true", help="تعطيل مفاتيح هذا المستخدم")
    a = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9-]{1,24}", a.user):
        print("اسم المستخدم: حروف إنجليزية/أرقام/شرطة فقط وبحد أقصى 24 حرفاً", file=sys.stderr)
        return 2

    doc = load()
    clients = doc.setdefault("clients", {})

    if a.disable:
        n = 0
        for rec in clients.values():
            if rec.get("user") == a.user:
                rec["enabled"] = False
                n += 1
        if not a.write:
            print("أضف --write لتطبيق التعطيل", file=sys.stderr)
            return 2
        CLIENTS_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"عُطّل {n} مفتاح للمستخدم {a.user}")
        return 0

    key = "sk_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    entry = {"user": a.user, "enabled": True}
    if a.admin:
        entry["admin"] = True

    print("=" * 64)
    print(f"المستخدم : {a.user}{'  (admin)' if a.admin else ''}")
    print(f"المفتاح   : {key}")
    print("   ↑ يظهر مرة واحدة فقط — سلّمه للمستخدم الآن (الهيدر: X-API-Key)")
    print("=" * 64)
    print("السطر الذي يُضاف إلى clients.json  ->  \"clients\":")
    print(json.dumps({key_hash: entry}, ensure_ascii=False, indent=2))

    if a.write:
        clients[key_hash] = entry
        CLIENTS_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ كُتب في {CLIENTS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
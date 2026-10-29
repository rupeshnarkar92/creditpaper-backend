"""Compare individual endpoints vs mega-endpoint."""

import http.cookiejar
import json
import time
import urllib.request

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
data = json.dumps({"email": "udayshimpi@cylsy.com", "password": "Password@2"}).encode()
req = urllib.request.Request("http://127.0.0.1:8000/api/auth/login", data=data, method="POST")
req.add_header("Content-Type", "application/json")
opener.open(req, timeout=10).read()

deal_id = "deal_71a0102a07ab"
tid = "0a75adef-7688-4840-8717-cab09b0b6ee3"
base = "http://127.0.0.1:8000"

endpoints = [
    ("workspace", f"/api/deals/{deal_id}"),
    ("slots", f"/api/deals/{deal_id}/slots"),
    ("factsheet", f"/api/deals/{deal_id}/facts"),
    ("activity", f"/api/deals/{deal_id}/activity"),
    ("sections", f"/api/deals/{deal_id}/paper/sections"),
]

times = []
for name, path in endpoints:
    t0 = time.perf_counter()
    req = urllib.request.Request(f"{base}{path}")
    req.add_header("X-Tenant-ID", tid)
    r = opener.open(req, timeout=30)
    r.read()
    elapsed = time.perf_counter() - t0
    times.append(elapsed)
    print(f"  {name:12s}: {elapsed:.3f}s")

serial = sum(times)
print(f"  {'5-call serial':12s}: ~{serial:.1f}s estimated")

t0 = time.perf_counter()
req = urllib.request.Request(f"{base}/api/deals/{deal_id}/page")
req.add_header("X-Tenant-ID", tid)
r = opener.open(req, timeout=30)
r.read()
mega = time.perf_counter() - t0
print(f"  {'MEGA':12s}: {mega:.3f}s")
print(f"  Speedup: {serial / mega:.1f}x fewer round-trips")

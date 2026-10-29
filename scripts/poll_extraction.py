import json
import time

import requests

TENANT = "feb561c5-5cd7-4227-9b30-79ee347ab75a"
DEAL = "4f6c3e22-860b-49e9-9234-32ea82b80b91"
H = {"X-Tenant-ID": TENANT}

print("Polling extraction status...")
for i in range(60):
    time.sleep(15)
    r = requests.get(f"http://127.0.0.1:8000/api/deals/{DEAL}/extraction-status", headers=H, timeout=30)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, dict):
            docs = data.get("documents", data)
            if isinstance(docs, dict):
                statuses = [v.get("status", "?") for v in docs.values() if isinstance(v, dict)]
                completed = sum(1 for s in statuses if s in ("completed", "completed_with_warnings"))
                print(f"  [{i * 15}s] {completed}/{len(statuses)} completed")
                if completed >= len(statuses) and len(statuses) > 0:
                    break
            elif isinstance(docs, list):
                completed = sum(
                    1
                    for d in docs
                    if isinstance(d, dict) and d.get("status") in ("completed", "completed_with_warnings")
                )
                print(f"  [{i * 15}s] {completed}/{len(docs)} completed")
                if completed >= len(docs) and len(docs) > 0:
                    break
        print(f"  Raw: {json.dumps(data, indent=2, default=str)[:500]}")
    else:
        print(f"  [{i * 15}s] HTTP {r.status_code}: {r.text[:200]}")

print("\n--- Extraction Results ---")
r = requests.get(f"http://127.0.0.1:8000/api/deals/{DEAL}/extraction-status", headers=H, timeout=30)
print(json.dumps(r.json(), indent=2, default=str)[:3000])

print("\n--- Facts ---")
r = requests.get(f"http://127.0.0.1:8000/api/deals/{DEAL}/facts", headers=H, timeout=30)
if r.status_code == 200:
    facts = r.json()
    if isinstance(facts, list):
        print(f"Total facts: {len(facts)}")
        for f in facts[:30]:
            field = f.get("field_name", f.get("field", "?"))
            val = f.get("value", "?")
            conf = f.get("confidence", "?")
            print(f"  {field}: {val} (confidence={conf})")
    else:
        print(json.dumps(facts, indent=2, default=str)[:2000])
else:
    print(f"Error {r.status_code}: {r.text[:300]}")

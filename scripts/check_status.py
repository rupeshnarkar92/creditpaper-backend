import requests

TENANT = "feb561c5-5cd7-4227-9b30-79ee347ab75a"
DEAL = "4f6c3e22-860b-49e9-9234-32ea82b80b91"
H = {"X-Tenant-ID": TENANT}

r = requests.get(f"http://127.0.0.1:8000/api/deals/{DEAL}/workspace", headers=H, timeout=30)
ws = r.json()
print("Deal:", ws["name"])
print("State:", ws["current_state"])
print("Documents:", len(ws.get("documents", [])))
for d in ws.get("documents", []):
    name = d.get("display_name") or d.get("name") or "?"
    status = d.get("extraction_status", "none")
    print(f"  - {name} [{status}]")

print()
print("--- Triggering extraction ---")
r = requests.post(f"http://127.0.0.1:8000/api/deals/{DEAL}/extract", headers=H, timeout=120)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")

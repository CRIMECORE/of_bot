import os, sys, json
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()
import httpx

key = os.getenv("ONLYMONSTER_API_KEY")
h   = {"x-om-auth-token": key}
ACC = "10148"

fan_ids = ["12463839", "208007833", "85391040"]

# Try different URL patterns for messages
patterns = [
    "accounts/{acc}/chats/{fan}/messages?limit=3",
    "accounts/{acc}/fans/{fan}/messages?limit=3",
    "accounts/{acc}/fans/{fan}/chats?limit=3",
    "accounts/{acc}/fans/{fan}",
    "chats/{fan}/messages?limit=3",
    "accounts/{acc}/chats?limit=5",
    "accounts/{acc}/fans?limit=5",
]

print("=== Testing fans endpoint with params ===")
for p in ["", "?limit=5", "?limit=5&offset=0", "?with_details=true"]:
    url = f"https://omapi.onlymonster.ai/api/v0/accounts/{ACC}/fans{p}"
    r = httpx.get(url, headers=h, timeout=10)
    data = r.json()
    keys = list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]"
    print(f"  fans{p} → {r.status_code} keys={keys}")
    if r.status_code == 200 and isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict):
                    print(f"    {k}[0] keys: {list(first.keys())}")
                    print(f"    {k}[0]: {json.dumps(first, ensure_ascii=False)[:300]}")
                else:
                    print(f"    {k}: {v[:5]}")

print("\n=== Testing message URL patterns for fan 12463839 ===")
fan = fan_ids[0]
for pat in patterns:
    url = "https://omapi.onlymonster.ai/api/v0/" + pat.replace("{acc}", ACC).replace("{fan}", fan)
    try:
        r = httpx.get(url, headers=h, timeout=10)
        data = r.json()
        keys = list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]"
        print(f"  {pat} → {r.status_code} {keys}")
        if r.status_code == 200:
            print(f"    FOUND! data: {json.dumps(data, ensure_ascii=False)[:300]}")
    except Exception as e:
        print(f"  {pat} → ERROR: {e}")

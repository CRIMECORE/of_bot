import os, sys, json
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()
import httpx

key = os.getenv("ONLYMONSTER_API_KEY")
h   = {"x-om-auth-token": key}
ACC = "10148"

tests = [
    ("members limit+offset",
     f"https://omapi.onlymonster.ai/api/v0/members?limit=3&offset=0"),
    ("members with account_id",
     f"https://omapi.onlymonster.ai/api/v0/members?limit=3&offset=0&account_id={ACC}"),
    ("metrics from+to",
     f"https://omapi.onlymonster.ai/api/v0/users/metrics?from=2026-04-01&to=2026-05-11"),
]

for name, url in tests:
    try:
        r    = httpx.get(url, headers=h, timeout=15)
        data = r.json()
        print(f"=== {name} (HTTP {r.status_code}) ===")
        if isinstance(data, dict):
            print("  keys:", list(data.keys()))
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"  {k}: list[{len(v)}]")
                    if v and isinstance(v[0], dict):
                        print(f"    first keys: {list(v[0].keys())}")
                        print(f"    first: {json.dumps(v[0], ensure_ascii=False)[:500]}")
                    elif v:
                        print(f"    sample: {v[:3]}")
                else:
                    print(f"  {k}: {str(v)[:200]}")
        elif isinstance(data, list):
            print(f"  list[{len(data)}]")
            if data:
                print(f"  first: {json.dumps(data[0], ensure_ascii=False)[:500]}")
        print()
    except Exception as e:
        print(f"=== {name} ERROR: {e} ===\n")

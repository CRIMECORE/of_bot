import sys, logging, re
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")
from dotenv import load_dotenv; load_dotenv()
from onlymonster import OnlyMonsterClient

om = OnlyMonsterClient()

print("--- accounts ---")
accts = om.get_accounts()
for a in accts:
    print(f"  id={a['id']} platform={a['platform']} name={a.get('name')}")

acc_id = str(accts[0]["id"]) if accts else "10148"

print(f"\n--- fan_ids for account {acc_id} ---")
ids = om.get_fan_ids(acc_id)
print(f"  {len(ids)} fans, first 5: {ids[:5]}")

for fan_id in ids[:3]:
    print(f"\n--- messages for fan {fan_id} (limit=100) ---")
    try:
        msgs = om.get_messages(acc_id, fan_id, limit=100)
        print(f"  OK: {len(msgs)} messages")
        for m in msgs[:2]:
            role = "Fan" if not m.get("is_sent_by_me") else "Model"
            text = re.sub(r"<[^>]+>", "", m.get("text") or "").strip()
            print(f"  [{role}] {text[:80]}")
    except Exception as e:
        print(f"  ERROR: {e}")

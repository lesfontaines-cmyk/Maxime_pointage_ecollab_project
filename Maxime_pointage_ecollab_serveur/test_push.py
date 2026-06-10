#!/usr/bin/env python3
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pywebpush import webpush, WebPushException

keys_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vapid_keys.json')
subs_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'push_subscriptions.json')

if not os.path.exists(keys_file):
    print("NO VAPID KEYS FILE")
    sys.exit(1)
if not os.path.exists(subs_file):
    print("NO SUBSCRIPTIONS FILE")
    sys.exit(1)

keys = json.load(open(keys_file))
subs = json.load(open(subs_file))

if not subs:
    print("NO SUBSCRIPTIONS REGISTERED")
    sys.exit(1)

for email, sub in subs.items():
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps({"title": "Test deploy", "body": "Push OK depuis le serveur !"}),
            vapid_private_key=keys["privateKey"],
            vapid_claims={"sub": "mailto:" + email}
        )
        print("PUSH OK for", email)
    except WebPushException as e:
        resp = ""
        if hasattr(e, "response") and e.response:
            resp = str(e.response.status_code) + " " + str(e.response.text[:200])
        print("PUSH FAIL for", email, ":", e, resp)
    except Exception as e:
        print("ERROR for", email, ":", e)

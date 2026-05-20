import os
import urllib.request

url = os.environ["BOT_URL"] + "/cron/morning-report"
token = os.environ["CRON_SECRET"]

req = urllib.request.Request(url, method="POST")
req.add_header("Authorization", f"Bearer {token}")
urllib.request.urlopen(req)
print("Morning report triggered successfully")

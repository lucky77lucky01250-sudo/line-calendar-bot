import os
import urllib.request

url = os.environ["BOT_URL"] + "/"
urllib.request.urlopen(url, timeout=60)
print("Keepalive ping sent successfully")

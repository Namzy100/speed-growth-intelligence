from dotenv import load_dotenv
import os
from anthropic import Anthropic
from apify_client import ApifyClient
from supabase import create_client
import requests

load_dotenv()

# Test Anthropic
try:
    client = Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "say hi"}]
    )
    print("Anthropic: OK")
except Exception as e:
    print(f"Anthropic: FAILED - {e}")

# Test Apify
try:
    client = ApifyClient(os.getenv("APIFY_API_KEY"))
    user = client.user().get()
    print("Apify: OK")
except Exception as e:
    print(f"Apify: FAILED - {e}")

# Test Supabase
try:
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    print("Supabase: OK")
except Exception as e:
    print(f"Supabase: FAILED - {e}")

# Test YouTube
try:
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q=bitcoin+lightning&type=channel&key={os.getenv('YOUTUBE_API_KEY')}"
    r = requests.get(url)
    if r.status_code == 200:
        print(f"YouTube: OK - got {len(r.json().get('items', []))} results")
    else:
        print(f"YouTube: FAILED - {r.json()}")
except Exception as e:
    print(f"YouTube: FAILED - {e}")
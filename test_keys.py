from dotenv import load_dotenv
import os

load_dotenv()

print("Anthropic:", "OK" if os.getenv("ANTHROPIC_API_KEY") else "MISSING")
print("Apify:", "OK" if os.getenv("APIFY_API_KEY") else "MISSING")
print("Supabase URL:", "OK" if os.getenv("SUPABASE_URL") else "MISSING")
print("Supabase Key:", "OK" if os.getenv("SUPABASE_KEY") else "MISSING")
print("YouTube:", "OK" if os.getenv("YOUTUBE_API_KEY") else "MISSING")
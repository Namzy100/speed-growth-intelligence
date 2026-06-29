#!/bin/bash
cd /Users/namzysacc/Documents/Speed
source venv/bin/activate
export DASHBOARD_AUTODEPLOY=1
python pipelines/run_daily_sync.py
# Trigger Vercel redeploy
source .env 2>/dev/null || true
if [ -n "$VERCEL_DEPLOY_HOOK" ]; then
  curl -s -X POST "$VERCEL_DEPLOY_HOOK" > /dev/null
  echo "Vercel redeploy triggered."
fi

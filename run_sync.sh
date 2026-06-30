#!/bin/bash
cd /Users/namzysacc/Documents/Speed
source venv/bin/activate
export DASHBOARD_AUTODEPLOY=1
python pipelines/run_daily_sync.py

#!/bin/bash
cd /Users/namzysacc/Documents/Speed
source venv/bin/activate
export DASHBOARD_AUTODEPLOY=1
python pipelines/run_daily_sync.py

# Final step: evaluate all outputs and alert (email) if the overall score < 7.
python intelligence/agent_evaluator.py

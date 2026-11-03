#!/usr/bin/env bash
# One process group for Render's free web service: the worker in the background
# (restarted if it ever exits) and the dashboard in the foreground.
set -u
(while true; do python -m desk.cli run; echo "worker exited, restarting in 5 s"; sleep 5; done) &
exec streamlit run ui/app.py --server.port "${PORT:-8501}" --server.address 0.0.0.0

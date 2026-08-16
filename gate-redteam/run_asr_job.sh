#!/bin/bash
set -eo pipefail
cd /home/lumen/minerva/gate-redteam
. .venv/bin/activate
export ASR_SEEDS=3 ASR_ITERS=2 ASR_CALL_CAP=200
python asr_loop.py

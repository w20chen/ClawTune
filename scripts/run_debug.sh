#!/bin/bash
# Wrapper to run the debug script with proper conda + sudo
set -e
source ~/miniconda3/etc/profile.d/conda.sh
conda activate ML
export PYTHONPATH=/home/weitian/claw/services/scheduler/src
echo "3yq7T6Lq" | sudo -S "$(command -v python3)" /home/weitian/debug_cgroup.py 2>&1

#!/bin/bash
set -euo pipefail
cd /mnt/c/Users/29068/Desktop/ClawTune
for mode in before-clean after-clean; do
 for family in edge lattice flush; do
  echo "START $mode $family"
  bash research/kb_algorithm_acceleration_20261012/run.sh "$mode" "$family"
  echo "DONE $mode $family"
 done
done

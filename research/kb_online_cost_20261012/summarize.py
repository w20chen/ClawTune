"""Print only small, decision-relevant timing summaries."""
import json
from pathlib import Path
import sys

keys = ('model', 'limit', 'phase', 'elapsed_s', 'ready_s', 'duration_nodes',
        'graph_nodes', 'graph_edges', 'ready_rss_mib', 'peak_rss_mib',
        'read_only_combined', 'online_begin_plus_compose', 'online_complete',
        'cold_begin', 'cold_complete', 'updates', 'snapshot_capture_ms',
        'snapshot_json_ms', 'snapshot_write_fsync_ms', 'snapshot_bytes',
        'pair_baseline', 'pair_during_update', 'pair_during_update_lock_wait',
        'update_visible_s', 'update_durable_s', 'published_added_observations',
        'returncode', 'termination', 'wall_s')
for path in sorted(Path(sys.argv[1]).glob('*.json')):
    if path.name.endswith(('-trained.json', '-snapshot.json')) or path.name in {'environment.json', 'source_sha256.json'}:
        continue
    value = json.loads(path.read_bytes())
    print(path.name, json.dumps({k: value[k] for k in keys if k in value}))

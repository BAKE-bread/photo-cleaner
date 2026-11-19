"""Distinguish untouched reference/output inventory from retained input files."""
STATUS_SQL="""CASE WHEN state='rejected' AND (json_extract(metrics,'$.is_std')=1 OR
    (json_extract(metrics,'$.in_output')=1 AND json_extract(metrics,'$.requested')=0))
    THEN 'existing' ELSE state END"""

def display_state(row,metrics):
    if row['state']=='rejected' and (metrics.get('is_std') or (metrics.get('in_output') and not metrics.get('requested',True))):return 'existing'
    return row['state']

def retained_counts(store,run):
    return {row[0]:row[1] for row in store.db.execute('SELECT '+STATUS_SQL+",COUNT(*) FROM files WHERE seen_run=? AND state='rejected' GROUP BY 1",(run,))}

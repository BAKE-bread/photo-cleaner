"""Managed group folders are transparent when classifying later additions."""
from pathlib import Path

MARKER='.photocleaner-group'

def managed(directory):
    return (directory/MARKER).is_file()

def logical_parent(path,root):
    root=Path(root);current=root;parts=[]
    for part in path.parent.relative_to(root).parts:
        current/=part
        if not managed(current):parts.append(part)
    return root.joinpath(*parts)

def group_directory(parent,category):
    directory=Path(parent)/('来源_'+category);n=1
    while directory.exists() and not managed(directory):
        directory=Path(parent)/('来源_'+category+'__'+str(n));n+=1
    return directory

def group_summary(store,run_id):
    where="seen_run=? AND json_extract(metrics,'$.method') IS NOT NULL"
    row=store.db.execute("""SELECT COUNT(DISTINCT json_extract(metrics,'$.parent')) directories,
        COUNT(DISTINCT json_extract(metrics,'$.method')) methods,
        COUNT(DISTINCT json_extract(metrics,'$.parent')||char(0)||json_extract(metrics,'$.category')) groups,
        SUM(CASE WHEN json_extract(metrics,'$.category')='其他' THEN 1 ELSE 0 END) other_images,
        COUNT(DISTINCT CASE WHEN json_extract(metrics,'$.category')='其他' THEN json_extract(metrics,'$.parent') END) other_groups
        FROM files WHERE """+where,(run_id,)).fetchone()
    return {k:(row[k] or 0) for k in row.keys()}

"""Private preview journals when no destination has been chosen."""
import hashlib,json,sqlite3,time
from pathlib import Path
from .config import PROJECT
from .storage import Store
from .moving import fingerprint

def preview_output(task,roots):
    key=hashlib.sha256(json.dumps([task,sorted(map(str,roots))]).encode()).hexdigest()[:20]
    return PROJECT/'.jobs'/'previews'/key/'output'


def _database_stamp(source):
    values=[]
    for path in (source,Path(str(source)+'-wal')):
        try:
            stat=path.stat()
            # A read-only WAL connection can create an empty sidecar. It has
            # no committed rows and must not invalidate an otherwise stable DB.
            values.append(None if path!=source and stat.st_size==0 else (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_dev,stat.st_ino))
        except FileNotFoundError:values.append(None)
    return json.dumps(values)

def import_decisions(database,metadata,scheme=None,evidence_key=None):
    source=Path(database)
    if not source.is_file() or source.resolve()==(Path(metadata)/'state.sqlite3').resolve():return
    Path(metadata).mkdir(parents=True,exist_ok=True);target=Store(Path(metadata)/'state.sqlite3')
    other=None;imported=0;stamp=_database_stamp(source)
    receipt_key=str(source.resolve())+'|'+(scheme or '*')+'|'+(evidence_key or '*')
    try:
        target.db.execute('CREATE TABLE IF NOT EXISTS preview_imports(source TEXT PRIMARY KEY,stamp TEXT,imported REAL,rows INTEGER)')
        receipt=target.db.execute('SELECT stamp FROM preview_imports WHERE source=?',(receipt_key,)).fetchone()
        if receipt and receipt[0]==stamp:return 0
        other=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=30);other.row_factory=sqlite3.Row
        other.execute('BEGIN')
        query="SELECT f.*,r.started AS evidence_started FROM files f LEFT JOIN runs r ON r.id=f.seen_run WHERE f.state IN ('selected','rejected')"
        if scheme:query+=" AND COALESCE(json_extract(CASE WHEN json_valid(r.config) THEN r.config ELSE '{}' END,'$.scheme'),'photo')=?"
        cursor=other.execute(query,(scheme,) if scheme else ())
        allowed={r[1] for r in target.db.execute('PRAGMA table_info(files)')}
        while True:
            rows=cursor.fetchmany(512)
            if not rows:break
            for row in rows:
                path=Path(row['source'])
                try:
                    if path.resolve()!=path or fingerprint(path)!=(row['size'],row['mtime']):continue
                except OSError:continue
                existing=target.db.execute('SELECT f.*,r.started AS evidence_started FROM files f LEFT JOIN runs r ON r.id=f.seen_run WHERE f.source=?',(row['source'],)).fetchone()
                if existing:
                    # Import evidence, never replace file-operation journals or
                    # unresolved recovery state. A later valid preview may,
                    # however, supersede older/empty cached evidence.
                    if existing['state'] not in ('selected','rejected','queued'):continue
                    same=(existing['size'],existing['mtime'])==(row['size'],row['mtime'])
                    current_version=False;incoming_version=False
                    if evidence_key and same:
                        try:current_version=json.loads(existing['metrics'] or '{}').get('evidence_key')==evidence_key
                        except (ValueError,TypeError,AttributeError):pass
                        try:incoming_version=json.loads(row['metrics'] or '{}').get('evidence_key')==evidence_key
                        except (ValueError,TypeError,AttributeError):pass
                        if current_version and not incoming_version:continue
                    # A resumed preview can hold fresh model evidence even if
                    # its run originally started before the target's last run.
                    newer_evidence=incoming_version and not current_version
                    if same and not newer_evidence and existing['metrics'] not in (None,'','{}') and (existing['evidence_started'] or 0)>=(row['evidence_started'] or 0):continue
                values={key:row[key] for key in row.keys() if key in allowed}
                # Duplicate decisions depend on the previous destination. Keep
                # scores but calculate the representative again for this one.
                if values.get('reason') in ('exact_duplicate','duplicate_variant','backup_duplicate'):
                    values.update(state='queued',reason='')
                values['destination']=None
                columns=list(values)
                update=','.join(key+'=excluded.'+key for key in columns if key!='source')
                target.db.execute('INSERT INTO files('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+') ON CONFLICT(source) DO UPDATE SET '+update,[values[key] for key in columns])
                imported+=1
        # A live preview may keep writing while this snapshot is imported.
        # Mark a receipt only for a stable source DB + WAL generation; the next
        # continue action then avoids a second full-library stat/import pass.
        if _database_stamp(source)==stamp:
            target.db.execute('INSERT OR REPLACE INTO preview_imports VALUES(?,?,?,?)',(receipt_key,stamp,time.time(),imported))
        target.db.commit()
        return imported
    finally:
        if other is not None:other.close()
        target.close()

"""Reviewable in-place actions; old bytes remain undoable until normal close."""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import sqlite3
import threading
import time
import uuid
from PIL import Image
from .moving import ensure_parents,fingerprint,file_hash,rename_no_replace,available_destination
from .recycling import recycle_file
from .review import comparison_gate,single_root
from .storage import output_lock


def plain_path(path,root):
    path=Path(path).absolute();root=Path(root).resolve()
    path.relative_to(root)
    if path.resolve()!=path:raise OSError('路径含链接或目录已变化，操作停止。')
    for parent in (path,*path.parents):
        if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():raise OSError('路径含目录联接或符号链接。')
    return path


def verified_copy(source,destination,digest):
    with Path(source).open('rb') as reader,Path(destination).open('xb') as writer:
        for block in iter(lambda:reader.read(1024*1024),b''):writer.write(block)
        writer.flush();os.fsync(writer.fileno())
    if file_hash(destination)!=digest:raise OSError('复制校验失败，现有文件保留。')


class SafeActions:
    def __init__(self,recycler=recycle_file):
        self.recycler=recycler;self.stack=ExitStack();self.roots={};self.mutex=threading.RLock()

    def _connect(self,root):
        root=single_root([root]);metadata=root/'.photocleaner'/'safe-actions'
        plain_path(metadata,root);ensure_parents(metadata/'state.sqlite3',root)
        with self.mutex:
            first=root not in self.roots
            if root not in self.roots:
                self.stack.enter_context(output_lock(metadata));self.roots[root]=metadata
        database=plain_path(metadata/'state.sqlite3',root)
        db=sqlite3.connect(database,timeout=30);db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,kind TEXT,phase TEXT,payload TEXT,error TEXT DEFAULT \'\')')
        db.execute("CREATE INDEX IF NOT EXISTS action_source ON actions(json_extract(payload,'$.source'))")
        db.commit()
        if first:self._recover(db)
        return root,metadata,db

    def _recover(self,db):
        # A forced exit retains hidden backups. Repair only journal states;
        # never recycle or overwrite anything during startup.
        for row in db.execute("SELECT * FROM actions WHERE phase IN ('planned','held','review','undoing')").fetchall():
            p=json.loads(row['payload']);source=Path(p['source']);target=Path(p['target'])
            backup=Path(p['backup']) if p['backup'] else None;discard=Path(p['discard'])
            def matches(path,digest):
                try:return bool(digest and path.is_file() and file_hash(path)==digest)
                except OSError:return False
            if row['kind']=='png':
                phase='undone' if not target.exists() and matches(discard,p['new_hash']) else 'applied' if matches(target,p['new_hash']) else 'cancelled'
            elif backup and matches(backup,p['old_hash']):
                phase='applied' if row['kind']=='numbered' and not source.exists() else 'applied' if matches(target,p['new_hash']) else 'held' if not source.exists() and not target.exists() else 'review'
            else:phase='undone' if matches(source,p['old_hash']) else 'review'
            self._save(db,row['id'],phase,p)

    def _save(self,db,id,phase,payload,error=''):
        db.execute('UPDATE actions SET phase=?,payload=?,error=? WHERE id=?',(phase,json.dumps(payload,ensure_ascii=False),error,id));db.commit()

    def _new(self,db,metadata,kind,source,target,old_hash,new_hash=''):
        id=uuid.uuid4().hex;folder=metadata/'undo'/id
        payload={'source':str(source),'target':str(target),'backup':str(folder/'original'/source.name) if kind!='png' else '',
                 'discard':str(folder/'returned'/target.name),'stage':str(folder/'replacement.jpg'),
                 'old_hash':old_hash,'new_hash':new_hash}
        db.execute('INSERT INTO actions(id,kind,phase,payload) VALUES(?,?,?,?)',(id,kind,'planned',json.dumps(payload,ensure_ascii=False)));db.commit()
        ensure_parents(folder/'replacement.jpg',metadata)
        return id,payload

    def apply_compression(self,root,row,now=None):
        root,metadata,db=self._connect(root)
        try:
            source=plain_path(row['source'],root);compressed=Path(row['compressed']).absolute()
            if compressed.resolve()!=compressed:raise OSError('压缩副本路径含链接。')
            check=comparison_gate(row,now)
            if not check['eligible']:raise OSError(check['reason'])
            if not row['digest'] or file_hash(compressed)!=row['digest']:raise OSError('压缩副本哈希已改变，未回写。')
            with Image.open(compressed) as image:
                image.load()
                expected=(row.get('output_width'),row.get('output_height'))
                if image.format!='JPEG' or (all(expected) and image.size!=expected):raise OSError('JPG 副本格式或尺寸校验失败。')
            old_hash=file_hash(source)
            png=row['format']=='PNG'
            target=source if not png and source.suffix.lower() in ('.jpg','.jpeg','.jpe') else available_destination(source.with_suffix('.jpg'))
            plain_path(target,root)
            id,payload=self._new(db,metadata,'png' if png else 'replace',source,target,old_hash,row['digest'])
            stage=Path(payload['stage']);backup=Path(payload['backup']) if payload['backup'] else None
            try:
                verified_copy(compressed,stage,row['digest'])
                # Recheck after expensive copying and immediately before a cut.
                check=comparison_gate(row,now)
                if not check['eligible'] or file_hash(source)!=old_hash:raise OSError('原图或副本在校验期间改变，未回写。')
                # A replacement is a new version. Undo retains the old date
                # because the old file is renamed, never retimestamped.
                written=time.time_ns();os.utime(stage,ns=(written,written))
                payload['writeback_mtime_ns']=written
                if backup:
                    ensure_parents(backup,metadata);rename_no_replace(source,backup)
                    self._save(db,id,'held',payload)
                rename_no_replace(stage,target)
                self._save(db,id,'applied',payload)
                return str(target)
            except Exception as exc:
                if backup and backup.exists() and not os.path.lexists(source):rename_no_replace(backup,source)
                phase='review' if backup and backup.exists() else 'cancelled'
                self._save(db,id,phase,payload,str(exc));raise
        finally:db.close()

    def remove_numbered(self,root,row):
        root,metadata,db=self._connect(root)
        try:
            source=plain_path(row['source'],root);keep=plain_path(row['representative'],root)
            if source==keep or not row.get('numbered') or not row.get('candidate'):raise OSError('不是已确认的编号副本。')
            if fingerprint(source)!=(row['size'],row['mtime']):raise OSError('副本已变化，未清理。')
            if file_hash(source)!=row['digest'] or file_hash(keep)!=row['digest']:raise OSError('副本或保留图像发生变化，未清理。')
            id,payload=self._new(db,metadata,'numbered',source,keep,row['digest'])
            backup=Path(payload['backup']);ensure_parents(backup,metadata)
            if fingerprint(source)!=(row['size'],row['mtime']):raise OSError('校验期间副本改变，未清理。')
            rename_no_replace(source,backup);self._save(db,id,'applied',payload)
            return str(source)
        finally:db.close()

    def statuses(self,root,sources=None):
        root,metadata,db=self._connect(root)
        try:
            result={}
            query='SELECT * FROM actions';params=()
            if sources is not None:
                if not sources:return result
                query+=' WHERE json_extract(payload,\'$.source\') IN ('+','.join('?' for _ in sources)+')';params=tuple(sources)
            for r in db.execute(query+' ORDER BY rowid',params):
                p=json.loads(r['payload']);result[p['source']]={'phase':p.get('final_state',r['phase']) if r['phase']=='finalized' else r['phase'],
                    'closed':r['phase']=='finalized','new_hash':p['new_hash'],'kind':r['kind']}
            return result
        finally:db.close()

    def undo(self,root=None,kind=None,emit=lambda _:None,stop=None):
        restored=errors=0
        roots=[Path(root).resolve()] if root else list(self.roots)
        for entry in roots:
            entry,metadata,db=self._connect(entry)
            try:
                self._recover(db)
                query="SELECT * FROM actions WHERE phase IN ('applied','held','review','undoing')"
                params=()
                if kind=='compression':query+=" AND kind IN ('replace','png')"
                elif kind:query+=' AND kind=?';params=(kind,)
                for row in db.execute(query+' ORDER BY rowid DESC',params):
                    if stop and stop.is_set():break
                    p=json.loads(row['payload']);source=plain_path(p['source'],entry);target=plain_path(p['target'],entry)
                    backup=plain_path(p['backup'],entry) if p['backup'] else None;discard=plain_path(p['discard'],entry)
                    try:
                        if row['kind']=='png':
                            if file_hash(source)!=p['old_hash']:raise OSError('PNG 原图已变化，未撤回。')
                            if file_hash(target)!=p['new_hash']:raise OSError('回写的 JPG 已变化，未撤回。')
                            ensure_parents(discard,metadata);self._save(db,row['id'],'undoing',p)
                            rename_no_replace(target,discard)
                        else:
                            if not backup or file_hash(backup)!=p['old_hash']:raise OSError('撤回备份缺失或已变化。')
                            if row['kind']=='replace' and target.exists():
                                if file_hash(target)!=p['new_hash']:raise OSError('回写后文件已变化，未覆盖新修改。')
                                if target!=source and os.path.lexists(source):raise OSError('原路径出现新文件，未覆盖。')
                                ensure_parents(discard,metadata);self._save(db,row['id'],'undoing',p)
                                rename_no_replace(target,discard)
                            if os.path.lexists(source):raise OSError('原路径已有文件，未覆盖。')
                            ensure_parents(source,entry);rename_no_replace(backup,source)
                        self._save(db,row['id'],'undone',p);restored+=1;emit(f'已撤回：{source}')
                    except (OSError,ValueError) as exc:
                        errors+=1;emit(f'撤回跳过：{source}；{exc}')
            finally:db.close()
        return {'restored':restored,'errors':errors}

    def finalize(self,emit=lambda _:None):
        """Recycle pending old/discarded copies on explicit normal app close."""
        recycled=errors=0
        for root in list(self.roots):
            root,metadata,db=self._connect(root)
            try:
                for row in db.execute("SELECT * FROM actions WHERE phase<>'finalized'"):
                    p=json.loads(row['payload'])
                    try:
                        if row['phase'] in ('held','review','undoing'):
                            raise OSError('有中断操作，请先撤回；备份保留。')
                        for key,digest in (('backup',p['old_hash']),('discard',p['new_hash'])):
                            if not p[key]:continue
                            path=plain_path(p[key],root)
                            if not path.exists():continue
                            if file_hash(path)!=digest:raise OSError('备份已变化，未回收。')
                            self.recycler(path);recycled+=1;emit(f'已移入回收站：{path.name}')
                        p['final_state']=row['phase'];self._save(db,row['id'],'finalized',p)
                    except (OSError,ValueError) as exc:
                        errors+=1;emit(f'关闭回收未完成：{p["source"]}；{exc}；备份：{p["backup"] or p["discard"]}')
            finally:db.close()
        return {'recycled':recycled,'errors':errors}

    def release(self):self.stack.close()

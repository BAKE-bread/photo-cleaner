"""Content equality across names and runs. Duplicates are retained, never deleted."""
from pathlib import Path
from .moving import file_hash,fingerprint

class FlatDedup:
    def __init__(self,store,output,stop,issue,emit=lambda _:None):
        from .engine import stream_files
        self.store=store;self.output=Path(output);self.stop=stop
        store.db.execute('CREATE TABLE IF NOT EXISTS hash_cache(path TEXT PRIMARY KEY,size INTEGER,mtime INTEGER,digest TEXT)')
        store.db.execute('DELETE FROM content_index')
        batch=[];indexed=0
        def flush():
            nonlocal indexed
            self.prime(batch)
            if stop.is_set():batch.clear();return
            for row in batch:
                try:
                    p=Path(row['source']);digest=self.digest(p,(row['size'],row['mtime']))
                    store.db.execute('INSERT OR IGNORE INTO content_index VALUES(?,?)',(digest,str(p)))
                except OSError as exc:issue(str(exc))
            indexed+=len(batch);batch.clear();store.db.commit()
            if indexed%1024==0:emit({'kind':'log','message':f'已有输出去重索引：已检查 {indexed:,} 个文件；未变化指纹复用缓存。'})
        for p,st in stream_files(self.output,[self.output/'.photocleaner',self.output/'重复副本备份'],stop,issue):
            batch.append({'source':str(p),'size':st.st_size,'mtime':st.st_mtime_ns})
            if len(batch)>=64:flush()
        if batch and not stop.is_set():flush()
        store.db.commit()

    def digest(self,path,expected):
        if Path(path).resolve()!=Path(path) or fingerprint(path)!=expected:raise OSError('文件路径或内容已改变，停止去重。')
        row=self.store.db.execute('SELECT * FROM hash_cache WHERE path=?',(str(path),)).fetchone()
        digest=row['digest'] if row and (row['size'],row['mtime'])==expected else file_hash(path)
        if fingerprint(path)!=expected:raise OSError('计算内容指纹时文件改变。')
        self.store.db.execute('INSERT OR REPLACE INTO hash_cache VALUES(?,?,?,?)',(str(path),*expected,digest))
        return digest

    def duplicate(self,path,expected):
        digest=self.digest(path,expected)
        row=self.store.db.execute('SELECT path FROM content_index WHERE digest=?',(digest,)).fetchone()
        if row and Path(row[0])!=path:
            ref=Path(row[0])
            try:
                if self.digest(ref,fingerprint(ref))==digest:return str(ref),digest
            except OSError:pass
        self.remember(path,digest)
        return None,digest

    def prime(self,rows):
        """Overlap independent disk reads; DB/cache mutations stay on owner."""
        from concurrent.futures import ThreadPoolExecutor
        work=[]
        for row in rows:
            old=self.store.db.execute('SELECT size,mtime FROM hash_cache WHERE path=?',(row['source'],)).fetchone()
            if old is None or tuple(old)!=(row['size'],row['mtime']):work.append(row)
        def hashed(row):
            if self.stop.is_set():return None
            path=Path(row['source']);expected=(row['size'],row['mtime'])
            try:
                if fingerprint(path)!=expected:return None
                digest=file_hash(path)
                return digest if fingerprint(path)==expected else None
            except OSError:return None
        if work:
            with ThreadPoolExecutor(max_workers=4,thread_name_prefix='content-hash') as pool:
                for row,digest in zip(work,pool.map(hashed,work)):
                    if digest:self.store.db.execute('INSERT OR REPLACE INTO hash_cache VALUES(?,?,?,?)',(row['source'],row['size'],row['mtime'],digest))

    def remember(self,path,digest):
        self.store.db.execute('INSERT OR REPLACE INTO content_index VALUES(?,?)',(digest,str(path)))

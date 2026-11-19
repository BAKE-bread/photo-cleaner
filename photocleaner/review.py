"""Paged compression comparisons and conservative numbered-copy discovery."""
from .numbers import number
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from PIL import Image
from .config import PROJECT
from .engine import normalize_inputs,normalize_paths,stream_files
from .moving import fingerprint,file_hash

MIN_SAVING=500_000
MIN_AGE=3*24*60*60


def single_root(inputs):
    if len(inputs)!=1:raise ValueError('此操作仅允许一个输入目录；请只保留一项。')
    return normalize_inputs(inputs)[0]


def compression_rows(root,output,offset=0,limit=300):
    root,out=normalize_paths([root],output)
    database=out/'.photocleaner'/'compress'/'state.sqlite3'
    if not database.is_file():raise ValueError('请先在当前输入/输出目录生成 JPG 副本，再打开对比列表。')
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        condition="f.root=? AND f.state='copied' AND c.id=(SELECT MAX(id) FROM copies WHERE source=f.source AND phase='done')"
        total=db.execute('SELECT COUNT(*) FROM files f JOIN copies c ON c.source=f.source WHERE '+condition,(str(root[0]),)).fetchone()[0]
        rows=db.execute('SELECT f.*,c.digest,c.destination AS compressed FROM files f JOIN copies c ON c.source=f.source WHERE '+condition+' ORDER BY f.source LIMIT ? OFFSET ?',
                        (str(root[0]),limit,offset)).fetchall()
        result=[]
        for r in rows:
            metrics=json.loads(r['metrics'])
            result.append({'source':r['source'],'compressed':r['compressed'],'relative':r['relative'],
                'size':r['size'],'mtime':r['mtime'],'expected_output':metrics.get('output_fingerprint'),
                'digest':r['digest'],'format':metrics.get('format',''),'width':metrics.get('width'),
                'height':metrics.get('height'),'primary_only':metrics.get('primary_only',False),
                'output_width':metrics.get('output_width'),'output_height':metrics.get('output_height'),'quality':metrics.get('jpg_quality')})
    return result,total


def comparison_gate(row,now=None):
    """A fast list check. The actual action also verifies hashes and decode."""
    now=time.time() if now is None else now
    data=dict(row);data.update(eligible=False,tone='blocked',saving=None,output_size=None)
    try:
        source,out=Path(row['source']),Path(row['compressed'])
        if fingerprint(source)!=(row['size'],row['mtime']):raise OSError('原图已变化，需重新生成副本')
        current=fingerprint(out);data['output_size']=current[0];data['output_mtime']=current[1];data['saving']=row['size']-current[0]
        if row.get('expected_output') and list(current)!=list(row['expected_output']):raise OSError('压缩副本已变化，需重新生成')
        if data['saving']<MIN_SAVING:
            data['reason']='体积减少不足 0.5 MB';return data
        if row['mtime']/1e9>now-MIN_AGE:
            data['reason']='原图在最近 72 小时内修改（含未来时间）';return data
        data.update(eligible=True,tone='eligible',reason='允许添加 JPG，PNG 原图保留' if row['format']=='PNG' else '允许回写，旧原图可撤回')
    except (OSError,ValueError) as exc:data.update(reason=str(exc),tone='error')
    return data


_NUMBER=re.compile(r'(?:\s*[（(]\s*\d+\s*[）)]|[ _-]+\d+)\s*$')
_COPY=re.compile(r'(?:[ _-]*(?:副本|拷贝|copy))\s*$',re.I)


def numbered_base(name):
    stem=Path(name).stem;value=stem;numbered=False
    # Numeric camera sequences without a separator remain distinct. Even a
    # matched suffix is actionable only with a byte-identical peer.
    for _ in range(12):
        new=_NUMBER.sub('',value)
        new=_COPY.sub('',new).rstrip(' _-')
        if new==value or not new:break
        value=new;numbered=True
    return value.casefold(),numbered


def scan_numbered(root,output=None,stop=None,emit=lambda _:None):
    root=single_root([root]);stop=stop or threading.Event()
    out=Path(output).expanduser().resolve() if output else None
    # This is an in-place tool. A flatten output is only an exclusion when
    # it is a proper descendant, never a required destination setting.
    if out and root not in out.parents:out=None
    key=hashlib.sha256(str(root).encode()).hexdigest()[:16]
    metadata=PROJECT/'.jobs'/'numbered'/key;metadata.mkdir(parents=True,exist_ok=True)
    database=metadata/'review.sqlite3'
    if database.resolve()!=database.absolute():raise OSError('副本列表数据库不能包含链接或目录联接。')
    from .storage import output_lock
    with output_lock(metadata):return _scan_numbered(root,out,stop,emit,database)


def _scan_numbered(root,out,stop,emit,database):
    db=sqlite3.connect(database,timeout=30);db.row_factory=sqlite3.Row
    try:
        db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;')
        db.executescript('''CREATE TABLE IF NOT EXISTS images(source TEXT PRIMARY KEY,parent TEXT,base TEXT,
            numbered INTEGER,size INTEGER,mtime INTEGER,digest TEXT DEFAULT '',representative TEXT DEFAULT '',candidate INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS numbered_groups ON images(base,size);
            CREATE INDEX IF NOT EXISTS numbered_candidates ON images(candidate,source);''')
        db.execute('''CREATE TABLE IF NOT EXISTS hash_cache(source TEXT PRIMARY KEY,size INTEGER,mtime INTEGER,identity TEXT,digest TEXT)''')
        db.execute('DELETE FROM images');db.commit() # Disposable preview, no file removal.
        supported=set(Image.registered_extensions())|{'.heic','.heif','.avif'};count=0
        excluded=[PROJECT/'models',PROJECT/'.jobs']+([out] if out else [])
        for path,st in stream_files(root,excluded,stop,lambda text:emit({'kind':'log','message':text})):
            if path.suffix.lower() not in supported:continue
            base,numbered=numbered_base(path.name)
            db.execute('INSERT INTO images(source,parent,base,numbered,size,mtime) VALUES(?,?,?,?,?,?)',
                       (str(path),str(path.parent),base,int(numbered),st.st_size,st.st_mtime_ns));count+=1
            if count%1000==0:db.commit();emit({'kind':'scan','scanned':count})
        db.commit()
        groups=db.execute('SELECT base,size FROM images GROUP BY base,size HAVING COUNT(*)>1 AND SUM(numbered)>0')
        checked=0
        for group in groups:
            if stop.is_set():break
            for row in db.execute('SELECT * FROM images WHERE base=? AND size=?',(group['base'],group['size'])):
                if stop.is_set():break
                p=Path(row['source'])
                try:
                    expected=(row['size'],row['mtime'])
                    if fingerprint(p)!=expected:continue
                    with Image.open(p) as image:
                        if image.width<=0 or image.height<=0:continue
                    st=p.stat();identity=json.dumps([st.st_dev,st.st_ino,st.st_ctime_ns])
                    cached=db.execute('SELECT digest FROM hash_cache WHERE source=? AND size=? AND mtime=? AND identity=?',(str(p),*expected,identity)).fetchone()
                    digest=cached[0] if cached else None
                    if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):digest=file_hash(p)
                    if fingerprint(p)!=expected:continue
                    current=p.stat()
                    if json.dumps([current.st_dev,current.st_ino,current.st_ctime_ns])!=identity:continue
                    db.execute('INSERT OR REPLACE INTO hash_cache VALUES(?,?,?,?,?)',(str(p),*expected,identity,digest))
                    db.execute('UPDATE images SET digest=? WHERE source=?',(digest,str(p)))
                except (OSError,ValueError,SyntaxError):continue
                checked+=1
                if checked%100==0:db.commit();emit({'kind':'log','message':f'副本校验：已检查 {number(checked,group=True)} 个可能重复的图像。'})
        if not stop.is_set():
            db.execute('''WITH ranked AS (SELECT source,numbered,FIRST_VALUE(source) OVER (
                PARTITION BY base,size,digest ORDER BY numbered,length(source),source) keep,
                COUNT(*) OVER(PARTITION BY base,size,digest) n FROM images WHERE digest<>'')
                UPDATE images SET representative=(SELECT keep FROM ranked WHERE ranked.source=images.source),
                candidate=CASE WHEN source IN(SELECT source FROM ranked WHERE numbered=1 AND n>1 AND source<>keep) THEN 1 ELSE 0 END''')
        db.commit()
        total=db.execute('SELECT COUNT(*) FROM images WHERE candidate=1').fetchone()[0]
        return {'database':str(database),'scanned':count,'checked':checked,'candidates':total,'stopped':stop.is_set()}
    finally:db.close()


def numbered_rows(database,offset=0,limit=300):
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        total=db.execute('SELECT COUNT(*) FROM images WHERE candidate=1').fetchone()[0]
        return [dict(row) for row in db.execute('SELECT * FROM images WHERE candidate=1 ORDER BY source LIMIT ? OFFSET ?',(limit,offset))],total

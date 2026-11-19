"""Confirmed search exports are verified copies, with no source deletion."""
from pathlib import Path
import sqlite3,time,uuid,os,shutil,json,csv,hashlib
from .moving import fingerprint,file_hash,ensure_parents,available_destination,rename_no_replace
from .engine import normalize_paths
from .storage import output_lock,excel_safe

def copy_results(results,roots,output,stop,emit=lambda _:None,retain_tree=False,observe=None):
    roots,out=normalize_paths(roots,output);out.mkdir(parents=True,exist_ok=True)
    meta=out/'.photocleaner';copied=skipped=errors=0;run=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    if meta.is_symlink() or getattr(meta,'is_junction',lambda:False)():raise OSError('输出记录目录不能是链接或目录联接。')
    with output_lock(meta):
        db=sqlite3.connect(meta/'search_copies.sqlite3')
        db.execute('''CREATE TABLE IF NOT EXISTS copies(id INTEGER PRIMARY KEY,run TEXT,source TEXT,destination TEXT,
            digest TEXT,staging TEXT,state TEXT,error TEXT,size INTEGER,mtime INTEGER)''')
        from .storage import Store
        from .flat_dedup import FlatDedup
        content_store=Store(meta/'search_content.sqlite3') if not retain_tree else None
        try:
            index=FlatDedup(content_store,out,stop,lambda text:emit({'kind':'log','message':text}),emit) if content_store else None
            for n,row in enumerate(results,1):
                if stop.is_set():break
                source=Path(row['path']);dest=None;record=None;digest=None;state='error';error=''
                try:
                    if source.resolve()!=source or not any(source.is_relative_to(r) for r in roots):raise OSError('源路径已变为链接或不在本次输入目录内')
                    if source==out or out in source.parents:raise OSError('候选位于输出目录内，未重复复制')
                    expected=(row['size'],row['mtime'])
                    if fingerprint(source)!=expected:raise OSError('原图在搜索后发生变化，未复制；请更新索引')
                    digest=file_hash(source)
                    if row.get('sha') and digest!=row['sha']:raise OSError('图像内容已变化，未复制')
                    root=next(r for r in roots if source.is_relative_to(r))
                    bucket=root.name+'_'+hashlib.sha256(str(root).encode()).hexdigest()[:8]
                    planned=out/bucket/source.relative_to(root) if retain_tree else out/source.name
                    if index:
                        duplicate,_=index.duplicate(source,expected)
                        if duplicate:skipped+=1;dest=Path(duplicate);state='existing';continue
                    # Avoid extra exports when the same source was already
                    # verified into this output. Different equal files remain
                    # selectable; this is not a general deduplication tool.
                    old=db.execute("SELECT destination FROM copies WHERE source=? AND digest=? AND state='done' ORDER BY id DESC",(str(source),digest)).fetchall()
                    reused=False
                    for (previous,) in old:
                        p=Path(previous)
                        if p.resolve().parent==planned.parent and p.is_file() and file_hash(p)==digest:reused=True;break
                    if reused:skipped+=1;dest=p;state='existing';continue
                    dest=available_destination(planned);ensure_parents(dest,out)
                    staging=dest.with_name(dest.name+'.'+uuid.uuid4().hex+'.partial')
                    record=db.execute("INSERT INTO copies(run,source,destination,digest,staging,state,size,mtime) VALUES(?,?,?,?,?,'copying',?,?)",
                        (run,str(source),str(dest),digest,str(staging),*expected)).lastrowid;db.commit()
                    with source.open('rb') as reader,staging.open('xb') as writer:
                        shutil.copyfileobj(reader,writer,1024*1024);writer.flush();os.fsync(writer.fileno())
                    if fingerprint(source)!=expected or file_hash(staging)!=digest or file_hash(source)!=digest:
                        raise OSError('复制校验未通过，原图及临时副本均保留')
                    shutil.copystat(source,staging,follow_symlinks=False)
                    while True:
                        ensure_parents(dest,out)
                        try:rename_no_replace(staging,dest);break
                        except FileExistsError:dest=available_destination(planned)
                    db.execute('UPDATE copies SET destination=?,state=? WHERE id=?',(str(dest),'done',record));db.commit();copied+=1
                    if index:index.remember(dest,digest);content_store.db.commit()
                    state='copied'
                except (OSError,ValueError) as exc:
                    errors+=1;error=str(exc)
                    if record:db.execute("UPDATE copies SET state='review',error=? WHERE id=?",(str(exc),record));db.commit()
                    else:
                        db.execute("INSERT INTO copies(run,source,state,error) VALUES(?,?,'blocked',?)",(run,str(source),str(exc)));db.commit()
                    emit({'kind':'log','message':f'未完成复制，现有文件保留：{source.name}：{exc}'})
                finally:
                    if observe:observe(row,state,str(dest) if dest else None,digest,error)
                    emit({'kind':'progress','phase':'复制所选图像','processed':n,'total':len(results),'counts':{}})
            report=meta/f'search_copy_{run}.csv'
            with report.open('w',newline='',encoding='utf-8-sig') as f:
                writer=csv.writer(f);writer.writerow(['源图像','复制目标','SHA256','状态','保留的临时文件','错误'])
                for r in db.execute('SELECT source,destination,digest,state,staging,error FROM copies WHERE run=?',(run,)):
                    writer.writerow([excel_safe(str(x or '')) for x in r])
        finally:
            db.close()
            if content_store:content_store.close()
    return {'copied':copied,'skipped':skipped,'errors':errors,'stopped':stop.is_set(),'report':str(report)}

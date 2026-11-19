"""SHA256 deduplication in independent roots; keepers retain their original paths."""
from pathlib import Path
from dataclasses import asdict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
import csv,hashlib,json,os,threading,time,uuid
from .config import PROJECT
from .directory_lists import clean_path
from .engine import stream_files
from .storage import Store,output_lock,excel_safe
from .moving import fingerprint,file_hash
from .hash_task import digest_record
from .recycling import recycle_file

def inplace_locations(inputs):
    roots=[]
    for value in inputs:
        path=Path(clean_path(str(value))).expanduser().absolute()
        if path.resolve()!=path or not path.is_dir():raise ValueError('请选择存在的普通输入目录，路径应直接指向实际文件夹。')
        if path in roots:continue
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in roots):raise ValueError('原位去重请添加互相独立的目录；每个目录会自动包含它的所有子目录。')
        roots.append(path)
    if not roots:raise ValueError('请添加至少一个输入目录。')
    # Scope order is intentionally independent of priority order, so changing
    # priorities reuses existing file hashes in the same database.
    key=hashlib.sha256(json.dumps(sorted(str(p).casefold() for p in roots)).encode()).hexdigest()[:20]
    metadata=PROJECT/'.jobs'/'hash_inplace'/key
    if metadata.resolve()!=metadata.absolute():raise ValueError('运行记录应保存在实际目录。')
    if any(metadata.is_relative_to(root) for root in roots):raise ValueError('请选择具体的资料目录；程序运行记录应位于这些目录之外。')
    return roots,metadata

@contextmanager
def keeper_guard(path):
    # Windows keeps the representative readable while denying writes/renames
    # throughout deletion of its duplicates. Nothing is written into the roots.
    if os.name=='nt':
        try:
            import win32file,win32con,pywintypes
        except ImportError as exc:raise OSError('请运行 setup.bat 准备回收站组件；现有文件保留。') from exc
        try:handle=win32file.CreateFile(str(path),win32con.GENERIC_READ,win32con.FILE_SHARE_READ,None,win32con.OPEN_EXISTING,win32con.FILE_ATTRIBUTE_NORMAL,None)
        except pywintypes.error as exc:raise OSError('保留文件暂时无法锁定，副本保留：'+str(exc)) from exc
        try:yield
        finally:handle.Close()
    else:
        with path.open('rb'):yield

class InplaceHashRunner:
    def __init__(self,config,emit=None,stop=None,recycler=recycle_file):
        self.config=config;self.emit=emit or (lambda _:None);self.stop=stop or threading.Event();self.recycler=recycler
    def run(self,inputs,mode='preview'):
        self.config.validate()
        if mode not in ('preview','move'):raise ValueError('请选择预览或执行操作。')
        roots,metadata=inplace_locations(inputs);metadata.mkdir(parents=True,exist_ok=True)
        run=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6];issues=[];count=0;groups=0;live={}
        def counts():return {r[0]:r[1] for r in store.db.execute('SELECT state,COUNT(*) FROM files WHERE seen_run=? GROUP BY state',(run,))}
        def transition(old,new):
            live[old]=live.get(old,0)-1;live[new]=live.get(new,0)+1
        def progress(phase,done,total):self.emit({'kind':'progress','phase':phase,'processed':done,'total':total,'scanned':count,'counts':{k:v for k,v in live.items() if v},'hash_mode':'inplace','duplicate_groups':groups})
        with output_lock(metadata):
            store=Store(metadata/'state.sqlite3')
            try:
                store.db.execute('CREATE TABLE IF NOT EXISTS recycle_events(id INTEGER PRIMARY KEY,run_id TEXT,source TEXT,keeper TEXT,digest TEXT,phase TEXT,error TEXT DEFAULT \'\')')
                store.db.execute('INSERT INTO runs VALUES(?,?,?,?,NULL,NULL)',(run,time.time(),json.dumps(asdict(self.config)),mode));store.db.commit()
                for old in store.db.execute("SELECT * FROM recycle_events WHERE phase='planned'").fetchall():
                    source=Path(old['source']);keeper=Path(old['keeper']);done=False
                    try:done=not os.path.lexists(source) and keeper.resolve()==keeper and file_hash(keeper)==old['digest']
                    except OSError:pass
                    state='recycled' if done else 'review'
                    store.db.execute('UPDATE recycle_events SET phase=? WHERE id=?',('done' if done else 'review',old['id']))
                    store.db.execute('UPDATE files SET state=?,reason=? WHERE source=?',(state,'inplace_duplicate' if done else 'review',str(source)))
                    self.emit({'kind':'log','message':('已核对上次回收记录：' if done else '上次回收记录保留待核对：')+str(source)})
                self.emit({'kind':'log','message':'原位去重：按输入目录从上到下选择保留文件，副本送入回收站；所有保留文件及目录结构维持原位。'})
                for root in roots:
                    for path,stat in stream_files(root,[PROJECT/'.jobs',PROJECT/'models'],self.stop,issues.append):
                        store.register(path,root,stat,run,'inplace-hash-v1',True,preserve_evidence=True);count+=1
                        if count%1000==0:store.db.commit();self.emit({'kind':'scan','scanned':count})
                    if self.stop.is_set():break
                store.db.execute("UPDATE files SET state='queued',reason='' WHERE seen_run=? AND state<>'review'",(run,));store.db.commit()
                live.update(counts());last='';done=live.get('review',0)
                with ThreadPoolExecutor(max_workers=4,thread_name_prefix='inplace-hash') as pool:
                    while not self.stop.is_set():
                        rows=store.db.execute("SELECT * FROM files WHERE seen_run=? AND state<>'review' AND source>? ORDER BY source LIMIT 128",(run,last)).fetchall()
                        if not rows:break
                        last=rows[-1]['source']
                        for row,hashed in pool.map(lambda r:digest_record(r,self.stop),rows):
                            if not hashed:
                                if self.stop.is_set():break
                                store.decision(row['source'],'error','changed',{})
                                transition('queued','error')
                            else:
                                digest,identity=hashed
                                rank=next(n for n,root in enumerate(roots) if Path(row['source']).is_relative_to(root))
                                store.decision(row['source'],'queued','',{'sha256':digest,'hash_identity':identity,'priority_rank':rank+1})
                            done+=1
                        store.db.commit();progress('计算文件哈希',done,count)
                store.db.execute('DELETE FROM content_index');done=count-live.get('queued',0)
                if not self.stop.is_set():
                    for row in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='queued' ORDER BY json_extract(metrics,'$.priority_rank'),length(relative),source",(run,)):
                        if self.stop.is_set():break
                        metrics=json.loads(row['metrics']);digest=metrics['sha256']
                        other=store.db.execute('SELECT path FROM content_index WHERE digest=?',(digest,)).fetchone()
                        if other:
                            metrics.update(role='recycle',duplicate_of=other[0]);state,reason='selected','inplace_duplicate'
                        else:
                            store.db.execute('INSERT INTO content_index VALUES(?,?)',(digest,row['source']));metrics['role']='keep';state,reason='rejected','inplace_keeper'
                        store.decision(row['source'],state,reason,metrics);transition('queued',state);done+=1
                        if done%128==0:store.db.commit();progress('按目录优先级选择保留文件',done,count)
                    store.db.commit()
                    groups=store.db.execute("SELECT COUNT(DISTINCT json_extract(metrics,'$.sha256')) FROM files WHERE seen_run=? AND state='selected'",(run,)).fetchone()[0]
                    progress('按目录优先级选择保留文件',done,count)
                if mode=='move' and not issues and not self.stop.is_set():
                    total=counts().get('selected',0);done=0
                    cursor=store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' ORDER BY json_extract(metrics,'$.sha256'),source",(run,))
                    for digest,duplicates in groupby(cursor,lambda r:json.loads(r['metrics'])['sha256']):
                        duplicates=iter(duplicates);first=next(duplicates);ref=Path(json.loads(first['metrics'])['duplicate_of'])
                        def process(row,keeper_error=None):
                            nonlocal done
                            if self.stop.is_set():return
                            path=Path(row['source']);metrics=json.loads(row['metrics']);event_id=None
                            try:
                                if keeper_error:raise OSError(keeper_error)
                                if path==ref or path.resolve()!=path or fingerprint(path)!=(row['size'],row['mtime']) or file_hash(path)!=digest:raise OSError('副本在执行前变化，已保留。')
                                event_id=store.db.execute("INSERT INTO recycle_events(run_id,source,keeper,digest,phase) VALUES(?,?,?,?,?)",(run,str(path),str(ref),digest,'planned')).lastrowid;store.db.commit()
                                self.recycler(path)
                                if os.path.lexists(path):raise OSError('回收尚未完成，文件保留原位。')
                                store.db.execute("UPDATE recycle_events SET phase='done' WHERE id=?",(event_id,))
                                store.db.execute("UPDATE files SET state='recycled',destination=? WHERE source=?",(str(ref),str(path)))
                                transition('selected','recycled')
                            except (OSError,RuntimeError) as exc:
                                metrics['error']=str(exc);store.decision(str(path),'review','review',metrics)
                                transition('selected','review')
                                if event_id:store.db.execute("UPDATE recycle_events SET phase='review',error=? WHERE id=?",(str(exc),event_id))
                                self.emit({'kind':'log','message':f'保留待核对：{path}；{exc}'})
                            store.db.commit();done+=1
                            if done%32==0 or done==total:progress('核验并回收重复副本',done,total)
                        try:
                            with keeper_guard(ref):
                                reference=store.db.execute('SELECT size,mtime FROM files WHERE source=?',(str(ref),)).fetchone()
                                if ref.resolve()!=ref or reference is None or fingerprint(ref)!=tuple(reference) or file_hash(ref)!=digest:raise OSError('保留文件发生变化，本组副本留在原处。')
                                process(first)
                                for row in duplicates:
                                    if self.stop.is_set():break
                                    process(row)
                        except (OSError,RuntimeError) as exc:
                            process(first,str(exc))
                            for row in duplicates:
                                if self.stop.is_set():break
                                process(row,str(exc))
                        if self.stop.is_set():break
                summary={'task':'hash','hash_mode':'inplace','run_id':run,'mode':mode,'scanned':count,'counts':counts(),'duplicate_groups':groups,'roots':list(map(str,roots)),
                    'priority_roots':list(map(str,roots)),'config':asdict(self.config),'metadata':str(metadata),'scan_issues':len(issues),'stopped':self.stop.is_set(),
                    'fatal_error':'部分目录读取未完成，待目录可用后继续核对；本轮保持所有文件原位。' if issues else None}
                store.db.execute('UPDATE runs SET finished=?,summary=? WHERE id=?',(time.time(),json.dumps(summary,ensure_ascii=False),run));store.db.commit()
                summary['report']=str(store.export(run,metadata));journal=metadata/'recycle_history.csv'
                with journal.open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream);writer.writerow(['运行','原路径','保留文件','SHA256','阶段','提示'])
                    for event in store.db.execute('SELECT run_id,source,keeper,digest,phase,error FROM recycle_events ORDER BY id'):writer.writerow([excel_safe(str(v)) for v in event])
                summary['history']=str(journal);self.emit({'kind':'done',**summary});return summary
            finally:store.close()

"""SHA256-only consolidation. Recycle duplicates only after keeper verification."""
from pathlib import Path
from dataclasses import asdict
import hashlib,json,threading,time,uuid
from concurrent.futures import ThreadPoolExecutor
from .config import PROJECT
from .engine import normalize_inputs,stream_files
from .storage import Store,output_lock
from .moving import fingerprint,file_hash,execute_move,ensure_parents,recover_moves
from .recycling import recycle_file
from .directory_lists import clean_path

def digest_record(row,stop):
    if stop.is_set():return row,None
    try:
        p=Path(row['source']);before=(row['size'],row['mtime'])
        if p.resolve()!=p or fingerprint(p)!=before:raise OSError('源文件变化')
        st=p.stat();identity=[st.st_dev,st.st_ino,st.st_ctime_ns]
        try:cached=json.loads(row['metrics'] or '{}')
        except (ValueError,TypeError):cached={}
        if not isinstance(cached,dict):cached={}
        h=cached.get('sha256') if cached.get('hash_identity')==identity else None
        if not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h):h=None
        if not h:h=file_hash(p)
        if fingerprint(p)!=before:raise OSError('读取期间变化')
        after=p.stat()
        if [after.st_dev,after.st_ino,after.st_ctime_ns]!=identity:raise OSError('读取期间文件被替换')
        return row,(h,identity)
    except (OSError,ValueError,TypeError):return row,None

class HashRunner:
    def __init__(self,config,emit=None,stop=None,recycler=recycle_file):
        self.config=config;self.emit=emit or (lambda _:None);self.stop=stop or threading.Event();self.recycler=recycler
    def locations(self,inputs,output):
        if self.config.hash_mode=='inplace':
            from .inplace_hash import inplace_locations
            roots,metadata=inplace_locations(inputs)
            return roots,None,metadata
        if self.config.hash_mode=='std':
            from .std_hash import std_locations
            std,others,metadata=std_locations(inputs,self.config.std_path)
            return normalize_inputs([std,*others]),None,metadata
        roots=normalize_inputs(inputs)
        output=clean_path(output or '')
        if not output:raise ValueError('哈希去重需要输出目录，以确定优先保留哪一份；输出可以是一个输入目录。')
        out=Path(output).expanduser().absolute()
        if out.resolve()!=out:raise ValueError('输出路径不能含链接或目录联接。')
        return roots,out,out/'.photocleaner'/'hash'
    def run(self,inputs,output,mode='preview',resume=True):
        if self.config.hash_mode=='inplace':
            from .inplace_hash import InplaceHashRunner
            return InplaceHashRunner(self.config,self.emit,self.stop,self.recycler).run(inputs,mode)
        if self.config.hash_mode=='std':
            from .std_hash import StandardHashRunner
            return StandardHashRunner(self.config,self.emit,self.stop,self.recycler).run(inputs,mode)
        self.config.validate()
        if mode not in ('preview','move'):raise ValueError('模式无效。')
        roots,out,meta=self.locations(inputs,output);out.mkdir(parents=True,exist_ok=True);ensure_parents(meta/'state.sqlite3',out)
        output_is_input=any(Path(clean_path(str(value))).expanduser().resolve()==out for value in inputs)
        run=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6];errors=0;issues=[]
        with output_lock(out/'.photocleaner'),output_lock(meta):
            store=Store(meta/'state.sqlite3')
            try:
                store.db.execute('INSERT INTO runs VALUES(?,?,?,?,NULL,NULL)',(run,time.time(),json.dumps(asdict(self.config)),mode));store.db.commit()
                recover_moves(store,lambda t:self.emit({'kind':'log','message':t}))
                self.emit({'kind':'log','message':'哈希去重：输出可与输入重合。优先保留输出已有内容；多余副本经校验移入回收站。请勿运行时自行操作文件夹。'})
                scope=normalize_inputs([*roots,out]);count=0
                for root in scope:
                    for p,st in stream_files(root,[PROJECT/'models',PROJECT/'.jobs',meta],self.stop,issues.append):
                        store.register(p,root,st,run,'hash-v1',True);count+=1
                        if count%1000==0:store.db.commit();self.emit({'kind':'scan','scanned':count})
                store.db.commit()
                last='';done=0
                def digest(row):
                    return digest_record(row,self.stop)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    while not self.stop.is_set():
                        batch=store.db.execute('SELECT * FROM files WHERE seen_run=? AND source>? ORDER BY source LIMIT 128',(run,last)).fetchall()
                        if not batch:break
                        last=batch[-1]['source']
                        for row,hashed in pool.map(digest,batch):
                            if hashed:
                                h,identity=hashed
                                p=Path(row['source']);inside=p.is_relative_to(out);requested=output_is_input if inside else any(p.is_relative_to(r) for r in roots)
                                store.decision(str(p),'selected','selected',{'sha256':h,'hash_identity':identity,'in_output':inside,'requested':requested})
                            elif not self.stop.is_set():store.decision(row['source'],'error','changed',{})
                            done+=1
                        store.db.commit();self.emit({'kind':'progress','phase':'SHA256 校验','processed':done,'total':count,'counts':{}})
                store.db.execute('DELETE FROM content_index')
                for row in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' ORDER BY json_extract(metrics,'$.in_output') DESC,length(relative),source",(run,)):
                    m=json.loads(row['metrics']);h=m['sha256'];representative=store.db.execute('SELECT path FROM content_index WHERE digest=?',(h,)).fetchone()
                    if representative:
                        m.update(role='recycle' if m['requested'] else 'keep',duplicate_of=representative[0])
                    else:
                        store.db.execute('INSERT INTO content_index VALUES(?,?)',(h,row['source']));m['role']='keep' if m['in_output'] else 'move'
                    store.decision(row['source'],'rejected' if m['role']=='keep' else 'selected','hash_kept' if m['role']=='keep' else 'hash_duplicate' if m['role']=='recycle' else 'selected',m)
                store.db.commit()
                if mode=='move' and not self.stop.is_set() and not issues:
                    # Representatives must arrive before any duplicate removal.
                    for role in ('move','recycle'):
                        progress=0;total=store.db.execute("SELECT COUNT(*) FROM files WHERE seen_run=? AND state='selected' AND json_extract(metrics,'$.role')=?",(run,role)).fetchone()[0]
                        for row in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' AND json_extract(metrics,'$.role')=? ORDER BY source",(run,role)):
                            if self.stop.is_set():break
                            p=Path(row['source']);m=json.loads(row['metrics']);h=m['sha256'];expected=(row['size'],row['mtime'])
                            try:
                                if p.resolve()!=p or fingerprint(p)!=expected or file_hash(p)!=h:raise OSError('文件变化，未处理')
                                if role=='move':
                                    target=execute_move(store,run,p,out/p.name,expected,lambda _:None)
                                    store.db.execute('UPDATE content_index SET path=? WHERE digest=?',(str(target),h))
                                    store.db.execute("UPDATE files SET state='moved',destination=? WHERE source=?",(str(target),str(p)))
                                else:
                                    ref=Path(store.db.execute('SELECT path FROM content_index WHERE digest=?',(h,)).fetchone()[0])
                                    if ref==p or not ref.is_relative_to(out) or ref.resolve()!=ref or file_hash(ref)!=h:raise OSError('输出代表文件不完整，副本保留')
                                    keeper_fp=fingerprint(ref)
                                    target=meta/'recycle_pending'/uuid.uuid4().hex/p.name;ensure_parents(target,out)
                                    target=execute_move(store,run,p,target,expected,lambda _:None)
                                    if file_hash(target)!=h or fingerprint(ref)!=keeper_fp or file_hash(ref)!=h:raise OSError('回收前校验失败，现有文件保留在暂存目录')
                                    self.recycler(target)
                                    store.db.execute("UPDATE files SET state='recycled',destination=? WHERE source=?",(str(ref),str(p)))
                                store.db.commit()
                            except OSError as exc:
                                m['error']=str(exc);store.decision(str(p),'review','review',m);store.db.commit();errors+=1
                                self.emit({'kind':'log','message':f'保留待复查：{p.name}：{exc}'})
                            progress+=1
                            if progress%32==0 or progress==total:self.emit({'kind':'progress','phase':'移动代表文件' if role=='move' else '验证并回收重复副本','processed':progress,'total':total,'counts':{}})
                counts={r[0]:r[1] for r in store.db.execute('SELECT state,COUNT(*) FROM files WHERE seen_run=? GROUP BY state',(run,))}
                summary={'task':'hash','run_id':run,'mode':mode,'scanned':count,'counts':counts,'scan_issues':len(issues),'stopped':self.stop.is_set(),'fatal_error':'部分目录未读完，本轮未执行移动或回收；请解决读取问题后重试。' if issues else None,'metadata':str(meta),'errors':errors}
                summary.update(config=asdict(self.config),roots=list(map(str,roots)))
                from .result_status import retained_counts
                summary.update(output=str(out),output_is_input=output_is_input,retained_counts=retained_counts(store,run))
                summary['report']=str(store.export(run,meta));summary['history']=str(meta/'move_history.csv')
                self.emit({'kind':'done',**summary});return summary
            finally:store.close()

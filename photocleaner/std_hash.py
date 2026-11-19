"""Read-only standard library; recycle matching images only from other roots."""
from pathlib import Path
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import hashlib,json,threading,time,uuid
from PIL import Image
from .config import PROJECT
from .directory_lists import clean_path
from .engine import normalize_inputs,stream_files
from .storage import Store,output_lock
from .moving import ensure_parents,execute_move,file_hash,fingerprint,recover_moves
from .recycling import recycle_file
from .hash_task import digest_record

def std_locations(inputs,std_path):
    if not str(std_path).strip():raise ValueError('请从输入列表中设置一个基准目录。')
    raw=[]
    for value in inputs:
        p=Path(clean_path(str(value))).expanduser().absolute()
        if p not in raw:raw.append(p)
    std=Path(clean_path(std_path)).expanduser().absolute()
    if std not in raw:raise ValueError('基准目录必须从输入列表中选择。')
    if std.resolve()!=std or not std.is_dir():raise ValueError('基准目录必须是存在的普通目录，不能包含链接。')
    others=normalize_inputs([p for p in raw if p!=std])
    if any(p==std or p.is_relative_to(std) for p in others):raise ValueError('其他输入不能位于基准目录内；基准目录及其子目录始终只读。')
    key=hashlib.sha256(json.dumps([str(std),list(map(str,others))]).encode()).hexdigest()[:20]
    metadata=PROJECT/'.jobs'/'hash_std'/key
    if metadata.is_relative_to(std):raise ValueError('基准目录包含程序缓存位置，请选择更具体的目录。')
    if metadata.resolve()!=metadata.absolute():raise ValueError('记录目录不能包含链接。')
    return std,others,metadata

class StandardHashRunner:
    def __init__(self,config,emit=None,stop=None,recycler=recycle_file):
        self.config=config;self.emit=emit or (lambda _:None);self.stop=stop or threading.Event();self.recycler=recycler
    def run(self,inputs,mode='preview'):
        self.config.validate()
        if mode not in ('preview','move'):raise ValueError('模式无效。')
        std,others,metadata=std_locations(inputs,self.config.std_path)
        metadata.mkdir(parents=True,exist_ok=True)
        run=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
        counts={'queued':0};std_count=0;other_count=0;issues=[];errors=0
        def progress(phase,processed,total):
            self.emit({'kind':'progress','phase':phase,'processed':processed,'total':total,'scanned':std_count+other_count,
                       'counts':dict(counts),'hash_mode':'std','std_count':std_count,'comparison_count':other_count})
        with output_lock(metadata):
            store=Store(metadata/'state.sqlite3')
            try:
                store.db.execute('INSERT INTO runs VALUES(?,?,?,?,NULL,NULL)',(run,time.time(),json.dumps(asdict(self.config)),mode));store.db.commit()
                recover_moves(store,lambda text:self.emit({'kind':'log','message':text}))
                self.emit({'kind':'log','message':f'只读基准目录：{std}；仅回收其他输入内与它字节相同的图像，不归集文件。'})
                supported=set(Image.registered_extensions())|{'.heic','.heif','.avif','.mpo'}
                for root in [std,*others]:
                    exclusions=[PROJECT/'.jobs',PROJECT/'models']+([] if root==std else [std])
                    for path,stat in stream_files(root,exclusions,self.stop,issues.append):
                        if path.suffix.lower() not in supported:continue
                        store.register(path,root,stat,run,'std-hash-v1',True,preserve_evidence=True)
                        if root==std:std_count+=1
                        else:other_count+=1
                        if (std_count+other_count)%1000==0:
                            store.db.commit();self.emit({'kind':'scan','scanned':std_count+other_count})
                    if self.stop.is_set():break
                store.db.commit();count=std_count+other_count;counts={'queued':count}
                store.db.execute('CREATE TABLE IF NOT EXISTS std_references(digest TEXT,path TEXT,PRIMARY KEY(digest,path))')
                store.db.execute('DELETE FROM std_references');last='';done=0
                with ThreadPoolExecutor(max_workers=4,thread_name_prefix='std-hash') as pool:
                    while not self.stop.is_set():
                        rows=store.db.execute('SELECT * FROM files WHERE seen_run=? AND source>? ORDER BY source LIMIT 128',(run,last)).fetchall()
                        if not rows:break
                        last=rows[-1]['source']
                        for row,hashed in pool.map(lambda row:digest_record(row,self.stop),rows):
                            if not hashed:
                                if self.stop.is_set():break
                                state,reason,metrics='error','changed',{};errors+=1
                            else:
                                digest,identity=hashed;is_std=Path(row['source']).is_relative_to(std)
                                metrics={'sha256':digest,'hash_identity':identity,'is_std':is_std,'in_output':is_std,'requested':not is_std,'role':'keep' if is_std else 'compare'}
                                if is_std:
                                    store.db.execute('INSERT OR IGNORE INTO std_references VALUES(?,?)',(digest,row['source']))
                                    state,reason='rejected','std_reference'
                                else:state,reason='queued',''
                            store.decision(row['source'],state,reason,metrics)
                            counts['queued']-=1;counts[state]=counts.get(state,0)+1;done+=1
                        store.db.commit();progress('基准与其他图像哈希',done,count)
                done=0
                if not self.stop.is_set():
                    for row in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='queued' ORDER BY source",(run,)):
                        if self.stop.is_set():break
                        metrics=json.loads(row['metrics'])
                        ref=store.db.execute('SELECT path FROM std_references WHERE digest=? ORDER BY path LIMIT 1',(metrics['sha256'],)).fetchone()
                        if ref:metrics.update(role='recycle',duplicate_of=ref[0]);state,reason='selected','std_duplicate'
                        else:metrics['role']='keep';state,reason='rejected','std_unmatched'
                        store.decision(row['source'],state,reason,metrics);counts['queued']-=1;counts[state]=counts.get(state,0)+1;done+=1
                        if done%128==0:store.db.commit();progress('比对基准目录',done,other_count)
                    store.db.commit();progress('比对基准目录',done,other_count)
                if mode=='move' and not issues and not self.stop.is_set():
                    done=0;total=counts.get('selected',0)
                    for row in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' ORDER BY source",(run,)):
                        if self.stop.is_set():break
                        path=Path(row['source']);metrics=json.loads(row['metrics']);digest=metrics['sha256'];stage=None
                        try:
                            if path.is_relative_to(std) or path.resolve()!=path or fingerprint(path)!=(row['size'],row['mtime']) or file_hash(path)!=digest:raise OSError('源文件变化，未回收。')
                            ref=None
                            for candidate in store.db.execute('SELECT path FROM std_references WHERE digest=? ORDER BY path',(digest,)):
                                p=Path(candidate[0])
                                try:
                                    if p.is_relative_to(std) and p.resolve()==p and file_hash(p)==digest:ref=p;break
                                except OSError:continue
                            if ref is None:raise OSError('基准目录中没有可验证的同哈希图像，副本保留。')
                            ref_fp=fingerprint(ref)
                            # Stage on the source volume, outside STD. The input
                            # path is retained in the durable movement journal.
                            stage=path.parent/'.photocleaner'/'hash_std_pending'/uuid.uuid4().hex/path.name
                            ensure_parents(stage,path.parent)
                            stage=execute_move(store,run,path,stage,(row['size'],row['mtime']),lambda _:None)
                            store.db.execute('UPDATE files SET destination=? WHERE source=?',(str(stage),str(path)));store.db.commit()
                            if file_hash(stage)!=digest or fingerprint(ref)!=ref_fp or file_hash(ref)!=digest:raise OSError('回收前核验失败，副本保留在暂存目录。')
                            self.recycler(stage)
                            store.db.execute("UPDATE files SET state='recycled',destination=? WHERE source=?",(str(ref),str(path)))
                            counts['selected']-=1;counts['recycled']=counts.get('recycled',0)+1
                        except OSError as exc:
                            metrics['error']=str(exc)
                            if stage is not None and stage.is_file():metrics['retained_path']=str(stage)
                            store.decision(str(path),'review','review',metrics);counts['selected']-=1;counts['review']=counts.get('review',0)+1;errors+=1
                            self.emit({'kind':'log','message':f'副本保留待核对：{path}；{exc}'})
                        store.db.commit();done+=1
                        if done%32==0 or done==total:progress('验证并回收匹配副本',done,total)
                summary={'task':'hash','hash_mode':'std','std_path':str(std),'std_count':std_count,'comparison_count':other_count,'run_id':run,'mode':mode,'scanned':count,
                         'counts':{k:v for k,v in counts.items() if v},'scan_issues':len(issues),'errors':errors,'stopped':self.stop.is_set(),
                         'fatal_error':'部分目录未读完，本轮未执行回收。' if issues else None,'metadata':str(metadata),'config':asdict(self.config),'roots':list(map(str,[std,*others]))}
                from .result_status import retained_counts
                summary['retained_counts']=retained_counts(store,run)
                summary['report']=str(store.export(run,metadata));summary['history']=str(metadata/'move_history.csv')
                self.emit({'kind':'done',**summary});return summary
            finally:store.close()

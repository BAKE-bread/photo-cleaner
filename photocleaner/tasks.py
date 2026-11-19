"""Independent tools share scanning, reports and safe file transfer."""
from dataclasses import asdict, dataclass
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import math
import os
import threading
import time
import uuid
import warnings

from PIL import Image
from .config import PROJECT
from .engine import normalize_inputs, normalize_paths, stream_files
from .moving import ensure_parents, execute_move, file_hash, fingerprint, recover_moves, rename_no_replace, available_destination
from .naming import naming_method
from .grouping import logical_parent,group_directory,group_summary
from .storage import Store, output_lock
from .vision import oriented_image, rgb_image
from .size_rules import DEFAULT_RULES,parse_size_rules

TASK_NAMES = {'small':'像素尺寸筛选','file_size':'文件体积筛选','meme':'表情包','photo':'实拍照片','people':'人物与活动','creative':'绘画与设计','flatten':'扁平整理','group':'命名分组','hash':'哈希去重','repair':'图像兼容修复','text_search':'文字搜图','image_search':'以图搜图','compress':'大图压缩'}
GROUP_MARKER = '.photocleaner-group'


@dataclass(frozen=True)
class TaskConfig:
    task: str = 'flatten'
    inclusive: bool = True
    retain_tree: bool = True
    dedup: bool = True
    backup_duplicates: bool = False
    repair_all: bool = False
    min_side: int = 400
    file_size_rules: str = DEFAULT_RULES
    hash_mode: str = 'consolidate'
    std_path: str = ''
    rare_count: int = 5
    rare_ratio: float = .02
    min_mb: float = 12
    min_mp: float = 10
    max_aspect: float = 16/9
    quality: int = 95
    sniff_all: bool = False
    max_pixels: int = 80_000_000

    def validate(self):
        if self.task not in ('flatten','small','file_size','group','compress','hash','repair'):
            raise ValueError('未知功能。')
        if not all(math.isfinite(v) for v in (self.rare_ratio,self.min_mb,self.min_mp,self.max_aspect)):
            raise ValueError('参数必须是有限数值。')
        if self.min_side < 1 or self.rare_count < 0 or not 0 <= self.rare_ratio <= 1:
            raise ValueError('尺寸须为正数，少量数量不能为负，少量比例范围 0–1。')
        if self.min_mb < 0 or self.min_mp < 0 or self.max_aspect < 1 or not 1 <= self.quality <= 95 or self.max_pixels < 1:
            raise ValueError('体积/像素数不能为负；比例至少为 1；JPG 质量范围 1–95。')
        if self.task=='file_size':parse_size_rules(self.file_size_rules,self.inclusive)
        if self.task=='hash':
            if self.hash_mode not in ('consolidate','std','inplace'):raise ValueError('请选择有效的哈希去重方式。')

    def key(self):
        # Revisit old rejected MPO/JPG records after the primary-image fix.
        data=asdict(self)
        if self.task!='repair':data.pop('repair_all',None)
        if self.task!='hash':
            data.pop('hash_mode',None);data.pop('std_path',None)
        if self.task=='file_size':
            # Text spacing/units and equivalent expressions do not change a decision.
            data['file_size_rules']=parse_size_rules(self.file_size_rules,self.inclusive).intervals
        else:
            # Adding this independent tool must not invalidate other tools' cache.
            data.pop('file_size_rules',None)
        if self.task in ('flatten','compress'):data['version']='primary-jpeg-recursive-output-v3'
        if self.task=='group':data['version']='append-regroup-v3-naming'
        if self.task=='repair':data['version']='windows-decode-v2'
        return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def header_decision(metrics,byte_count,config):
    """Apply inexpensive thresholds to reusable header evidence, without decode."""
    from .thresholds import lower,upper
    if config.task=='small':
        return 'selected' if upper(min(metrics['width'],metrics['height']),config.min_side,config) else 'not_small'
    if config.task=='file_size':
        rules=parse_size_rules(config.file_size_rules,config.inclusive)
        metrics.update(file_size_bytes=byte_count,file_size_rule=rules.description)
        return 'selected' if rules.matches(byte_count) else 'not_file_size'
    if config.task=='compress':
        w,h=metrics['width'],metrics['height']
        if metrics['frames']>1 and metrics['format']!='MPO':return 'animated_or_multiframe'
        if metrics['format']=='MPO':
            metrics['primary_only']=True
            metrics['container_note']='MPO/JPEG：仅压缩主图，副图不写入 JPG；原容器保留'
        if not lower(byte_count,config.min_mb*1_000_000,config) or not lower(w*h,config.min_mp*1_000_000,config):return 'not_large'
        if not upper(max(w,h)/min(w,h),config.max_aspect,config):return 'wide_aspect'
        if w*h>config.max_pixels:return 'too_many_pixels'
        metrics.update(megapixels=w*h/1_000_000,size_mb=byte_count/1_000_000)
        return 'selected'
    raise ValueError('此功能不使用头部条件重判。')


def inspect_header(path, config):
    from .thresholds import lower,upper
    metrics = {}
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(path) as im:
                w,h=im.size
                metrics.update(width=w,height=h,format=im.format,frames=getattr(im,'n_frames',1))
                # Pixel counts/aspect/short-side decisions are orientation invariant.
                if config.task in ('small','file_size','compress'):
                    return header_decision(metrics,path.stat().st_size,config),metrics
                if config.task == 'group':
                    metrics['method']=naming_method(path.name)
                    metrics['parent']=str(path.parent)
                    metrics['naming_version']='append-regroup-v3-naming'
                    return 'selected',metrics
    except (OSError,ValueError,SyntaxError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
        metrics['error']=str(exc)
        return 'unreadable_or_unsupported',metrics


def compress_copy(store,run_id,source,target,expected,config):
    if fingerprint(source)!=expected:
        raise OSError('源文件发生变化，未压缩。')
    with warnings.catch_warnings(record=True) as decode_warnings, Image.open(source) as original:
        if original.format=='MPO':original.seek(0)
        elif getattr(original,'n_frames',1)>1:raise OSError('动图或多帧图像不压缩。')
        original.load()
        metrics={}
        oriented=oriented_image(original,metrics)
        messages=[str(w.message) for w in decode_warnings if 'exif' in str(w.message).lower()]
        if messages:metrics['exif_warning']='; '.join(messages)
        pixels=rgb_image(oriented)
        exif=oriented.info.get('exif')
        icc=oriented.info.get('icc_profile') if oriented.mode in ('RGB','RGBA','L') else None
        oriented.close()
        if fingerprint(source)!=expected:
            pixels.close()
            raise OSError('源文件发生变化，未压缩。')
        target=available_destination(target)
        staging=target.with_name(target.name+'.'+uuid.uuid4().hex+'.partial')
        copy_id=store.db.execute('INSERT INTO copies(run_id,source,destination,phase,staging) VALUES(?,?,?,?,?)',
                                 (run_id,str(source),str(target),'writing',str(staging))).lastrowid
        store.db.commit()
        try:
            kwargs={'quality':config.quality,'subsampling':0,'optimize':False}
            if exif and not metrics.get('exif_warning'):
                kwargs['exif']=exif
            if icc:
                kwargs['icc_profile']=icc
            with staging.open('xb') as handle:
                pixels.save(handle,format='JPEG',**kwargs)
                handle.flush(); os.fsync(handle.fileno())
            with Image.open(staging) as check:
                check.load()
                if check.size!=pixels.size or check.format!='JPEG':
                    raise OSError('压缩结果校验失败；原图及临时文件保留。')
            if fingerprint(source)!=expected:
                raise OSError('压缩期间源文件改变；原图及临时文件保留。')
            digest=file_hash(staging)
            store.db.execute("UPDATE copies SET digest=?,phase='verified' WHERE id=?",(digest,copy_id)); store.db.commit()
            while True:
                try:
                    rename_no_replace(staging,target)
                    break
                except FileExistsError:
                    target=available_destination(target)
                    store.db.execute('UPDATE copies SET destination=? WHERE id=?',(str(target),copy_id)); store.db.commit()
            store.db.execute("UPDATE copies SET phase='done' WHERE id=?",(copy_id,)); store.db.commit()
            metrics.update(output_bytes=target.stat().st_size,output_width=pixels.width,output_height=pixels.height,jpg_quality=config.quality,subsampling='4:4:4',output_sha256=digest,
                           output_fingerprint=list(fingerprint(target)),copy_retain_tree=config.retain_tree)
            return target,metrics
        except Exception as exc:
            store.db.execute("UPDATE copies SET phase='review',error=? WHERE id=?",(str(exc),copy_id)); store.db.commit()
            raise
        finally:
            pixels.close()


def recover_copies(store,log,config):
    for row in store.db.execute("SELECT c.* FROM copies c JOIN files f ON f.source=c.source WHERE f.config_key=? AND f.state<>'copied' AND c.phase IN ('writing','verified','review','done')",(config.key(),)).fetchall():
        dest=Path(row['destination'])
        try:
            valid=bool(row['digest'] and dest.is_file() and file_hash(dest)==row['digest'])
            if valid:
                store.db.execute("UPDATE copies SET phase='done' WHERE id=?",(row['id'],))
                store.db.execute("UPDATE files SET state='copied',destination=?,reason='compressed' WHERE source=? AND state IN ('selected','error','review')",(str(dest),row['source']))
                info=store.db.execute('SELECT metrics,size,mtime FROM files WHERE source=?',(row['source'],)).fetchone()
                if info:
                    metrics=json.loads(info['metrics']);metrics.update(source_fingerprint=[info['size'],info['mtime']],output_fingerprint=list(fingerprint(dest)))
                    store.db.execute('UPDATE files SET metrics=? WHERE source=?',(json.dumps(metrics,ensure_ascii=False),row['source']))
            elif row['phase']!='done':
                store.db.execute("UPDATE copies SET phase='review' WHERE id=?",(row['id'],))
                store.db.execute("UPDATE files SET state='review',reason='review' WHERE source=?",(row['source'],))
                log(f"压缩副本需复查，原图和临时文件均保留：{row['source']}")
        except OSError:
            pass
    store.db.commit()


class TaskRunner:
    def __init__(self,config,emit=None,stop=None):
        self.config=config
        from .runtime import ProgressEvents
        self.emit=ProgressEvents(emit or (lambda _:None))
        self.stop=stop or threading.Event()
        self.issues=0

    def log(self,text):
        self.emit({'kind':'log','message':text})

    def issue(self,text):
        self.issues+=1
        if self.issues<=100:self.log(text)

    def locations(self,inputs,output):
        if self.config.task=='group':
            roots=normalize_inputs(inputs)
            key=hashlib.sha256(json.dumps(list(map(str,roots))).encode()).hexdigest()[:16]
            return roots,None,PROJECT/'.jobs'/'group'/key
        roots,out=normalize_paths(inputs,output)
        return roots,out,out/'.photocleaner'/self.config.task

    def run(self,inputs,output=None,mode='preview',resume=True):
        if self.config.task=='repair':
            from .repair import RepairRunner
            return RepairRunner(self.config,self.emit,self.stop).run(inputs,output,mode,resume)
        self.config.validate()
        if mode not in ('preview','move'):raise ValueError('模式应为 preview 或 move。')
        if self.config.task in ('small','file_size') and not output:
            if mode=='move':raise ValueError('执行剪切前请选择输出目录。')
            from .preview_state import preview_output
            output=preview_output(self.config.task,normalize_inputs(inputs))
        roots,out,metadata=self.locations(inputs,output)
        self.log('运行提示：请勿在程序运行时对相关文件夹自行操作。')
        if self.config.task=='file_size':self.log('文件体积条件：'+parse_size_rules(self.config.file_size_rules,self.config.inclusive).description+'；KB/MB 使用十进制字节。')
        if out:out.mkdir(parents=True,exist_ok=True)
        if metadata.is_symlink() or getattr(metadata,'is_junction',lambda:False)():raise ValueError('记录目录不能是链接。')
        if out:ensure_parents(metadata/'state.sqlite3',out)
        with ExitStack() as locks:
            if out:locks.enter_context(output_lock(out/'.photocleaner'))
            locks.enter_context(output_lock(metadata))
            store=Store(metadata/'state.sqlite3')
            try:return self._run(store,roots,out,metadata,mode,resume)
            finally:store.close()

    def _run(self,store,roots,out,metadata,mode,resume):
        raw_filter=self.config.task in ('small','file_size','flatten','group','compress')
        identity=[self.config.task,'header-evidence-v1'] if raw_filter else self.config.key()
        key=hashlib.sha256(json.dumps([list(map(str,roots)),str(out),identity]).encode()).hexdigest()
        session=store.session(key,resume,mode,reuse_preview=True)
        if not session and resume and self.config.task=='compress':
            # Compression keeps every input: completed execution has a reusable
            # read-only source snapshot too, unlike actual move operations.
            candidate=store.db.execute('SELECT * FROM sessions WHERE key=?',(key,)).fetchone()
            if candidate and candidate['scan_complete'] and store.snapshot_unchanged(candidate['run_id'],candidate['scanned']):session=candidate
        if session and raw_filter:
            previous=store.db.execute('SELECT mode,config FROM runs WHERE id=?',(session['run_id'],)).fetchone()
            # A changed operation after a partly executed move starts a new
            # current-file snapshot; previous successful moves keep their journal.
            if previous and previous['mode']!='preview' and self.config.task!='compress':
                old=json.loads(previous['config'])
                old_config=TaskConfig(**{k:v for k,v in old.items() if k in TaskConfig.__dataclass_fields__})
                if old_config.key()!=self.config.key():session=None
        if session and session['scan_complete']:
            previous=store.db.execute('SELECT mode FROM runs WHERE id=?',(session['run_id'],)).fetchone()
            if previous and previous[0]=='preview' and not store.snapshot_unchanged(session['run_id'],session['scanned']):session=None
        run_id=session['run_id'] if session else time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
        if self.config.task=='compress':
            # Backfill old copy layout before the current run configuration is
            # replaced. Quality comes from actual JPG metrics, never run.config.
            store.db.execute("""UPDATE files SET metrics=json_set(metrics,'$.copy_retain_tree',COALESCE((
                SELECT json_extract(r.config,'$.retain_tree') FROM copies c JOIN runs r ON r.id=c.run_id
                WHERE c.source=files.source AND c.phase='done' ORDER BY c.id DESC LIMIT 1),1))
                WHERE json_type(metrics,'$.output_fingerprint') IS NOT NULL AND json_type(metrics,'$.copy_retain_tree') IS NULL""")
        store.db.execute('INSERT INTO runs VALUES(?,?,?,?,NULL,NULL) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,config=excluded.config',
                         (run_id,time.time(),json.dumps(asdict(self.config)),mode));store.db.commit()
        recover_moves(store,self.log)
        if session and raw_filter:
            # Changing only thresholds or output layout reuses the file list and
            # original header data; file fingerprints are checked before applying.
            store.db.execute("UPDATE files SET state='queued',reason='',destination=NULL WHERE seen_run=? AND config_key<>? AND state IN ('selected','rejected','copied','grouped') AND (reason<>'non_image_extension' OR ?)",(run_id,self.config.key(),self.config.sniff_all))
            store.db.execute("UPDATE files SET config_key=? WHERE seen_run=? AND state<>'review'",(self.config.key(),run_id));store.db.commit()
        self.emit({'kind':'snapshot','run_id':run_id,'mode':mode,'scanned':0,'roots':list(map(str,roots)),'output':str(out) if out else None,'report':str(metadata/f'report_{run_id}.csv'),'config':asdict(self.config),'live':True})
        if self.config.task=='compress':recover_copies(store,self.log,self.config)
        count=store.db.execute('SELECT COUNT(*) FROM files WHERE seen_run=?',(run_id,)).fetchone()[0] if session else 0
        self.issues=session['scan_issues'] if session else 0
        need_scan=not session or not session['scan_complete']
        self.log('扫描中继：仅继续未完成目录。' if session and need_scan else '继续阶段：复用扫描清单和已有判定。' if session else '扫描阶段：递归读取输入目录。')
        store.session_update(key,run_id,count,self.issues,not need_scan,'running')
        if need_scan:
            supported=set(Image.registered_extensions())|{'.heic','.heif','.avif'}
            for root in roots:
                exclusions=[metadata,PROJECT/'models',PROJECT/'.jobs']+([out] if out else [])
                for path,stat in stream_files(root,exclusions,self.stop,self.issue,store=store,run_id=run_id):
                    supported_file=self.config.task=='flatten' or self.config.sniff_all or not path.suffix or path.suffix.lower() in supported
                    store.register(path,root,stat,run_id,self.config.key(),supported_file,preserve_evidence=raw_filter)
                    count+=1
                    if count%1000==0:
                        store.session_update(key,run_id,count,self.issues,False,'running');self.emit({'kind':'scan','scanned':count})
                if self.stop.is_set():break
        scan_errors=store.db.execute("SELECT COUNT(*) FROM scan_dirs WHERE run_id=? AND state='error'",(run_id,)).fetchone()[0]
        complete=not self.stop.is_set() and not scan_errors
        store.session_update(key,run_id,count,self.issues,complete,'running')
        self.emit({'kind':'scan','scanned':count,'complete':complete})
        if self.config.task=='compress':
            # Re-check copied output fingerprints on reuse; this only reads
            # metadata and cached headers, and detects missing/edited copies.
            store.db.execute("UPDATE files SET state='queued',reason='' WHERE seen_run=? AND state='copied'",(run_id,));store.db.commit()
        error=None
        group_plan_complete=False
        try:
            if scan_errors:raise OSError('部分目录读取未完成；清单已保留，重连或解决权限后点击继续。')
            self.flat_dedup=None
            if (self.config.task=='flatten' and self.config.dedup) or (self.config.task in ('small','file_size') and (self.config.backup_duplicates or not self.config.retain_tree)):
                from .flat_dedup import FlatDedup
                self.flat_dedup=FlatDedup(store,out,self.stop,self.issue,self.emit)
                # Output contents can change independently of input directory
                # stamps. Revisit only duplicate dispositions using hash cache.
                store.db.execute("UPDATE files SET state='selected',reason='selected' WHERE seen_run=? AND state='rejected' AND reason='exact_duplicate'",(run_id,));store.db.commit()
            self._decide(store,run_id)
            if self.config.task=='group' and not self.stop.is_set():
                self._group_labels(store,run_id);group_plan_complete=True
            if mode=='move' and not self.stop.is_set():self._execute(store,run_id,out)
        except Exception as exc:
            error=str(exc);self.log(f'任务中断：{exc}。已完成进度保留。')
        counts=self._counts(store,run_id)
        summary={'task':self.config.task,'run_id':run_id,'mode':mode,'scanned':count,'counts':counts,'scan_issues':self.issues,
                 'stopped':self.stop.is_set(),'fatal_error':error,'metadata':str(metadata),
                 'config':asdict(self.config),'roots':list(map(str,roots)),'output':str(out) if out else None}
        if self.config.task=='group':summary.update(grouping=group_summary(store,run_id),group_plan_complete=group_plan_complete)
        summary['reasons']={r[0]:r[1] for r in store.db.execute('SELECT reason,COUNT(*) FROM files WHERE seen_run=? GROUP BY reason',(run_id,))}
        store.db.execute('UPDATE runs SET finished=?,summary=? WHERE id=?',(time.time(),json.dumps(summary,ensure_ascii=False),run_id))
        store.session_update(key,run_id,count,self.issues,complete,'paused' if self.stop.is_set() else 'error' if error else 'finished')
        summary['report']=str(store.export(run_id,metadata));summary['history']=str(metadata/'move_history.csv')
        self.emit({'kind':'done',**summary});return summary

    def _counts(self,store,run_id):
        return {r[0]:r[1] for r in store.db.execute('SELECT state,COUNT(*) FROM files WHERE seen_run=? GROUP BY state',(run_id,))}

    def _valid_copy(self,store,row,metrics):
        if metrics.get('jpg_quality')!=self.config.quality or bool(metrics.get('copy_retain_tree',True))!=self.config.retain_tree:return None
        if metrics.get('source_fingerprint')!=[row['size'],row['mtime']]:return None
        copy=store.db.execute("SELECT destination FROM copies WHERE source=? AND phase='done' ORDER BY id DESC LIMIT 1",(row['source'],)).fetchone()
        if not copy:return None
        destination=Path(copy[0])
        try:
            if destination.resolve()!=destination or metrics.get('output_fingerprint')!=list(fingerprint(destination)):return None
        except OSError:return None
        return str(destination)

    def _decide(self,store,run_id):
        supported=set(Image.registered_extensions())|{'.heic','.heif','.avif'}
        total=store.db.execute("SELECT COUNT(*) FROM files WHERE seen_run=? AND state='queued'",(run_id,)).fetchone()[0]
        done=0;last='';counts=self._counts(store,run_id)
        def record(source,state,reason,metrics,before):
            store.decision(source,state,reason,metrics)
            if before!=state:
                counts[before]=counts.get(before,0)-1;counts[state]=counts.get(state,0)+1
        self.emit({'kind':'progress','processed':0,'total':total,'phase':'判定','counts':dict(counts)})
        while not self.stop.is_set():
            rows=store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='queued' AND source>? ORDER BY source LIMIT 512",(run_id,last)).fetchall()
            if not rows:break
            last=rows[-1]['source']
            for row in rows:
                if self.stop.is_set():break
                path=Path(row['source'])
                try:
                    if fingerprint(path)!=(row['size'],row['mtime']):raise OSError('源文件改变。')
                    if self.config.task=='flatten':
                        reason,metrics='selected',json.loads(row['metrics'])
                        metrics.pop('duplicate_of',None)
                    elif self.config.task in ('small','file_size','compress','group'):
                        metrics=json.loads(row['metrics'])
                        eligible=self.config.sniff_all or not path.suffix or path.suffix.lower() in supported
                        if not eligible:reason,metrics='non_image_extension',{}
                        elif all(k in metrics for k in ('width','height','format','frames')):
                            if self.config.task=='group':
                                reason='selected'
                                if metrics.get('naming_version')!='append-regroup-v3-naming':
                                    metrics.update(method=naming_method(path.name),naming_version='append-regroup-v3-naming')
                            else:reason=header_decision(metrics,row['size'],self.config)
                        else:reason,metrics=inspect_header(path,self.config)
                    else:reason,metrics=inspect_header(path,self.config)
                    if self.config.task=='group' and reason=='selected' and not metrics.get('parent'):metrics['parent']=str(logical_parent(path,Path(row['root'])))
                    # First inspection records physical parent; remove managed
                    # grouping folders once, then reuse this logical directory.
                    if self.config.task=='group' and reason=='selected' and not metrics.get('logical_parent'):
                        metrics['parent']=str(logical_parent(path,Path(row['root'])));metrics['logical_parent']=True
                    destination=self._valid_copy(store,row,metrics) if self.config.task=='compress' and reason=='selected' else None
                    state='copied' if destination else 'selected' if reason=='selected' else 'rejected'
                    record(str(path),state,'compressed' if destination else reason,metrics,row['state'])
                    if destination:store.db.execute('UPDATE files SET destination=? WHERE source=?',(destination,str(path)))
                except OSError as exc:record(str(path),'error','error',{'error':str(exc)},row['state'])
                done+=1
            store.db.commit()
            self.emit({'kind':'progress','processed':done,'total':total,'phase':'判定','counts':dict(counts)})
        # Flatten content dedup must also work in preview. Include cached selected
        # rows, in deterministic order, and never remove any duplicate file.
        if self.flat_dedup is not None and not self.stop.is_set():
            last='';index_count=0
            index_total=store.db.execute("SELECT COUNT(*) FROM files WHERE seen_run=? AND state='selected'",(run_id,)).fetchone()[0]
            while not self.stop.is_set():
                rows=store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' AND source>? ORDER BY source LIMIT 64",(run_id,last)).fetchall()
                if not rows:break
                last=rows[-1]['source']
                self.flat_dedup.prime(rows)
                for row in rows:
                    if self.stop.is_set():break
                    p=Path(row['source'])
                    try:
                        if fingerprint(p)!=(row['size'],row['mtime']):raise OSError('源文件改变。')
                        ref,digest=self.flat_dedup.duplicate(p,(row['size'],row['mtime']))
                        metrics=json.loads(row['metrics']);metrics['sha256']=digest
                        if ref:
                            metrics['duplicate_of']=ref
                            backup=self.config.backup_duplicates
                            record(str(p),'selected' if backup else 'rejected','backup_duplicate' if backup else 'exact_duplicate',metrics,row['state'])
                        else:
                            record(str(p),'selected','selected',metrics,row['state'])
                        index_count+=1
                    except OSError as exc:record(str(p),'error','error',{'error':str(exc)},row['state'])
                store.db.commit()
                self.emit({'kind':'progress','processed':index_count,'total':index_total,'phase':'内容去重','counts':dict(counts)})

    def _group_labels(self,store,run_id):
        store.db.execute("UPDATE files SET state='selected' WHERE seen_run=? AND state='grouped'",(run_id,))
        # Group by the actual containing directory; neighboring folders never
        # determine each other's rare-source thresholds.
        store.db.execute("""CREATE TEMP TABLE naming_counts AS WITH base AS (
            SELECT json_extract(metrics,'$.parent') parent,json_extract(metrics,'$.method') method,COUNT(*) n
            FROM files WHERE seen_run=? AND state IN ('selected','moved') GROUP BY parent,method)
            SELECT *,SUM(n) OVER(PARTITION BY parent) total FROM base""",(run_id,))
        store.db.execute('CREATE INDEX naming_count_lookup ON naming_counts(parent,method)')
        store.db.execute("""UPDATE files SET metrics=json_set(metrics,'$.category',(
            SELECT CASE WHEN n{comparison}? OR CAST(n AS REAL)/total{comparison}? THEN '其他' ELSE method END
            FROM naming_counts WHERE parent=json_extract(files.metrics,'$.parent') AND method=json_extract(files.metrics,'$.method')))
            WHERE seen_run=? AND state='selected'""".format(comparison="<=" if self.config.inclusive else "<"),(self.config.rare_count,self.config.rare_ratio,run_id))
        store.db.commit()
        for row in store.db.execute("SELECT source,metrics FROM files WHERE seen_run=? AND state='selected'",(run_id,)):
            metrics=json.loads(row['metrics']);source=Path(row['source'])
            target=group_directory(metrics['parent'],metrics['category'])/source.name
            if target==source:
                store.db.execute("UPDATE files SET state='grouped',reason='already_grouped',destination=? WHERE source=?",(str(target),str(source)))
            else:store.db.execute('UPDATE files SET destination=? WHERE source=?',(str(target),str(source)))
        store.db.commit()

    def _execute(self,store,run_id,out):
        total=store.db.execute("SELECT COUNT(*) FROM files WHERE seen_run=? AND state='selected'",(run_id,)).fetchone()[0]
        done=0;last='';counts=self._counts(store,run_id)
        def progressed(state):
            counts['selected']=counts.get('selected',0)-1;counts[state]=counts.get(state,0)+1
        self.emit({'kind':'progress','processed':0,'total':total,'phase':'压缩' if self.config.task=='compress' else '移动','counts':dict(counts)})
        while not self.stop.is_set():
            rows=store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' AND source>? ORDER BY source LIMIT 32",(run_id,last)).fetchall()
            if not rows:break
            last=rows[-1]['source']
            for row in rows:
                if self.stop.is_set():break
                source=Path(row['source']);metrics=json.loads(row['metrics'])
                try:
                    if source.resolve()!=source:raise OSError('源路径包含链接。')
                    if self.config.task=='group':
                        category=metrics.get('category','其他')
                        parent=Path(metrics['parent']);directory=group_directory(parent,category)
                        target=directory/source.name
                        ensure_parents(target,Path(row['root']))
                        marker=target.parent/GROUP_MARKER
                        if not marker.exists():marker.write_text('PhotoCleaner naming group\n',encoding='utf-8')
                    elif row['reason']=='backup_duplicate':
                        target=out/'重复副本备份'/source.name;ensure_parents(target,out)
                    elif self.config.task=='flatten' or not self.config.retain_tree:
                        target=(out/'重复副本备份'/source.name) if row['reason']=='backup_duplicate' else out/source.name
                        ensure_parents(target,out)
                    else:
                        root=Path(row['root']);bucket=root.name+'_'+hashlib.sha256(str(root).encode()).hexdigest()[:8]
                        target=out/bucket/row['relative'];ensure_parents(target,out)
                    if self.config.task=='compress':
                        target=target.with_suffix('.jpg')
                        target,extra=compress_copy(store,run_id,source,target,(row['size'],row['mtime']),self.config)
                        metrics.update(extra);metrics['source_fingerprint']=[row['size'],row['mtime']]
                        if metrics.get('exif_warning'):self.log(f"元数据提示：{source.name} 的 EXIF 不完整；原图保留，JPG 副本不写入损坏 EXIF。")
                        store.decision(str(source),'copied','compressed',metrics)
                        success_state='copied'
                    else:
                        if self.flat_dedup is not None and row['reason']!='backup_duplicate':
                            duplicate,digest=self.flat_dedup.duplicate(source,(row['size'],row['mtime']))
                            if duplicate:
                                if self.config.backup_duplicates:
                                    target=out/'重复副本备份'/source.name;ensure_parents(target,out)
                                    metrics.update(duplicate_of=duplicate,sha256=digest)
                                    store.decision(str(source),'selected','backup_duplicate',metrics)
                                else:
                                    metrics.update(duplicate_of=duplicate,sha256=digest)
                                    store.decision(str(source),'rejected','exact_duplicate',metrics);store.db.commit();progressed('rejected');done+=1;continue
                        target=execute_move(store,run_id,source,target,(row['size'],row['mtime']),self.log)
                        store.db.execute("UPDATE files SET state='moved' WHERE source=?",(str(source),))
                        success_state='moved'
                        if self.flat_dedup is not None and metrics.get('sha256') and row['reason']!='backup_duplicate' and target.parent!=out/'重复副本备份':
                            store.db.execute('INSERT OR REPLACE INTO content_index VALUES(?,?)',(metrics['sha256'],str(target)))
                    store.db.execute('UPDATE files SET destination=? WHERE source=?',(str(target),str(source)));store.db.commit()
                    progressed(success_state)
                except (OSError,ValueError) as exc:
                    review=store.db.execute("SELECT 1 FROM moves WHERE source=? AND phase='review' UNION ALL SELECT 1 FROM copies WHERE source=? AND phase='review' LIMIT 1",(str(source),str(source))).fetchone()
                    state='review' if review else 'error';metrics['error']=str(exc)
                    store.decision(str(source),state,state,metrics);store.db.commit()
                    progressed(state)
                    self.log(f'文件保留：{source}；原因：{exc}')
                done+=1
            self.emit({'kind':'progress','processed':done,'total':total,'phase':'压缩' if self.config.task=='compress' else '移动','counts':dict(counts)})

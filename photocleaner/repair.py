"""Create Windows Photos compatible copies without altering any source file."""
from pathlib import Path
from dataclasses import asdict
import hashlib,io,json,os,shutil,threading,time,uuid,warnings
from PIL import Image,ImageOps
from .engine import stream_files
from .config import PROJECT
from .storage import Store,output_lock
from .moving import fingerprint,file_hash,ensure_parents,available_destination,rename_no_replace
from .windows_decode import probe_image,PHOTO_SUFFIXES,PROBE_VERSION

EXTENSIONS={'JPEG':{'.jpg','.jpeg','.jpe','.jfif'},'PNG':{'.png'},'BMP':{'.bmp','.dib'},'GIF':{'.gif'},'TIFF':{'.tif','.tiff'}}
SUFFIX={'JPEG':'.jpg','PNG':'.png','BMP':'.bmp','GIF':'.gif','TIFF':'.tif','WEBP':'.webp','AVIF':'.avif','HEIF':'.heic','HEIC':'.heic'}
EVIDENCE_VERSION=2

def repair_plan(path,metrics,config):
    fmt=metrics['format'];frames=metrics['frames'];status=metrics.get('windows_decode_status','unavailable')
    for key in ('repair_action','repair_format','repair_suffix','repair_method'):metrics.pop(key,None)
    recognized=path.suffix.lower() in PHOTO_SUFFIXES or path.suffix.lower() in metrics.get('windows_extensions',[])
    metrics['filename_check']='可用图像后缀' if recognized else '缺少后缀' if not path.suffix else '非图像后缀'
    metrics['repair_basis']=''
    if not config.repair_all and recognized and status=='passed':
        metrics['repair_basis']='Windows 系统可解码；'+('后缀和编码不同不影响本轮判断' if path.suffix.lower() not in EXTENSIONS.get(fmt,{path.suffix.lower()}) else '后缀可用')
        return 'repair_unneeded',{}
    if not config.repair_all and recognized and status=='unavailable':
        metrics['repair_basis']='系统检查未完成；可按需开启强制生成副本'
        return 'repair_unverified',{}
    if not config.repair_all and not recognized and fmt in SUFFIX and status!='failed':
        metrics['repair_basis']=metrics['filename_check']+'；补全文件名以便直接打开'
        return 'selected',{'repair_action':'补全可打开的后缀，复制原编码','repair_format':fmt,'repair_suffix':SUFFIX[fmt],'repair_method':'copy'}
    if frames>1:
        metrics['repair_basis']='需要重编码但包含多帧，不自动丢弃后续帧'
        return 'repair_multiframe',{}
    metrics['repair_basis']='按用户选择强制生成显示副本' if config.repair_all else 'Windows 系统无法解码，改为普通 PNG'
    # PNG avoids further lossy compression and preserves transparency.
    return 'selected',{'repair_action':'重新编码为 PNG','repair_format':'PNG','repair_suffix':'.png','repair_method':'encode'}

def inspect_repair(path,config):
    metrics={}
    try:
        if path.stat().st_size==0:return 'empty_file',{'error':'空文件，没有可解码的图像数据'},None
        with warnings.catch_warnings(record=True) as notices,Image.open(path) as image:
            if image.width*image.height>config.max_pixels:return 'too_many_pixels',metrics,None
            metrics.update(format=image.format,width=image.width,height=image.height,frames=getattr(image,'n_frames',1),image_mode=image.mode,repair_evidence=EVIDENCE_VERSION)
            if metrics['frames']>256:return 'repair_multiframe',metrics,None
            # A compatible copy needs a full decode, not just a readable header.
            for frame in range(metrics['frames']):
                image.seek(frame);image.load()
            image.seek(0);thumb=ImageOps.exif_transpose(image);thumb.thumbnail((256,180));thumb=thumb.convert('RGB')
            buf=io.BytesIO();thumb.save(buf,format='JPEG',quality=80);thumb.close()
            if notices:metrics['metadata_warning']='; '.join(str(n.message) for n in notices)
        check=probe_image(path,config.max_pixels)
        metrics.update(windows_decode_status=check['status'],windows_decode={'passed':'通过','failed':'未通过','unavailable':'未检查'}.get(check['status'],'未检查'),windows_decode_detail=check['detail'],windows_probe_version=PROBE_VERSION,windows_extensions=check.get('extensions',[]))
        reason,plan=repair_plan(path,metrics,config);metrics.update(plan)
        return reason,metrics,buf.getvalue()
    except (OSError,ValueError,SyntaxError,Image.DecompressionBombError) as exc:
        metrics['error']=str(exc);return 'unreadable_or_unsupported',metrics,None

def repair_copy(store,run,source,target,expected,metrics):
    if source.resolve()!=source or fingerprint(source)!=expected:raise OSError('源文件已变化，未修复。')
    source_digest=file_hash(source);target=available_destination(target)
    staging=target.with_name(target.name+'.'+uuid.uuid4().hex+'.partial')
    identity=store.db.execute('INSERT INTO copies(run_id,source,destination,phase,staging) VALUES(?,?,?,?,?)',(run,str(source),str(target),'writing',str(staging))).lastrowid;store.db.commit()
    try:
        output_size=None
        with staging.open('xb') as handle:
            if metrics['repair_method']=='copy':
                with source.open('rb') as original:shutil.copyfileobj(original,handle,1024*1024)
            else:
                with warnings.catch_warnings(record=True),Image.open(source) as original:
                    original.load();pixels=ImageOps.exif_transpose(original)
                    try:
                        converted=pixels.convert('RGBA' if 'A' in pixels.getbands() or 'transparency' in pixels.info else 'RGB')
                        output_size=converted.size
                        try:
                            # Fresh pixel container deliberately omits malformed
                            # EXIF/text chunks and format-specific side data.
                            clean=Image.frombytes(converted.mode,converted.size,converted.tobytes())
                            try:clean.save(handle,format='PNG')
                            finally:clean.close()
                        finally:converted.close()
                    finally:pixels.close()
            handle.flush();os.fsync(handle.fileno())
        with Image.open(staging) as check:
            check.load()
            if check.format!=metrics['repair_format']:raise OSError('输出格式校验失败。')
            if output_size is not None and check.size!=output_size:raise OSError('输出像素尺寸校验失败。')
            if metrics['repair_method']=='copy' and check.size!=(metrics['width'],metrics['height']):raise OSError('输出像素尺寸校验失败。')
        windows_check=probe_image(staging,metrics['width']*metrics['height'])
        if windows_check['status']=='failed':raise OSError('生成的副本未通过 Windows 系统解码：'+windows_check['detail'])
        digest=file_hash(staging)
        if metrics['repair_method']=='copy' and digest!=source_digest:raise OSError('副本内容校验失败。')
        if fingerprint(source)!=expected or file_hash(source)!=source_digest:raise OSError('源文件在修复期间变化；现有文件保留。')
        store.db.execute("UPDATE copies SET phase='verified',digest=? WHERE id=?",(digest,identity));store.db.commit()
        while True:
            try:rename_no_replace(staging,target);break
            except FileExistsError:target=available_destination(target)
        if file_hash(target)!=digest:raise OSError('发布后校验失败；原图和输出均保留。')
        store.db.execute("UPDATE copies SET phase='done',destination=? WHERE id=?",(str(target),identity));store.db.commit()
        return target,{'output_sha256':digest,'output_fingerprint':list(fingerprint(target)),'source_sha256':source_digest,'repair_recipe':metrics['repair_method']+':'+metrics['repair_format'],'output_windows_decode':'通过' if windows_check['status']=='passed' else '未检查'}
    except Exception as exc:
        store.db.execute("UPDATE copies SET phase='review',error=? WHERE id=?",(str(exc),identity));store.db.commit();raise

class RepairRunner:
    def __init__(self,config,emit=None,stop=None):self.config=config;self.emit=emit or (lambda _:None);self.stop=stop or threading.Event()
    def run(self,inputs,output,mode='preview',resume=True):
        from .tasks import TaskRunner
        self.config.validate()
        if mode not in ('preview','move'):raise ValueError('操作模式无效。')
        roots,out,metadata=TaskRunner(self.config).locations(inputs,output);out.mkdir(parents=True,exist_ok=True);ensure_parents(metadata/'state.sqlite3',out)
        run=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6];issues=[];scanned=0;fatal=None;reused=0
        with output_lock(out/'.photocleaner'),output_lock(metadata):
            store=Store(metadata/'state.sqlite3')
            try:
                # Interrupted writes are retained for review, never silently
                # regenerated or removed on the next session.
                store.db.execute("UPDATE files SET state='review',reason='review' WHERE source IN (SELECT source FROM copies WHERE phase<>'done')")
                store.db.execute('INSERT INTO runs VALUES(?,?,?,?,NULL,NULL)',(run,time.time(),json.dumps(asdict(self.config)),mode));store.db.commit()
                for root in roots:
                    for path,stat in stream_files(root,[out,PROJECT/'models',PROJECT/'.jobs'],self.stop,issues.append):
                        store.register(path,root,stat,run,self.config.key(),True,preserve_evidence=True);scanned+=1
                        if scanned%1000==0:store.db.commit();self.emit({'kind':'scan','scanned':scanned})
                    if self.stop.is_set():break
                store.db.commit();last='';done=0
                self.emit({'kind':'snapshot','run_id':run,'mode':mode,'report':str(metadata/f'report_{run}.csv'),'metadata':str(metadata),'output':str(out),'roots':list(map(str,roots)),'config':asdict(self.config),'scanned':scanned})
                while not self.stop.is_set():
                    rows=store.db.execute('SELECT * FROM files WHERE seen_run=? AND source>? ORDER BY source LIMIT 64',(run,last)).fetchall()
                    if not rows:break
                    last=rows[-1]['source']
                    for row in rows:
                        if self.stop.is_set():break
                        source=Path(row['source']);metrics=json.loads(row['metrics'] or '{}');thumb=row['thumb']
                        if row['state']=='review':done+=1;continue
                        try:
                            if source.resolve()!=source or fingerprint(source)!=(row['size'],row['mtime']):raise OSError('源文件变化。')
                            old_recipe=metrics.get('repair_recipe')
                            compatible=metrics.get('repair_evidence')==EVIDENCE_VERSION and metrics.get('windows_probe_version')==PROBE_VERSION and resume
                            if compatible and metrics['width']*metrics['height']>self.config.max_pixels:reason='too_many_pixels'
                            elif compatible and metrics.get('frames',1)<=256:
                                reason,plan=repair_plan(source,metrics,self.config);metrics.update(plan);reused+=1
                            else:reason,metrics,thumb=inspect_repair(source,self.config)
                            state='selected' if reason=='selected' else 'rejected';destination=None
                            if state=='selected':
                                old=store.db.execute("SELECT * FROM copies WHERE source=? AND phase='done' ORDER BY id DESC LIMIT 1",(str(source),)).fetchone()
                                if old and old_recipe==metrics['repair_method']+':'+metrics['repair_format'] and metrics.get('output_sha256') and row['destination']:
                                    old_path=Path(row['destination'])
                                    if old_path.resolve()==old_path and old_path.is_file() and file_hash(old_path)==metrics['output_sha256']:
                                        result=probe_image(old_path,self.config.max_pixels) if metrics.get('output_windows_decode')!='通过' else {'status':'passed'}
                                        if result['status']!='failed':
                                            metrics['output_windows_decode']='通过' if result['status']=='passed' else '未检查'
                                            state='copied';reason='repaired';destination=old_path
                            store.decision(str(source),state,reason,metrics,thumb)
                            if mode=='move' and state=='selected' and not issues:
                                root=Path(row['root']);bucket=root.name+'_'+hashlib.sha256(str(root).encode()).hexdigest()[:8]
                                target=out/bucket/row['relative'] if self.config.retain_tree else out/source.name
                                target=target.with_suffix(metrics['repair_suffix']);ensure_parents(target,out)
                                destination,extra=repair_copy(store,run,source,target,(row['size'],row['mtime']),metrics);metrics.update(extra)
                                state='copied';reason='repaired';store.decision(str(source),state,reason,metrics,thumb)
                            if destination is not None:store.db.execute('UPDATE files SET destination=? WHERE source=?',(str(destination),str(source)))
                        except (OSError,ValueError) as exc:
                            metrics['error']=str(exc);review=store.db.execute("SELECT 1 FROM copies WHERE source=? AND phase='review'",(str(source),)).fetchone()
                            store.decision(str(source),'review' if review else 'error','review' if review else 'error',metrics,thumb)
                        done+=1
                    store.db.commit();self.emit({'kind':'progress','phase':'兼容副本生成' if mode=='move' else '兼容性检查','processed':done,'total':scanned,'counts':{r[0]:r[1] for r in store.db.execute('SELECT state,COUNT(*) FROM files WHERE seen_run=? GROUP BY state',(run,))}})
                if issues:fatal='部分目录未读取完成，本轮未生成副本。'
                summary={'task':'repair','run_id':run,'mode':mode,'scanned':scanned,'counts':{r[0]:r[1] for r in store.db.execute('SELECT state,COUNT(*) FROM files WHERE seen_run=? GROUP BY state',(run,))},'stopped':self.stop.is_set(),'fatal_error':fatal,'scan_issues':len(issues),'metadata':str(metadata),'output':str(out),'roots':list(map(str,roots)),'config':asdict(self.config),'reused_headers':reused}
                store.db.execute('UPDATE runs SET finished=?,summary=? WHERE id=?',(time.time(),json.dumps(summary,ensure_ascii=False),run));store.db.commit()
                summary['report']=str(store.export(run,metadata));summary['history']=str(metadata/'move_history.csv');self.emit({'kind':'done',**summary});return summary
            finally:store.close()

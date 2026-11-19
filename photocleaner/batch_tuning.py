"""One measured batch profile per device, frozen model and task.

Profiles contain timings, never image paths or image data. They are independent
of input directories and decision thresholds, and survive application restarts.
"""
import hashlib
import json
import os
import platform
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT

PROFILE_PATH = PROJECT / '.jobs' / 'batch_profiles.json'
VERSION = 'measured-batch-v1'
FEATURE_LABELS={'photo':'实拍照片','people':'人物与活动','meme':'表情包','creative':'绘画与设计',
                'search':'图文检索','search-style':'风格检索'}
_lock = threading.RLock()
_calibration_lock = threading.RLock()


def identity(torch, device, feature, signature):
    hardware = {'device': device}
    if device == 'cuda':
        index = torch.cuda.current_device()
        p = torch.cuda.get_device_properties(index)
        hardware.update(name=p.name, total_memory=p.total_memory,
                        capability=[p.major, p.minor], uuid=str(getattr(p, 'uuid', index)))
    else:
        hardware.update(cpu=platform.processor(), machine=platform.machine(), cores=os.cpu_count())
    details = {'version': VERSION, 'hardware': hardware, 'torch': str(torch.__version__),
               'cuda': torch.version.cuda, 'feature': feature, 'signature': signature}
    key = hashlib.sha256(json.dumps(details, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return key, details


@contextmanager
def _disk_lock(path):
    """An advisory sidecar is retained; concurrent processes cannot lose a profile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path=path.with_suffix('.lock').absolute()
    if lock_path.resolve()!=lock_path:raise OSError('批量配置锁路径不能包含符号链接或目录联接。')
    with lock_path.open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell():stream.write(b'0');stream.flush()
        if os.name == 'nt':
            import msvcrt
            while True:
                stream.seek(0)
                try:msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1);break
                except OSError:time.sleep(.1)
            try:yield
            finally:stream.seek(0);msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) and isinstance(value.get('profiles'),dict) else {'profiles':{}}
    except (OSError, ValueError):return {'profiles':{}}


def saved(key, path=None):
    path=Path(path or PROFILE_PATH).absolute()
    if path.resolve()!=path:raise OSError('批量配置路径不能包含符号链接或目录联接。')
    with _lock:
        # Another running process may have lowered an OOM safety cap.
        profile=_read(path)['profiles'].get(key)
        if not isinstance(profile,dict) or not isinstance(profile.get('batch'),int) or not 1<=profile['batch']<=4096:return None
        return dict(profile)


def _save(key, profile, path=None):
    path=Path(path or PROFILE_PATH).absolute()
    if path.resolve()!=path:raise OSError('批量配置路径不能包含符号链接或目录联接。')
    with _lock,_disk_lock(path):
        if path.exists():
            try:json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,UnicodeError):
                preserved=path.with_name(path.name+f'.unreadable.{time.time_ns()}.bak')
                preserved.write_bytes(path.read_bytes())
        data=_read(path)
        # Keep different model/device profiles as well as the previous value.
        previous=data['profiles'].get(key)
        if previous is not None and not isinstance(previous,dict):
            data.setdefault('invalid_profiles',[]).append({'key':key,'value':previous});previous=None
        if previous and previous.get('oom_reduced') and profile.get('oom_reduced'):
            profile={**profile,'batch':min(profile['batch'],previous['batch'])}
        if previous and previous!=profile:
            data.setdefault('history',[]).append({'key':key,**previous})
        data['profiles'][key]=profile
        temporary=path.with_name(path.name+f'.{os.getpid()}.{threading.get_ident()}.tmp')
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)


def conservative(key, batch, path=None):
    """Remember successful recovery; never increase an established safety cap."""
    profile=saved(key,path)
    if profile is None:return
    value=max(1,int(batch))
    if value>=profile['batch']:return
    profile.update(batch=value,oom_reduced=True,updated_at=datetime.now(timezone.utc).isoformat())
    _save(key,profile,path)


def tune(torch, device, feature, signature, images, run, candidates, emit=lambda _:None, path=None):
    """Benchmark an uncached inference callback; return (batch, key, profile).

    The callback must apply the supplied micro-batch limit and must not write
    image evidence caches. Images may be repeated to compare full batches, but
    the persisted report records the number of distinct available inputs.
    """
    key,details=identity(torch,device,feature,signature)
    path=Path(path or PROFILE_PATH).absolute()
    if path.resolve()!=path:raise OSError('批量配置路径不能包含符号链接或目录联接。')
    calibration_path=path.with_name(path.stem+'.'+key+'.tune')
    # A separate per-profile sidecar covers the benchmark and second cache
    # lookup. Saving JSON uses its own lock, never reacquiring this same byte.
    with _calibration_lock,_disk_lock(calibration_path):
        profile=saved(key,path)
        if profile:return profile['batch'],key,profile
        if not images:return min(candidates),key,None
        from .numbers import number
        # Individual forwards already use gpu_work. Holding that lock around
        # whole callbacks would invert their model-lock order and can deadlock
        # another page; only initialisations are serialised here.
        label=FEATURE_LABELS.get(feature,feature)
        emit(f'自动批量初始化：正在为{label}测量本机速度；完成后固定复用。')
        sizes=sorted(set(max(1,int(n)) for n in candidates))
        count=max(sizes[-1],min(64 if device=='cuda' else 8,len(images)))
        # Copies prevent task-specific metadata from bypassing inference
        # through path caches, or leaking trial scene annotations.
        available=[images[round(i*(len(images)-1)/max(1,min(len(images),64)-1))] for i in range(min(len(images),64))]
        sample=[]
        for i in range(count):
            source=available[i%len(available)]
            im=source.copy();im.info={k:v for k,v in source.info.items() if k=='search224'};sample.append(im)
        rows=[];limit=None
        def measure(size):
            if device=='cuda':torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats()
            started=time.perf_counter();run(sample,size)
            if device=='cuda':torch.cuda.synchronize()
            elapsed=time.perf_counter()-started
            return {'batch':size,'seconds':elapsed,'images':len(sample),'rate':len(sample)/max(elapsed,1e-9),
                    'peak_allocated_bytes':torch.cuda.max_memory_allocated() if device=='cuda' else None}
        try:
            warm=min(sizes)
            while True:
                try:run(sample[:min(4,len(sample))],warm);break
                except torch.cuda.OutOfMemoryError:
                    if warm==1:raise
                    warm=max(1,warm//2);sizes=[warm];limit=warm
                    if device=='cuda':torch.cuda.empty_cache()
            pending=list(sizes)
            while pending:
                size=pending.pop(0)
                try:rows.append(measure(size))
                except torch.cuda.OutOfMemoryError:
                    if device=='cuda':torch.cuda.empty_cache()
                    if size==1:raise
                    limit=max(1,size//2)
                    if not rows:pending=[limit]
                    else:break
            if not rows:raise RuntimeError('当前可用显存不足以完成自动批量初始化，请关闭其他模型任务后重试。')
            # Repeat the two leading candidates to reduce first-run noise.
            for row in sorted(rows,key=lambda r:r['rate'],reverse=True)[:2]:
                try:
                    repeat=measure(row['batch']);row['repeat_seconds']=repeat['seconds']
                    row['rate']=2*len(sample)/(row['seconds']+repeat['seconds'])
                except torch.cuda.OutOfMemoryError:
                    limit=max(1,row['batch']//2)
                    if device=='cuda':torch.cuda.empty_cache()
            eligible=[r for r in rows if limit is None or r['batch']<=limit]
            while not eligible:
                try:eligible=[measure(limit)];rows.extend(eligible)
                except torch.cuda.OutOfMemoryError:
                    if limit==1:raise
                    limit=max(1,limit//2)
                    if device=='cuda':torch.cuda.empty_cache()
            fastest=max(r['rate'] for r in eligible)
            selected=min(r['batch'] for r in eligible if r['rate']>=fastest*.97)
            profile={**details,'batch':selected,'measurements':rows,'sample_unique':len(available),
                     'created_at':datetime.now(timezone.utc).isoformat(),'oom_reduced':limit is not None}
            _save(key,profile,path)
            emit(f'{label}自动批量已固定为 {number(selected)}；初始化样本最快 {number(fastest)} 张/秒。')
            return selected,key,profile
        finally:
            for im in sample:im.close()


def semantic_signature(scheme):
    from .config import MODEL_ID as CLIP, MODEL_REVISION as CLIP_REV
    from .people_model import MODEL_ID, MODEL_REVISION, RULES, VERIFY_RULES, ART_RULES
    from .search_model import FEATURE_KEY
    if scheme=='photo':
        from .photo import RULES as rules
    elif scheme=='meme':
        from .meme import RULES as rules
    elif scheme=='creative':
        from .content import RULES as shared
        from .creative import RULES as taxonomy
        rules=shared+taxonomy
    else:
        from .people_scene import SCENES
        from .media import PROMPTS
        from .people_rescue import RULES as REVIEW
        from .person_detector import MODEL_ID as detector, MODEL_REVISION as detector_rev
        rules=RULES+VERIFY_RULES+ART_RULES+REVIEW+str(SCENES)+str(PROMPTS)+detector+detector_rev
    return {'models':[MODEL_ID+'@'+MODEL_REVISION,CLIP+'@'+CLIP_REV,FEATURE_KEY],
            'rules':hashlib.sha256(rules.encode()).hexdigest(),
            'qwen':{'dtype':'bfloat16/cpu-float32','attention':'sdpa','tokens':[192,256,320],
                    'preparation':'lookahead-rope-cpu-v1','micro_limit':32},
            'task':'uncached-default-route-v1','clip_input':224,'siglip_input':224}

"""Initial device sizing, live memory information and progress estimates."""
from .numbers import number
import time

def device_memory(torch,device='cuda'):
    """A fresh allocator/device snapshot for each estimate event, no subprocess."""
    if device!='cuda' or not torch.cuda.is_available():return 'CPU'
    free,total=torch.cuda.mem_get_info()
    return f'{torch.cuda.get_device_name(torch.cuda.current_device())} · 显存已用 {number((total-free)/1024**3)} / {number(total/1024**3)} GB · 可用 {number(free/1024**3)} GB'

def batch_for_device(config,torch,semantic=False):
    cuda=config.device!='cpu' and torch.cuda.is_available()
    if not cuda:
        return (1 if semantic else min(config.batch_size,16)), 'CPU'
    free,total=torch.cuda.mem_get_info()
    gib=1024**3
    if not config.auto_batch:
        batch=min(config.batch_size,32 if semantic else 4096)
    elif semantic:
        # Longer prompts and dynamic aspect ratios make maximum occupancy a
        # poor throughput target. Reserve headroom for the subject review.
        budget=max(0,free/gib-1.5)
        # Repeated 4080 SUPER photo measurements: 8 beat 16/32 despite a
        # lower utilisation reading. Variable aspect ratios pad larger batches.
        ceiling=(8 if total/gib<20 else 16 if total/gib<40 else 24) if config.scheme=='photo' else (16 if total/gib<20 else 24 if total/gib<40 else 32)
        batch=max(1,min(ceiling,int(budget/.45)))
    else:
        batch=max(1,min(512,int(max(0,free/gib-1)/.012)))
        batch=max(1,2**(batch.bit_length()-1))
    return batch,device_memory(torch,'cuda')

def duration(seconds):
    n=max(0,round(seconds))
    if n<60:return f'{number(n)} 秒'
    if n<3600:return f'{number(n//60)} 分 {number(n%60)} 秒'
    return f'{number(n//3600)} 小时 {number(n%3600//60)} 分'

class Estimate:
    """First batch, then 5/25/50/75/95/100%; independent per phase."""
    def __init__(self,total,clock=time.monotonic):
        self.total=total;self.clock=clock;self.started=clock();self.next_fraction=0;self.sample=None
        self.checkpoints=(.05,.25,.5,.75,.95,1.)
    def update(self,processed):
        if not processed or not self.total:return None
        fraction=min(1,processed/self.total)
        if self.sample is None or fraction>=self.next_fraction:
            elapsed=self.clock()-self.started
            if elapsed<=0:return None
            self.sample=(processed,elapsed,processed/elapsed)
            self.next_fraction=next((p for p in self.checkpoints if p>fraction),float('inf'))
            return self.snapshot(processed)
        return None
    def snapshot(self,processed):
        if not self.sample:return None
        _,_,rate=self.sample
        return {'remaining_seconds':max(0,(self.total-processed)/rate),'total_seconds':self.total/rate,
                'rate':rate,'sampled_at':self.sample[0],'updated_at':self.clock()}

class ProgressEvents:
    """Attach per-phase estimates to utility task progress."""
    def __init__(self,emit):self.emit=emit;self.phase=None;self.estimate=None;self.base=0
    def __call__(self,event):
        self.emit(event)
        if event['kind']!='progress':return
        phase=event.get('phase','处理');done=event['processed'];total=event['total']
        if phase!=self.phase or done<self.base:
            self.phase=phase;self.base=done;self.estimate=Estimate(total-done);return
        sample=self.estimate.update(done-self.base)
        if sample:
            sample['total_seconds']=total/sample['rate']
            self.emit({'kind':'estimate','phase':phase,**sample})

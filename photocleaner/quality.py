"""Bounded diagnostics: reblur sensitivity, distributed detail and grid caveats."""
import numpy as np
import math,json
from functools import lru_cache
from PIL import Image,ImageFilter

QUALITY_VERSION='pbdb-focus-v1'

def pbdb(a,block=4):
    """Duan et al., ICIP 2021, equations 1–3, without a fitted mapping.

    https://sigport.org/sites/default/files/docs/An_Effective_Sharpness_Assessment_Method_For_Shallow_Depth-Of-Field_Images.pdf
    Work on 0..255 luminance. The final incomplete block is cropped.
    """
    q=np.abs((a[:-1,:-1]-a[1:,:-1])*(a[:-1,:-1]-a[:-1,1:]))
    h,w=(v//block*block for v in q.shape)
    if not h or not w:return 0.
    sums=q[:h,:w].reshape(h//block,block,w//block,block).sum(axis=(1,3),dtype=np.float64)
    return float(np.mean(sums*sums))

def weighted_median(values,weights):
    if not values or sum(weights)<=0:return None
    order=np.argsort(values);v=np.asarray(values)[order];w=np.asarray(weights,dtype=np.float64)[order]
    return float(v[np.searchsorted(np.cumsum(w),w.sum()/2)])

def box_axis(a,axis,width=9):
    pad=[(0,0)]*2;pad[axis]=(width//2,width//2)
    p=np.pad(a,pad,mode='edge');c=np.cumsum(p,axis=axis,dtype=np.float64)
    zero=np.zeros(tuple(1 if i==axis else s for i,s in enumerate(c.shape)))
    c=np.concatenate((zero,c),axis=axis)
    high=[slice(None)]*2;low=high.copy();high[axis]=slice(width,None);low[axis]=slice(None,-width)
    return ((c[tuple(high)]-c[tuple(low)])/width).astype(np.float32)

def reblur(a):
    """Crete-style reblur sensitivity, [0,1], higher means softer edges.
    Use the better-supported direction so a single dominant edge direction
    doesn't turn a blank axis into a false blur decision.
    """
    values=[]
    for axis in (0,1):
        d=np.abs(np.diff(a,axis=axis));b=np.abs(np.diff(box_axis(a,axis),axis=axis))
        if d.size and float(d.mean())>.25:
            values.append(float(np.clip(1-np.maximum(d-b,0).sum()/max(1.,float(d.sum())),0,1)))
    return min(values) if values else None

def extra_metrics(image):
    gray=image.convert('L');gray.thumbnail((768,768),Image.Resampling.LANCZOS)
    a=np.asarray(gray,np.float32);lap=-4*a[1:-1,1:-1]+a[:-2,1:-1]+a[2:,1:-1]+a[1:-1,:-2]+a[1:-1,2:]
    active=[];soft=[];focus_values=[];focus_weights=[];focus_sharp=[]
    for y in range(0,max(1,a.shape[0]-63),96):
        for x in range(0,max(1,a.shape[1]-63),96):
            patch=a[y:y+96,x:x+96]
            if min(patch.shape)<16:continue
            if np.percentile(patch,95)-np.percentile(patch,5)<10:continue
            p=lap[y:min(y+94,lap.shape[0]),x:min(x+94,lap.shape[1])]
            if not p.size:continue
            active.append(float(np.var(p)))
            value=reblur(patch)
            if value is not None:
                soft.append(value);focus_values.append(value);focus_weights.append(pbdb(patch)*patch.size);focus_sharp.append(float(np.var(p)))
    smooth=np.asarray(gray.filter(ImageFilter.MedianFilter(3)),np.float32)
    noise=float(np.median(np.abs(a-smooth))/.6745)
    # This is a diagnostic for line-art/QR/pixel-grid confounding, not a
    # semantic detector and never a reason to call something a meme.
    dx=np.abs(np.diff(a,axis=1));dy=np.abs(np.diff(a,axis=0))
    edge=np.concatenate((dx.ravel(),dy.ravel()))
    geometric=float(np.mean((edge<.2)|(edge>80)))
    result={'reblur':round(float(np.percentile(soft,50)),4) if soft else None,
            'sharpness_mid':round(float(np.median(active)),3) if active else 0.,
            'detail_coverage':round(sum(v>=25 for v in active)/max(1,len(active)),4),
            'noise_level':round(noise,3),'grid_caution':geometric>.85,'integrity':'完整解码'}
    # Our pooling adaptation: locally reblurred edge widths are weighted by
    # published PBDB energy. Smooth defocus background no longer gets the same
    # vote as the in-focus subject. No coefficients are trained on user images.
    result.update(pbdb=pbdb(a),focus_reblur=weighted_median(focus_values,focus_weights),focus_sharpness=weighted_median(focus_sharp,focus_weights),quality_version=QUALITY_VERSION)
    gray.close();return result

@lru_cache(maxsize=4)
def _calibration(path,mtime):
    return json.loads(__import__('pathlib').Path(path).read_text(encoding='utf8'))

def detail_score(metrics):
    from .defaults import DIRECTORY
    path=DIRECTORY/'quality_model.json'
    if not path.is_file():
        from .paths import resource_file
        path=resource_file('defaults','quality_model.json')
    model=_calibration(str(path),path.stat().st_mtime_ns)
    s=metrics.get('sharpness');m=metrics.get('sharpness_mid');r=metrics.get('reblur')
    if s is None or m is None or r is None:return None
    x=(1,math.log1p(max(0,s))/math.log(81),math.log1p(max(0,m))/math.log(21),(1-r)/.5)
    z=sum(a*b for a,b in zip(model['coefficients'],x))
    return round(100/(1+math.exp(-max(-30,min(30,z)))),3)

def quality_reason(metrics,config):
    from .thresholds import lower,upper
    # Compatibility for explicitly requested historical thresholds only.
    # New runs do not compute the small-sample fitted score by default.
    if config.detail_threshold>0:metrics['detail_score']=detail_score(metrics)
    if config.focus_sharpness_threshold>0:
        value=metrics.get('focus_sharpness')
        if value is None or not lower(value,config.focus_sharpness_threshold,config):return 'low_focus_sharpness'
    if config.focus_reblur_threshold<1:
        value=metrics.get('focus_reblur')
        if value is None or not upper(value,config.focus_reblur_threshold,config):return 'soft_focus_detail'
    if config.pbdb_threshold>0 and not lower(metrics.get('pbdb',0),config.pbdb_threshold,config):return 'low_pbdb'
    if config.detail_threshold>0 and (metrics['detail_score'] is None or not lower(metrics['detail_score'],config.detail_threshold,config)):return 'low_detail_score'
    if config.sharpness_mid_threshold>0 and not lower(metrics.get('sharpness_mid',0),config.sharpness_mid_threshold,config):return 'low_mid_detail'
    if config.sharpness_threshold>0 and not lower(metrics.get('sharpness',0),config.sharpness_threshold,config):return 'blurred'
    if not upper(metrics.get('blockiness',0),config.blockiness_threshold,config) and not metrics.get('grid_caution',False):return 'blocky'
    value=metrics.get('reblur')
    if config.reblur_threshold<1 and (value is None or not upper(value,config.reblur_threshold,config)):return 'soft_detail'
    return ''

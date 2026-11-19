"""Shared inexpensive media gate; ambiguous images retain specialist checks."""
import json,sqlite3,threading,hashlib
from contextlib import contextmanager
from pathlib import Path
import numpy as np
from .config import PROJECT
from .search_model import FEATURE_KEY

PROMPTS={
 'photo':['a camera photograph of a real scene','a photograph of scenery, buildings, trains or a room',
          'a photograph of food or physical objects','a photograph of a real person or animegao kigurumi cosplayer',
          'a camera photograph of tangible merchandise or toy dolls on a real table or in a room'],
 'art':['a digital anime illustration or manga drawing','a painting, sketch or animation frame',
        'a synthetic 3D render or video game character'],
 'poster':['a flat graphic design poster or advertisement','a flat scanned page, diagram, map or text document',
           'a product listing page with text and product pictures','a flat collage of catalog product pictures isolated on a white background'],
 'screenshot':['a screenshot of software, an app, a chat or a website','a screenshot of a video game interface',
               'a screenshot of a film or television scene with subtitles'],
 'meme':['a recycled reaction meme with a caption','a chat sticker, emoticon or funny low resolution reaction picture'],
}
VERSION='media-v3:'+FEATURE_KEY+':'+hashlib.sha256(json.dumps(PROMPTS,sort_keys=True).encode()).hexdigest()
_cache_lock=threading.RLock()

class EvidenceCache:
    def __init__(self,config=None,emit=None):
        self.lock=_cache_lock
        self.path=(PROJECT/'.jobs'/'media.sqlite3').absolute()
        if self.path.resolve()!=self.path:raise OSError('媒介缓存数据库路径不能包含符号链接或目录联接。')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS media(path TEXT PRIMARY KEY,size INTEGER,mtime INTEGER,version TEXT,scores TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS specialist(path TEXT,scheme TEXT,size INTEGER,mtime INTEGER,version TEXT,scores TEXT,PRIMARY KEY(path,scheme))')
    @contextmanager
    def connect(self):
        if self.path.resolve()!=self.path:raise OSError('媒介缓存数据库路径不能包含符号链接或目录联接。')
        db=sqlite3.connect(self.path,timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:yield db
        finally:db.close()
    def specialist_version(self,scheme):
        import hashlib
        from .people_model import MODEL_ID,MODEL_REVISION
        from .config import MODEL_ID as CLIP_ID, MODEL_REVISION as CLIP_REVISION
        if scheme=='content':
            from .content import RULES
        elif scheme=='meme':
            from .meme import RULES
        elif scheme=='photo':
            from .photo import RULES
        else:
            from .creative import RULES
        models=MODEL_ID+MODEL_REVISION
        if scheme in ('content','meme'):models+=CLIP_ID+CLIP_REVISION
        return hashlib.sha256((models+RULES).encode()).hexdigest()
    def cached_specialist(self,image,scheme,value=None):
        key=image.info.get('source_key')
        if not key or scheme=='people':return None
        version=self.specialist_version(scheme)
        with self.lock,self.connect() as db:
            if value is None:
                row=db.execute('SELECT scores FROM specialist WHERE path=? AND scheme=? AND size=? AND mtime=? AND version=?',(key[0],scheme,*key[1:],version)).fetchone()
                if not row:return None
                try:
                    result=json.loads(row[0])
                    required={'photo':('photo','photo_checks'),'meme':('meme_score','ordinary'),
                              'content':('camera','artwork','software','meme'),'creative':('comic','illustration','design')}[scheme]
                    return result if isinstance(result,dict) and all(k in result for k in required) else None
                except (ValueError,TypeError,KeyError):return None
            db.execute('INSERT OR REPLACE INTO specialist VALUES(?,?,?,?,?,?)',(key[0],scheme,*key[1:],version,json.dumps(value)))

class MediaGate(EvidenceCache):
    def __init__(self,config,emit):
        super().__init__(config,emit)
        self.auto_batch=config.auto_batch
        from .vision import get_search_encoder
        self.encoder=get_search_encoder(config,emit)
        self.text=self.encoder.text([p for values in PROMPTS.values() for p in values])
    def classify(self,images):
        result=[None]*len(images);pending=[]
        with self.lock,self.connect() as db:
            for i,im in enumerate(images):
                key=im.info.get('source_key')
                row=db.execute('SELECT scores FROM media WHERE path=? AND size=? AND mtime=? AND version=?',(*key,VERSION)).fetchone() if key else None
                if row:
                    try:
                        import math
                        scores=json.loads(row[0])
                        if isinstance(scores,dict) and all(isinstance(scores.get(k),(int,float)) and math.isfinite(scores[k]) and 0<=scores[k]<=1 for k in PROMPTS):result[i]=scores
                    except (ValueError,TypeError):pass
                if result[i] is None:pending.append(i)
        if pending:
            # No SQLite write transaction or cache mutex during GPU inference.
            vectors=self.encoder.images([images[i] for i in pending],auto_batch=self.auto_batch);similarities=vectors@self.text.T
            groups=[];offset=0
            for prompts in PROMPTS.values():
                groups.append(similarities[:,offset:offset+len(prompts)].max(1));offset+=len(prompts)
            logits=np.stack(groups,1)*80;logits-=logits.max(1,keepdims=True)
            values=np.exp(logits);values/=values.sum(1,keepdims=True);writes=[]
            for i,row in zip(pending,values):
                scores=dict(zip(PROMPTS,map(float,row)));result[i]=scores;key=images[i].info.get('source_key')
                if key:writes.append((*key,VERSION,json.dumps(scores)))
            if writes:
                with self.lock,self.connect() as db:db.executemany('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)',writes)
        return result

class RoutedClassifier:
    def __init__(self,config,emit,batch,description):
        self.config=config;self.emit=emit;self.actual_batch=batch;self.device_description=description
        self.gate=MediaGate(config,emit) if config.scheme=='people' else EvidenceCache()
        import torch
        self.device='cuda' if config.device!='cpu' and torch.cuda.is_available() else 'cpu'
        self.core=None;self.specialist=None;self.content=None
        self.scene=None
        self._batch_ready=not config.auto_batch;self._batch_profile_key=None
        if config.auto_batch:
            from .batch_tuning import identity,semantic_signature,saved,FEATURE_LABELS
            self._batch_signature=semantic_signature(config.scheme)
            self._batch_profile_key,_=identity(torch,self.device,config.scheme,self._batch_signature)
            profile=saved(self._batch_profile_key)
            if profile:
                self.actual_batch=profile['batch'];self._batch_ready=True
                emit(f'复用本机{FEATURE_LABELS[config.scheme]}自动批量：{self.actual_batch}。')
    def _specialist(self):
        if self.specialist is None:
            from .vision import get_semantic_core
            self.core=get_semantic_core(self.config,self.emit)
            from .runtime import batch_for_device
            fitted,_=batch_for_device(self.config,self.core.torch,True)
            if not self.config.auto_batch:self.actual_batch=min(self.actual_batch,fitted)
            if self.config.scheme=='photo':
                from .photo import PhotoSemantic
                self.specialist=PhotoSemantic(self.core,self.actual_batch)
            elif self.config.scheme=='meme':
                from .meme import MemeClassifier
                self.specialist=MemeClassifier(self.core,self.actual_batch)
            elif self.config.scheme=='creative':
                from .creative import CreativeClassifier
                self.specialist=CreativeClassifier(self.core,self.actual_batch)
            else:self.specialist=self.core
        if self.core is not None:self.core.batch_context.key=self._batch_profile_key if self._batch_ready else None
        return self.specialist
    def classify_people(self,images):return self.classify(images)
    def classify(self,images):
        if images and not self._batch_ready:
            # Calibrate the shared coarse encoder on the real incoming batch,
            # before a semantic trial's four-image warm-up can initialise it.
            if self.config.scheme=='people':
                self.gate.encoder.prepare_images(images)
                self.gate.encoder._ensure_batch(images,False)
            self._specialist()
            from .batch_tuning import tune
            from dataclasses import replace
            original=self.config
            def measured(sample,size):
                self._apply_batch(size)
                # Threshold choices must not create different machine profiles.
                self.config=replace(original,people_threshold=.97,people_verify_threshold=.95,people_rescue=True)
                try:
                    value=self._classify_plain(sample)
                    if self.device=='cuda' and self.core.micro_batch<size:raise self.core.torch.cuda.OutOfMemoryError('校准中已安全降低内部批量')
                    return value
                finally:self.config=original
            selected,key,_=tune(self.core.torch,self.device,original.scheme,self._batch_signature,images,measured,
                               (4,8,16,32) if self.device=='cuda' else (1,),self.emit)
            self._batch_profile_key=key;self._apply_batch(selected);self._batch_ready=True
        if self.core is not None:
            self.core.batch_context.key=self._batch_profile_key
        try:return self._classify_plain(images)
        finally:
            if self.core is not None:self.core.batch_context.key=None
    def _apply_batch(self,batch):
        self.actual_batch=batch
        for obj in (self.specialist,self.content,getattr(self.content,'memes',None)):
            if obj is not None and hasattr(obj,'batch'):obj.batch=batch
    def _classify_plain(self,images):
        if self.config.scheme=='photo':return self._classify_photo(images)
        if self.config.scheme!='people':return self._classify_content(images)
        coarse=self.gate.classify(images);result=[None]*len(images);review=[];scheme=self.config.scheme
        for i,s in enumerate(coarse):
            if s['art']>=.995:
                result[i]={'other':1.0,'people':0.0,'decision_route':'明确绘画，跳过人物细分'}
            else:review.append(i)
        if review:
            if scheme=='people':
                from .people_scene import SceneGuard
                if self.scene is None:self.scene=SceneGuard(self.config,self.emit)
                checks=self.scene.classify([images[i] for i in review]);remaining=[]
                for i,s in zip(review,checks):
                    images[i].info['scene_guard']=s
                    if s['veto'] and s['category'] in ('ritual','scenery','environment'):
                        result[i]={'other':1.0,'people':0.,'scene_score':s['score'],'scene_category':s['category'],'decision_route':'明确排除场景，跳过人物精细判断'}
                    else:remaining.append(i)
                review=remaining
        if review:
            model=self._specialist();ims=[images[i] for i in review]
            values=model.classify_people(ims,batch_limit=self.actual_batch,rescue=self.config.people_rescue,
                primary_threshold=self.config.people_threshold,verify_threshold=self.config.people_verify_threshold,inclusive=self.config.inclusive) if scheme=='people' else model.classify(ims)
            for i,s in zip(review,values):
                result[i]={**s,'decision_route':'大类不确定，专门复核','coarse_scores':coarse[i]}
                if hasattr(self.gate,'cached_specialist'):self.gate.cached_specialist(images[i],scheme,s)
        self.last_counts={'total':len(images),'specialist':len(review)}
        return result
    def _classify_photo(self,images):
        results=[self.gate.cached_specialist(im,'photo') for im in images]
        missing=[i for i,s in enumerate(results) if s is None]
        if missing:
            for i,s in zip(missing,self._specialist().classify([images[i] for i in missing])):
                results[i]=s;self.gate.cached_specialist(images[i],'photo',s)
        self.last_counts={'total':len(images),'specialist':len(missing)}
        return results
    def _classify_content(self,images):
        scheme=self.config.scheme;cache_key='meme' if scheme=='meme' else 'content';raw=[None]*len(images);missing=[]
        for i,im in enumerate(images):
            raw[i]=self.gate.cached_specialist(im,cache_key)
            if raw[i] is None:missing.append(i)
        if missing:
            specialist=self._specialist()
            if scheme=='meme':model=specialist
            else:
                from .content import ContentClassifier
                if self.content is None:self.content=specialist if scheme=='photo' else ContentClassifier(self.core,self.actual_batch,self.gate)
                model=self.content
            for i,s in zip(missing,model.classify([images[j] for j in missing])):
                raw[i]=s;self.gate.cached_specialist(images[i],cache_key,s)
                if cache_key=='content' and s.get('meme_evidence'):self.gate.cached_specialist(images[i],'meme',s['meme_evidence'])
        route='共享媒介核对' if missing else '复用媒介核对'
        self.last_counts={'total':len(images),'specialist':len(missing)}
        if scheme=='meme':return [{**s,'decision_route':'表情包专门语义判断'} for s in raw]
        if scheme=='photo':
            from .content import photo_scores
            return [{**photo_scores(s),'decision_route':route} for s in raw]
        # A separate medium decision must support creative taxonomy. A high
        # taxonomy score alone cannot override photography/collage evidence.
        result=[];taxonomy=[]
        for i,s in enumerate(raw):
            from .content import LABELS as CONTENT_LABELS
            best=max(CONTENT_LABELS,key=s.get)
            if s.get('meme',0)>=.80:best='meme'
            elif s.get('screen_check',0)>=.90:best='software'
            elif s.get('photo_rescued',False) or (best=='artwork' and s.get('physical_check',0)>=.60 and s.get('camera_check',0)>=.90):best='camera'
            if best=='artwork' and s['artwork']>=.55:
                result.append(None);taxonomy.append(i);continue
            label={'camera':'photo','photo_collage':'photo_collage','meme':'meme','software':'software'}.get(best,'other')
            result.append({label:max(s.get('screen_check',0),s[best]) if best=='software' else s[best],'media_scores':s,'media_rejected':best!='software','decision_route':'媒介核对排除/界面判定'})
        if taxonomy:
            pending=[]
            for i in taxonomy:
                cached=self.gate.cached_specialist(images[i],'creative')
                if cached is None:pending.append(i)
                else:result[i]={**cached,'media_scores':raw[i],'decision_route':'复用创作细分'}
            if pending:
                for i,s in zip(pending,self._specialist().classify([images[j] for j in pending])):
                    self.gate.cached_specialist(images[i],'creative',s)
                    result[i]={**s,'media_scores':raw[i],'decision_route':'媒介支持后创作细分'}
        return result
    def close(self):
        if self.content is not None:self.content.close()
        if self.specialist is not None and self.specialist is not self.core and self.specialist is not self.content:self.specialist.close()
        self.specialist=None;self.core=None;self.gate=None;self.scene=None;self.content=None

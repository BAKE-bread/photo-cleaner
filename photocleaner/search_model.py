"""Local multilingual retrieval encoders; no training or remote image requests."""
import hashlib,json,io,warnings
import threading
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from PIL import Image,ImageOps
from .config import PROJECT
from .vision import oriented_image,rgb_image

MODEL_ID='google/siglip2-base-patch16-224'
MODEL_REVISION='75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2'
DINO_ID='facebook/dinov2-small'
DINO_REVISION='ed25f3a31f01632728cabb09d1542f84ab7b0056'
FEATURE_KEY=MODEL_ID+'@'+MODEL_REVISION+':whole-v1'

@dataclass(frozen=True)
class SearchConfig:
    mode:str='text'
    prompts:tuple=()
    combine:str='any'
    reference:str=''
    limit:int=200
    refine:bool=False
    retain_tree:bool=False
    device:str='auto'
    offline:bool=False
    workers:int=8
    model_dir:str=str(PROJECT/'models')
    def __post_init__(self):
        from .directory_lists import clean_path
        object.__setattr__(self,'reference',clean_path(self.reference))
    def validate(self):
        if self.mode not in ('text','similar','style'):raise ValueError('搜索方式无效。')
        if self.mode=='text' and not any(str(p).strip() for p in self.prompts):raise ValueError('请至少输入一条搜索描述。')
        if len(self.prompts)>32:raise ValueError('一次最多输入 32 条描述，每行一条。')
        if self.mode!='text' and not Path(self.reference).is_file():raise ValueError('请选择参考图像。')
        if self.combine not in ('any','all'):raise ValueError('多描述组合方式无效。')
        if not 1<=self.limit<=5000:raise ValueError('候选数量范围为 1–5000。')
        if self.device not in ('auto','cpu','cuda') or not 1<=self.workers<=16:raise ValueError('设备或 CPU 解码线程设置无效。')

def unit(values):
    values=np.asarray(values,dtype=np.float32)
    return values/np.maximum(np.linalg.norm(values,axis=-1,keepdims=True),1e-8)

def load_search_image(path):
    """No quality/400px gate. JPEG scaled decode; actual MPO primary accepted."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',Image.DecompressionBombWarning)
        with Image.open(path) as original:
            if original.format!='MPO' and (getattr(original,'n_frames',1)>1 or getattr(original,'is_animated',False)):
                raise ValueError('动图/多帧图像跳过')
            width,height=original.size
            if max(width,height)/max(1,min(width,height))>200:raise ValueError('图像长宽比超过安全范围')
            if original.format in ('JPEG','MPO'):original.draft('RGB',(384,384))
            if original.width*original.height>80_000_000:raise ValueError('超过完整解码像素上限')
            original.load();metrics={};oriented=oriented_image(original,metrics)
            try:im=rgb_image(oriented)
            finally:oriented.close()
    if metrics.get('_axes_swapped'):width,height=height,width
    im.thumbnail((512,512),Image.Resampling.LANCZOS,reducing_gap=3)
    return im,width,height

def appearance(im):
    small=im.resize((32,32)).convert('L');a=np.asarray(small,dtype=np.float32);small.close()
    # 64-bit perceptual hash for duplicate-like ranking (not equality proof).
    c=np.cos(np.pi*np.arange(8)[:,None]*(2*np.arange(32)[None,:]+1)/64)
    low=c@a@c.T;bits=(low>np.median(low.ravel()[1:])).ravel();bits[0]=False
    phash=f'{sum(int(v)<<i for i,v in enumerate(bits)):016x}'
    hsv=im.resize((64,64)).convert('HSV');v=np.asarray(hsv);hsv.close()
    hist=np.concatenate([np.histogram(v[:,:,i],bins=16,range=(0,256))[0] for i in range(3)])
    return phash,unit(hist)

def thumbnail(im):
    small=im.copy();small.thumbnail((256,200),Image.Resampling.LANCZOS)
    buf=io.BytesIO();small.save(buf,format='JPEG',quality=85);small.close();return buf.getvalue()

class SearchEncoder:
    def __init__(self,config,emit=lambda _:None):
        import torch
        from transformers import AutoTokenizer,SiglipModel,SiglipImageProcessor
        self.torch=torch;self.config=config;self.emit=emit;self.dino=None
        self.inference_lock=threading.RLock()
        if config.device=='cuda' and not torch.cuda.is_available():raise RuntimeError('CUDA 不可用，请修复环境或选择 CPU。')
        self.device='cuda' if config.device!='cpu' and torch.cuda.is_available() else 'cpu'
        torch.set_num_threads(min(4 if self.device=='cuda' else 8,__import__('os').cpu_count() or 1))
        from .model_files import model_snapshot
        snapshot=model_snapshot(MODEL_ID,MODEL_REVISION,config,emit,required=('tokenizer.json','tokenizer_config.json','preprocessor_config.json'))
        args=dict(local_files_only=True)
        self.tokenizer=AutoTokenizer.from_pretrained(snapshot,**args)
        self.processor=SiglipImageProcessor.from_pretrained(snapshot,**args)
        self.dtype=torch.float16 if self.device=='cuda' else torch.float32
        self.model=SiglipModel.from_pretrained(snapshot,dtype=self.dtype,use_safetensors=True,**args).to(self.device).eval()
        self.batch=32 if self.device=='cuda' else 4
        if self.device=='cuda':
            free,_=torch.cuda.mem_get_info();self.batch=max(1,min(128,int(max(0,free/1024**3-1.5)/.030)))
        self.style_batch=min(self.batch,64);self._batch_keys={};self._batch_ready=set();self._tuning=False
        from .batch_tuning import identity,saved
        for style in (False,True):
            key,_=identity(torch,self.device,'search-style' if style else 'search',self._batch_signature(style))
            self._batch_keys[style]=key
            profile=saved(key)
            if profile:
                if style:self.style_batch=profile['batch']
                else:self.batch=profile['batch']
                self._batch_ready.add(style)
        emit(f'搜索模型就绪：{torch.cuda.get_device_name(0) if self.device=="cuda" else "CPU"}；推理批量上限 {self.batch}。')

    def text(self,prompts):
        prompts=[str(p).strip() for p in prompts]
        for i,prompt in enumerate(prompts,1):
            if len(self.tokenizer.encode(prompt))>64:raise ValueError(f'第 {i} 条描述超过模型长度限制，请拆成多行并选择综合全部描述。')
        inputs=self.tokenizer(prompts,padding='max_length',max_length=64,return_tensors='pt').to(self.device)
        from .vision import gpu_work
        with self.inference_lock,gpu_work(self.device),self.torch.inference_mode():return unit(self.model.get_text_features(**inputs).float().cpu().numpy())

    def images(self,images,auto_batch=True):
        self.prepare_images(images)
        if auto_batch:self._ensure_batch(images,False)
        with self.inference_lock:return self._batches(images,False)

    def prepare_images(self,images):
        # Identical to search decode workers; prepare once outside the batch
        # comparison, so shared retrieval uses the same model-input path.
        for im in images:
            if 'search224' not in im.info:im.info['search224']=np.asarray(im.resize((224,224),self.processor.resample))

    def style(self,images):
        with self.inference_lock:self._load_style()
        self._ensure_batch(images,True)
        with self.inference_lock:return self._batches(images,True)
    def _load_style(self):
        if self.dino is None:
            from transformers import Dinov2Model,BitImageProcessor
            from .model_files import model_snapshot
            snapshot=model_snapshot(DINO_ID,DINO_REVISION,self.config,self.emit,required=('preprocessor_config.json',))
            self.dino_processor=BitImageProcessor.from_pretrained(snapshot,local_files_only=True)
            self.dino=Dinov2Model.from_pretrained(snapshot,dtype=self.dtype,use_safetensors=True,local_files_only=True).to(self.device).eval()

    def _batch_signature(self,style):
        return {'models':([DINO_ID+'@'+DINO_REVISION] if style else [MODEL_ID+'@'+MODEL_REVISION]),
                'input':224,'dtype':str(self.dtype),'algorithm':'dino-pad-cls.4-mean.6-v1' if style else FEATURE_KEY+':cached224-v2',
                'processor':self.dino_processor.to_dict() if style and hasattr(self,'dino_processor') else
                  {'resize':224,'padding':'neutral-gray'} if style else self.processor.to_dict()}

    def _ensure_batch(self,images,style):
        if not images or style in self._batch_ready:return
        from .batch_tuning import tune
        # The DINO signature is independent of whether its lazy weights have
        # already loaded; its pinned revision supplies the processor config.
        signature=self._batch_signature(style)
        if style:signature['processor']={'resize':224,'padding':'neutral-gray'}
        def run(sample,size):
            with self.inference_lock:
                if style:self.style_batch=size
                else:self.batch=size
                self._tuning=True
                try:
                    value=self._batches(sample,style)
                    actual=self.style_batch if style else self.batch
                    if actual<size:raise self.torch.cuda.OutOfMemoryError('校准中已安全降低内部批量')
                    return value
                finally:self._tuning=False
        selected,key,_=tune(self.torch,self.device,'search-style' if style else 'search',signature,images,run,
                            (16,32,64,128) if self.device=='cuda' else (1,2,4),self.emit)
        with self.inference_lock:
            if style:self.style_batch=selected
            else:self.batch=selected
            self._batch_keys[style]=key;self._batch_ready.add(style)

    def _batches(self,images,style):
        result=[];offset=0
        while offset<len(images):
            size=self.style_batch if style else self.batch
            ims=images[offset:offset+size];padded=[]
            if style:padded=[ImageOps.pad(im,(224,224),color=(127,127,127)) for im in ims]
            inputs=None;hidden=None;values=None
            try:
                processor=self.dino_processor if style else self.processor
                if not style:
                    pixels=np.stack([im.info.get('search224') if 'search224' in im.info else np.asarray(im.resize((224,224),self.processor.resample)) for im in ims]).astype(np.float32)
                    pixels=(pixels/255-np.asarray(self.processor.image_mean,np.float32))/np.asarray(self.processor.image_std,np.float32)
                    inputs={'pixel_values':self.torch.from_numpy(pixels.transpose(0,3,1,2).copy()).to(self.device,dtype=self.dtype)}
                else:inputs=processor(images=padded,return_tensors='pt').to(self.device,dtype=self.dtype)
                from .vision import gpu_work
                with gpu_work(self.device),self.torch.inference_mode():
                    if style:
                        hidden=self.dino(**inputs).last_hidden_state
                        values=self.torch.cat((self.torch.nn.functional.normalize(hidden[:,0],dim=-1)*.4,
                            self.torch.nn.functional.normalize(hidden[:,1:].mean(1),dim=-1)*.6),-1)
                        del hidden
                    else:values=self.model.get_image_features(**inputs)
                    result.append(unit(values.float().cpu().numpy()));del values
                offset+=len(ims)
            except self.torch.cuda.OutOfMemoryError:
                if size==1:raise
                inputs=None;hidden=None;values=None
                reduced=max(1,size//2)
                if style:self.style_batch=reduced
                else:self.batch=reduced
                self.torch.cuda.empty_cache();self.emit(f'检索显存不足，批量减为 {reduced} 后重试。')
                if not self._tuning:
                    from .batch_tuning import conservative
                    conservative(self._batch_keys[style],reduced)
            finally:
                inputs=None
                for im in padded:im.close()
        return np.concatenate(result) if result else np.empty((0,768),np.float32)

    def close(self):self.model=None;self.dino=None

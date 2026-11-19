"""Cheap image checks first; CLIP inference runs only on surviving stills."""
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import math
import warnings
import zlib
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Config, MODEL_ID, MODEL_REVISION

# Multiple descriptions per group reduce dependence on one particular wording.
PROMPTS = {
    "photo": [
        "a photograph",
        "a photo of a person",
        "a photographic portrait of a real person",
        "a photograph of a landscape or a city",
        "a photograph of an animal",
        "a photograph of food or a real object",
        "a photograph with a small watermark",
    ],
    "screenshot": [
        "a screenshot of a phone app or computer screen with interface elements",
        "a screenshot of a website with browser interface elements",
        "a screenshot of a video game or computer application",
        "a screen capture of messages, a document or a presentation slide",
    ],
    "poster": [
        "a flat graphic design poster with prominent text and a photograph",
        "an advertisement, promotional banner or infographic",
        "a typography poster, a flyer or a collage of text and pictures",
        "a flat scanned page, a document, a chart or a diagram",
    ],
    "art": [
        "a drawing, cartoon, anime illustration or painting",
        "a synthetic computer generated 3D rendering",
        "a digital illustration or graphic design",
    ],
    "meme": [
        "an internet meme with a funny reaction face and a caption",
        "a low resolution reaction image used as a chat sticker",
        "a screenshot of a movie or television scene with subtitles",
        "a social media reaction meme with text over a photo",
        "a pixelated blurry internet meme",
        "a chat emoticon or a funny sticker",
    ],
}

_exif_lock = threading.Lock()
_model_lock = threading.RLock()
_models = {}
_model_users = 0
_gpu_lock=threading.RLock()

@contextmanager
def gpu_work(device):
    # Independent pages may decode in parallel. Avoid competing CUDA forwards
    # and concurrent activation peaks while retaining shared frozen weights.
    if device=='cuda':
        with _gpu_lock:yield
    else:yield

@contextmanager
def model_session():
    """Keep shared weights alive until the last running filter releases them."""
    global _model_users
    with _model_lock:_model_users+=1
    try:yield
    finally:
        with _model_lock:
            _model_users-=1
            if not _model_users:release_models()

def release_models():
    """Dispose GPU tensors and preparation workers before emptying allocator."""
    import gc,sys
    with _model_lock:
        if _model_users:return False
        cores=list(_models.values());_models.clear()
        for core in cores:
            close=getattr(core,'close',None)
            if close:close()
        cores.clear();gc.collect()
        torch=sys.modules.get('torch')
        if torch is not None and torch.cuda.is_initialized():torch.cuda.empty_cache()
    return True


def oriented_image(original, metrics):
    """Missing EXIF is normal; broken EXIF is recorded, not fatal to pixels."""
    with _exif_lock, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', UserWarning)
        try:
            swapped=original.getexif().get(274) in (5,6,7,8)
            image = ImageOps.exif_transpose(original)
            metrics['_axes_swapped']=swapped
        except (OSError, ValueError, SyntaxError) as exc:
            image = original.copy()
            metrics['exif_warning'] = str(exc)
        exif_warnings = [str(w.message) for w in caught if 'exif' in str(w.message).lower()]
        if exif_warnings:
            metrics['exif_warning'] = '; '.join(exif_warnings)
    return image


def rgb_image(image):
    if image.mode in ('RGBA','LA') or 'transparency' in image.info:
        rgba = image.convert('RGBA')
        background = Image.new('RGBA',rgba.size,'white')
        background.alpha_composite(rgba)
        return background.convert('RGB')
    return image.convert('RGB')


def get_clip_core(config,emit):
    device=config.device
    if device=='auto':
        import torch
        device='cuda' if torch.cuda.is_available() else 'cpu'
    key=('clip',device,str(Path(config.model_dir).resolve()))
    with _model_lock:
        if key not in _models:_models[key]=PhotoClassifier(config,emit)
        return _models[key]


def get_search_encoder(config,emit):
    import torch
    from .search_model import SearchEncoder
    device='cuda' if config.device!='cpu' and torch.cuda.is_available() else 'cpu'
    key=('retrieval',device,str(Path(config.model_dir).resolve()))
    with _model_lock:
        if key not in _models:_models[key]=SearchEncoder(config,emit)
        return _models[key]

def get_semantic_core(config,emit):
    device=config.device
    if device=='auto':
        import torch
        device='cuda' if torch.cuda.is_available() else 'cpu'
    scheme='people'
    key = (scheme,device, str(Path(config.model_dir).resolve()))
    with _model_lock:
        if key not in _models:
            from .people_model import PeopleClassifier
            _models[key]=PeopleClassifier(config,emit)
        else:
            emit(f"识别阶段：复用本会话的视觉模型，设备 {_models[key].device}。")
    return _models[key]

def get_classifier(config, emit,report_device=True):
    import torch
    from .runtime import batch_for_device
    batch,description=batch_for_device(config,torch,True)
    from .media import RoutedClassifier
    route='SigLIP 2 + CLIP 分流，Qwen3-VL 人物核对' if config.scheme=='people' else 'CLIP + Qwen3-VL 表情包判断与按需独立贴纸补判' if config.scheme=='meme' else 'Qwen3-VL 独立拍摄呈现、后期用途与局部遮挡复核' if config.scheme=='photo' else 'Qwen3-VL 共享媒介核对 + CLIP 反应图佐证；检索分数不直接放行'
    classifier=RoutedClassifier(config,emit,batch,description)
    sizing=(f'本机固定批量 {classifier.actual_batch}' if classifier._batch_ready else '首次识别时初始化自动批量') if config.auto_batch else f'手动批量上限 {batch}'
    emit(f'内容筛选：{route}。{description}；{sizing}。')
    return classifier


@dataclass
class Inspection:
    reason: str = ""
    metrics: dict = field(default_factory=dict)
    image: object = None
    thumbnail: bytes = b""
    pixel_digest: str = ""


def quality_metrics(image):
    # Blur is measured at a common scale, on detailed tiles, to allow bokeh.
    gray = image.convert("L")
    gray.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    a = np.asarray(gray, dtype=np.float32)
    lap = -4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    tiles = [float(np.var(lap[y:y+128, x:x+128]))
             for y in range(0, lap.shape[0]-63, 128)
             for x in range(0, lap.shape[1]-63, 128)]
    sharpness = float(np.percentile(tiles, 85)) if tiles else float(np.var(lap))
    # Check 8-pixel JPEG grid on unscaled patches. Taking the maximum over
    # grid phases also covers a JPEG that was cropped by a few pixels.
    w, h = image.size
    block_values = []
    for fx, fy in ((.2, .2), (.5, .5), (.8, .8)):
        x = max(0, min(w-512, int(w*fx)-256))
        y = max(0, min(h-512, int(h*fy)-256))
        patch = np.asarray(image.crop((x, y, min(x+512, w), min(y+512, h))).convert("L"), dtype=np.float32)
        ratios = []
        for diff, axis in ((np.abs(np.diff(patch, axis=1)), 1), (np.abs(np.diff(patch, axis=0)), 0)):
            phase_means = [float(np.mean(np.take(diff, range(p, diff.shape[axis], 8), axis=axis))) for p in range(8)]
            peak = max(phase_means)
            baseline = float(np.median(phase_means))
            # A tiny absolute discontinuity on a smooth wall is not blocking.
            ratios.append((peak+2.0)/(baseline+2.0))
        block_values.append(max(ratios))
    return {"sharpness": round(sharpness, 3),
            "global_laplacian": round(float(np.var(lap)), 3),
            "blockiness": round(float(np.median(block_values)), 3)}


def inspect_image(path: Path, config: Config, need_duplicate=False):
    result = Inspection()
    try:
        source_stat=path.stat()
        with warnings.catch_warnings(record=True) as decode_warnings:
            warnings.simplefilter("ignore" if config.scheme in ('people','creative') else "error", Image.DecompressionBombWarning)
            with Image.open(path) as original:
                result.metrics.update(width=original.width, height=original.height, format=original.format)
                if config.scheme=='creative' and original.format=='GIF':
                    from .creative import GIF_THEME
                    # GIF is a file-collection rule, not a single-frame model decision.
                    # Read the primary frame to reject corrupt headers; do not render an animation.
                    original.seek(0);original.load()
                    result.metrics.update(creative_gif=True,creative_output_group=GIF_THEME,creative_theme=GIF_THEME,creative_category='gif',decision_route='GIF 文件规则；不运行语义评分',frame_count=getattr(original,'n_frames',1))
                    return result
                if original.format!='MPO' and (getattr(original, "n_frames", 1) > 1 or getattr(original, "is_animated", False)):
                    result.reason = "animated_or_multiframe"
                    return result
                if original.format=='MPO':original.seek(0);result.metrics['primary_only']=True
                if not __import__('photocleaner.thresholds',fromlist=['lower']).lower(min(original.size),config.min_side,config) and config.scheme!='creative':
                    result.reason = "too_small"
                    return result
                scaled_allowed=config.scheme in ('people','creative','meme') and not need_duplicate and original.format=='JPEG'
                if original.width * original.height > config.max_pixels and not scaled_allowed:
                    result.reason = "too_many_pixels"
                    return result
                if config.scheme in ('people','creative','meme') and max(original.size)/min(original.size)>200:
                    result.reason='people_aspect';return result
                full_size=original.size
                drafted=False
                if config.scheme in ('people','creative','meme') and not need_duplicate and original.format=='JPEG':
                    # libjpeg scaled decode preserves the full image and EXIF,
                    # while avoiding several full-size RGB copies. Same-stem
                    # equality checks still decode original pixels in full.
                    original.draft('RGB',(1024,1024));drafted=original.size!=full_size
                if original.width*original.height>config.max_pixels:
                    result.reason='too_many_pixels';return result
                decode_size=original.size
                original.load()  # Reject broken/truncated files before any move.
                oriented = oriented_image(original, result.metrics)
                image = rgb_image(oriented)
                oriented.close()
        messages=[str(w.message) for w in decode_warnings if 'exif' in str(w.message).lower()]
        if messages:
            result.metrics['exif_warning']='; '.join(messages)
        dimensions=full_size[::-1] if result.metrics.pop('_axes_swapped',False) else full_size
        result.metrics.update(width=dimensions[0], height=dimensions[1])
        if config.scheme=='creative':result.metrics['creative_small']=not __import__('photocleaner.thresholds',fromlist=['lower']).lower(min(dimensions),config.min_side,config)
        if drafted:result.metrics['scaled_jpeg_decode']=True
        if config.scheme=='photo':
            from .quality import extra_metrics,quality_reason
            result.metrics.update(quality_metrics(image));result.metrics.update(extra_metrics(image))
            result.metrics['quality_reason']=quality_reason(result.metrics,config)
        if not result.reason:
            if need_duplicate:
                result.thumbnail = zlib.compress(image.resize((96, 96), Image.Resampling.LANCZOS).tobytes())
                # Exact decoded-pixel equality is independent of file suffix.
                digest = hashlib.sha256()
                digest.update(str(image.size).encode())
                for y in range(0, image.height, 128):
                    digest.update(image.crop((0, y, image.width, min(y+128, image.height))).tobytes())
                result.pixel_digest = digest.hexdigest()
            # Release the full resolution raster before batching model inputs.
            # Full-frame padding retains screenshot UI/poster edges.
            if config.scheme in ('photo','people','creative','meme'):
                image.thumbnail((1000,1000) if config.scheme=='people' else (512,512),Image.Resampling.LANCZOS,reducing_gap=3.0)
                if max(image.size)/min(image.size)>200:
                    result.reason='people_aspect';image.close();return result
                if config.scheme!='people' and result.metrics['format'] in ('JPEG','MPO'):
                    # A canonical scaled JPEG decode makes shared content
                    # evidence independent of which filter ran first. Photo
                    # quality/duplicate checks above still used full pixels.
                    from .search_model import load_search_image
                    with warnings.catch_warnings(record=True) as canonical_warnings:
                        warnings.simplefilter('always',UserWarning)
                        result.image,_,_=load_search_image(path)
                    exif_messages=[str(w.message) for w in canonical_warnings if 'exif' in str(w.message).lower()]
                    if exif_messages:result.metrics['exif_warning']='; '.join(exif_messages)
                else:result.image=image.copy()
                result.image.info['source_key']=(str(path),source_stat.st_size,source_stat.st_mtime_ns)
            else:
                color = tuple(int(v) for v in np.asarray(image.resize((1, 1))).ravel())
                result.image = ImageOps.pad(image, (224, 224), color=color, method=Image.Resampling.LANCZOS)
        image.close()
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        result.reason = "unreadable_or_unsupported"
        result.metrics["error"] = str(exc)[:500]
    return result


def same_content(a: Inspection, b_metrics, b_thumb, b_digest):
    if (a.metrics["width"], a.metrics["height"]) != (b_metrics.get("width"), b_metrics.get("height")):
        return False
    if a.pixel_digest and a.pixel_digest == b_digest:
        return True
    if not a.thumbnail or not b_thumb:
        return False
    # Conservative allowance for JPEG vs PNG/recompression, same dimensions.
    x = np.frombuffer(zlib.decompress(a.thumbnail), np.uint8).astype(np.float32)
    y = np.frombuffer(zlib.decompress(b_thumb), np.uint8).astype(np.float32)
    d = x-y
    return float(np.abs(d).mean()) <= 1.5 and float(np.sqrt(np.mean(d*d))) <= 3.0 and float(np.percentile(np.abs(d), 99)) <= 10


class PhotoClassifier:
    def close(self):
        with self.inference_lock:
            self.model=None;self.text_features=None;self.scale=None
            if hasattr(self,'person_text_features'):self.person_text_features=None
    def __init__(self, config: Config, emit=lambda _: None):
        import torch
        from transformers import CLIPModel, CLIPProcessor
        self.torch = torch
        if config.device == "cuda" and not torch.cuda.is_available():
            detail = '当前安装的是 CPU 版 PyTorch' if torch.version.cuda is None else 'CUDA 运行时或显卡驱动未就绪'
            raise RuntimeError(f"CUDA 不可用：{detail}。可运行 fix_cuda.bat 修复运行环境，或选择 CPU。")
        self.device = "cuda" if (config.device == "cuda" or config.device == "auto" and torch.cuda.is_available()) else "cpu"
        # CUDA still performs tensor preparation on CPU. Avoid a large default
        # PyTorch thread pool competing with the explicit decode workers.
        torch.set_num_threads(max(1,min(4 if self.device=='cuda' else 8,__import__('os').cpu_count() or 1)))
        from .model_files import model_snapshot
        snapshot=model_snapshot(MODEL_ID,MODEL_REVISION,config,emit,required=('tokenizer.json','tokenizer_config.json','preprocessor_config.json','vocab.json','merges.txt'),weight='pytorch_model.bin')
        kwargs = dict(local_files_only=True)
        self.processor = CLIPProcessor.from_pretrained(snapshot, use_fast=False, **kwargs)
        # Official checkpoint is pytorch_model.bin. torch >= 2.6 uses the
        # restricted weights-only loader; do not execute model repository code.
        self.model = CLIPModel.from_pretrained(snapshot, use_safetensors=False, **kwargs).to(self.device).eval()
        texts = [text for group in PROMPTS.values() for text in group]
        inputs = self.processor(text=texts, padding=True, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            self.text_features = self.model.get_text_features(**inputs)
            self.text_features /= self.text_features.norm(dim=-1, keepdim=True)
        self.scale = self.model.logit_scale.exp().clamp(max=100)
        emit(f"加载完成：运行设备 {self.device}" + (f"，{torch.cuda.get_device_name(0)}。" if self.device=='cuda' else '。'))
        self.inference_lock = threading.Lock()

    def classify(self, images):
        torch = self.torch
        inputs = self.processor(images=images, return_tensors="pt").to(self.device)
        with self.inference_lock, torch.inference_mode():
            features = self.model.get_image_features(**inputs)
            features /= features.norm(dim=-1, keepdim=True)
            logits = (features @ self.text_features.T) * self.scale
            scores, offset = [], 0
            for group in PROMPTS.values():
                n = len(group)
                scores.append(torch.logsumexp(logits[:, offset:offset+n], dim=1)-math.log(n))
                offset += n
            values = torch.stack(scores, dim=1).softmax(dim=1).cpu().tolist()
        return [dict(zip(PROMPTS, row)) for row in values]

    def classify_people(self,images):
        from .people import PERSON_PROMPTS,TARGETS
        torch=self.torch
        with self.inference_lock,torch.inference_mode():
            if not hasattr(self,'person_text_features'):
                inputs=self.processor(text=[t for g in PERSON_PROMPTS.values() for t in g],padding=True,return_tensors='pt').to(self.device)
                self.person_text_features=self.model.get_text_features(**inputs)
                self.person_text_features/=self.person_text_features.norm(dim=-1,keepdim=True)
            expanded=[];ranges=[]
            for im in images:
                start=len(expanded);expanded.extend([im,*im.info.get('people_views',[])])
                ranges.append((start,len(expanded)))
            # Extra long-image sections do not multiply the requested batch's
            # peak GPU allocation. Classification remains one result per file.
            predictions=[]
            for start in range(0,len(expanded),max(1,len(images))):
                inputs=self.processor(images=expanded[start:start+len(images)],return_tensors='pt').to(self.device)
                features=self.model.get_image_features(**inputs);features/=features.norm(dim=-1,keepdim=True)
                predictions.extend(self.people_from_features(features))
            merged=[]
            for start,end in ranges:
                whole=predictions[start];views=predictions[start+1:end]
                best=max(PERSON_PROMPTS,key=whole.get)
                if views and best!='crowd_environment':
                    # Require two supporting sections, so a tiny incidental
                    # person in one corner cannot override a panorama.
                    ordered=sorted(views,key=lambda r:r['people'],reverse=True)
                    if ordered[1]['people']>whole['people']:
                        whole={**ordered[0],'people':ordered[1]['people'],'people_views':len(views)+1}
                merged.append(whole)
            return merged

    def people_from_features(self,features):
        from .people import PERSON_PROMPTS,TARGETS
        torch=self.torch;logits=(features@self.person_text_features.T)*self.scale
        groups=[];offset=0
        for prompts in PERSON_PROMPTS.values():
            # Each description is an alternative scene, not an ensemble vote.
            # Averaging unrelated exclusions diluted strong scenery/doll cues.
            n=len(prompts);groups.append(logits[:,offset:offset+n].max(dim=1).values);offset+=n
        group_logits=torch.stack(groups,dim=1)
        positive=group_logits[:,:len(TARGETS)].max(dim=1).values;negative=group_logits[:,len(TARGETS):].max(dim=1).values
        people=(positive-negative).sigmoid().cpu().tolist()
        values=group_logits.softmax(dim=1).cpu().tolist()
        return [{**dict(zip(PERSON_PROMPTS,row)),'people':p} for row,p in zip(values,people)]

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import math

from .paths import APP_HOME
PROJECT = APP_HOME
MODEL_ID = "openai/clip-vit-base-patch32"
MODEL_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"


@dataclass(frozen=True)
class Config:
    scheme: str = 'photo'
    people_threshold: float = .97
    people_verify_threshold: float = .95
    people_policy: str = 'semantic-v4'
    people_rescue: bool = True
    nearby: bool = False
    creative_threshold: float = .85
    creative_layout: str = 'flat'
    retain_tree: bool = True
    backup_duplicates: bool = False
    include_software: bool = True
    inclusive: bool = True
    min_side: int = 400
    photo_threshold: float = 0.55
    meme_threshold: float = 0.50
    detail_threshold: float = 0.0
    focus_reblur_threshold: float = .50
    focus_sharpness_threshold: float = 25.0
    pbdb_threshold: float = 0.0
    meme_select_threshold: float = 0.80
    reblur_threshold: float = 1.0
    sharpness_threshold: float = 0.0
    sharpness_mid_threshold: float = 0.0
    quality_mode: str = 'normal'
    blockiness_threshold: float = 1.7
    batch_size: int = 8
    auto_batch: bool = True
    workers: int = 2
    device: str = "auto"
    offline: bool = False
    sniff_all: bool = False
    model_dir: str = str(PROJECT / "models")
    max_pixels: int = 80_000_000

    def validate(self):
        if self.scheme not in ('photo','people','creative','meme'):raise ValueError('筛选方案应为 photo、people、meme 或 creative。')
        # Pages persist the shared dataclass, including historical values for
        # other schemes. Only validate fields used by the active scheme.
        if self.scheme=='creative':
            if self.creative_layout not in ('flat','tree','theme'):raise ValueError('创作图输出方式应为 flat、tree 或 theme。')
            if not math.isfinite(self.creative_threshold) or not 0<self.creative_threshold<=1:raise ValueError('创作图分数须大于 0 且不超过 1。')
        elif self.scheme=='meme':
            if not math.isfinite(self.meme_select_threshold) or not 0<self.meme_select_threshold<=1:raise ValueError('表情包入选分数须大于 0 且不超过 1。')
        elif self.scheme=='people':
            if self.people_policy!=Config().people_policy:raise ValueError('人物策略版本不匹配，请重新打开程序以迁移旧设置。')
            if not math.isfinite(self.people_threshold) or not 0<self.people_threshold<=1:raise ValueError('人物内容分数须大于 0 且不超过 1。')
            if not math.isfinite(self.people_verify_threshold) or not 0<self.people_verify_threshold<=1:raise ValueError('主体复核分数须大于 0 且不超过 1。')
        else:
            if not math.isfinite(self.focus_reblur_threshold) or not 0<self.focus_reblur_threshold<=1:raise ValueError('细节加权再模糊上限须大于 0 且不超过 1；1 不限制。')
            if not math.isfinite(self.pbdb_threshold) or self.pbdb_threshold<0:raise ValueError('分块锐度下限须为非负有限数值；0 不限制。')
            if not math.isfinite(self.focus_sharpness_threshold) or self.focus_sharpness_threshold<0:raise ValueError('细节加权清晰度下限须为非负有限数值；0 不限制。')
            if not math.isfinite(self.detail_threshold) or not 0<=self.detail_threshold<=100:raise ValueError('综合细节分下限须在 0–100；0 不限制。')
            if not math.isfinite(self.sharpness_mid_threshold) or self.sharpness_mid_threshold<0:raise ValueError('中位细节下限必须为非负有限数值，0 表示不单独限制。')
            if not all(math.isfinite(v) for v in (self.photo_threshold, self.meme_threshold, self.sharpness_threshold, self.blockiness_threshold)):
                raise ValueError("筛选阈值必须是有限数值，不能是 NaN 或无穷大。")
            if not 0 <= self.photo_threshold <= 1:
                raise ValueError("照片阈值必须介于 0 和 1 之间。")
            if not 0 < self.meme_threshold <= 1:
                raise ValueError("表情包排除阈值必须大于 0 且不超过 1。")
            if self.sharpness_threshold < 0 or self.blockiness_threshold < 1:
                raise ValueError("清晰度阈值不能为负，块效应阈值必须至少为 1。")
            if not math.isfinite(self.reblur_threshold) or not 0<self.reblur_threshold<=1:raise ValueError('再模糊上限须大于 0 且不超过 1；1 表示不据此排除。')
        if self.min_side < (0 if self.scheme=='meme' else 1):raise ValueError('尺寸必须为正数；表情包可填 0 不限尺寸。')
        if not 1 <= self.batch_size <= 4096 or not 1 <= self.workers <= 16:
            raise ValueError("模型推理每批 1–4096 张；CPU 解码线程 1–16。实际可用批量取决于显存和内存，建议逐步增加。")
        if self.device not in ("auto", "cpu", "cuda"):
            raise ValueError("设备必须是 auto、cpu 或 cuda。")
        if self.max_pixels < 1:
            raise ValueError("最大解码像素数必须为正数。")

    def key(self,legacy_v5=False,evidence=False):
        # Performance and download options do not change cached decisions.
        data = asdict(self)
        if not self.backup_duplicates:data.pop('backup_duplicates')
        if evidence:data.pop('inclusive')
        if legacy_v5 and self.scheme=='photo':
            for k in ('sharpness_mid_threshold','quality_mode'):data.pop(k)
        for k in ("batch_size", "auto_batch", "workers", "device", "offline", "model_dir"):
            data.pop(k)
        if self.scheme!='photo':
            for k in ('reblur_threshold','sharpness_mid_threshold','quality_mode','detail_threshold','focus_reblur_threshold','pbdb_threshold','focus_sharpness_threshold'):data.pop(k)
        if self.scheme!='meme':data.pop('meme_select_threshold')
        if self.scheme=='photo':
            for k in ('scheme','people_threshold','people_verify_threshold','people_policy','people_rescue','nearby','creative_threshold','creative_layout','include_software'):data.pop(k)
            data['algorithm']='photo-purpose-quality-v7-pbdb'
            if not evidence and self.detail_threshold>0:
                from .paths import resource_file
                path=resource_file('defaults','quality_model.json')
                data['detail_calibration']=hashlib.sha256(path.read_bytes()).hexdigest()
        elif self.scheme=='people':
            for k in ('photo_threshold','meme_threshold','sharpness_threshold','blockiness_threshold'):data.pop(k)
            for k in ('creative_threshold','creative_layout','include_software'):data.pop(k)
            data['algorithm']='people-semantic-v4-art-guard-v3-media1'
            from .person_detector import MODEL_ID as DETECTOR,MODEL_REVISION as DETECTOR_REVISION
            data['detector']=DETECTOR+'@'+DETECTOR_REVISION
        elif self.scheme=='meme':
            for k in ('photo_threshold','meme_threshold','sharpness_threshold','blockiness_threshold','people_threshold','people_verify_threshold','people_policy','people_rescue','nearby','creative_threshold','creative_layout','include_software'):data.pop(k)
            data['algorithm']='meme-semantic-v1'
        else:
            for k in ('photo_threshold','meme_threshold','sharpness_threshold','blockiness_threshold','people_threshold','people_verify_threshold','people_policy','people_rescue','nearby'):data.pop(k)
            data['algorithm']='creative-medium-guard-v2' if legacy_v5 else 'creative-medium-guard-v4-other'
        if self.scheme in ('photo','people','creative','meme'):
            from .people_model import MODEL_ID as PEOPLE_MODEL,MODEL_REVISION as PEOPLE_REVISION
            data['model']=PEOPLE_MODEL+'@'+PEOPLE_REVISION
        else:data["model"] = MODEL_ID + "@" + MODEL_REVISION
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def evidence_key(self):
        """Same model/image rules: stored scores can be re-decided cheaply."""
        from dataclasses import replace
        cfg=replace(self,min_side=400,max_pixels=80_000_000,sniff_all=False,retain_tree=True,backup_duplicates=False,creative_layout='flat',include_software=False,
            people_threshold=.97,people_verify_threshold=.95,people_rescue=True,nearby=False,photo_threshold=.55,
            meme_threshold=.2,meme_select_threshold=.8,creative_threshold=.85,sharpness_threshold=25,inclusive=True,detail_threshold=0,
            blockiness_threshold=1.7,reblur_threshold=.92,sharpness_mid_threshold=0,quality_mode='normal',focus_reblur_threshold=1,pbdb_threshold=0,focus_sharpness_threshold=0)
        # Raw evidence has a separate identity from output layout and gates.
        if self.scheme=='people':
            from .people_model import RULES,VERIFY_RULES,ART_RULES
            from .people_rescue import RULES as RESCUE_RULES
            from .people_scene import SCENES
            from .media import PROMPTS
            from .search_model import FEATURE_KEY
            rules='\n'.join((self.people_policy,RULES,VERIFY_RULES,ART_RULES,RESCUE_RULES,json.dumps(SCENES,sort_keys=True),json.dumps(PROMPTS,sort_keys=True),FEATURE_KEY,MODEL_ID,MODEL_REVISION,'people-evidence-v2'))
        elif self.scheme=='photo':
            from .photo import RULES
            rules=RULES
        else:
            from .content import RULES
            rules=RULES
            if self.scheme=='creative':
                from .creative import RULES as CREATIVE_RULES
                rules+='\n'+CREATIVE_RULES
        return hashlib.sha256((cfg.key(legacy_v5=self.scheme=='creative',evidence=True)+rules+'inspection-v5').encode()).hexdigest()

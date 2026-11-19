"""Editable processing defaults, independent of saved task state."""
import json
from .config import PROJECT
DIRECTORY=PROJECT/'defaults'

def processing_defaults(task):
    path=DIRECTORY/(task+'.json')
    if not path.is_file():
        from .paths import resource_file
        path=resource_file('defaults',task+'.json')
    if not path.exists():return {}
    try:
        values=json.loads(path.read_text(encoding='utf8'))
        if not isinstance(values,dict):raise ValueError('需要 JSON 对象')
        return {k:v for k,v in values.items() if not k.startswith('_')}
    except (OSError,ValueError) as exc:raise ValueError(f'默认条件文件无法读取：{path}\n{exc}') from exc

def models_cached(task,model_dir=None):
    """Inspect local snapshot files without contacting the network."""
    from pathlib import Path
    from .model_files import complete_snapshot
    from .config import MODEL_ID as CLIP,MODEL_REVISION as CLIP_REV
    from .people_model import MODEL_ID as QWEN,MODEL_REVISION as QWEN_REV
    from .search_model import MODEL_ID as SIGLIP,MODEL_REVISION as SIGLIP_REV,DINO_ID,DINO_REVISION
    from .person_detector import MODEL_ID as DETECTOR,MODEL_REVISION as DETECTOR_REV
    root=Path(model_dir or PROJECT/'models')
    specs=[(QWEN,QWEN_REV,('tokenizer.json','tokenizer_config.json','preprocessor_config.json','chat_template.json'),'model.safetensors')]
    if task in ('meme','creative','people'):specs.append((CLIP,CLIP_REV,('tokenizer.json','tokenizer_config.json','preprocessor_config.json','vocab.json','merges.txt'),'pytorch_model.bin'))
    if task in ('people','text_search','image_search'):specs.append((SIGLIP,SIGLIP_REV,('tokenizer.json','tokenizer_config.json','preprocessor_config.json'),'model.safetensors'))
    if task in ('text_search','image_search'):specs.append((DINO_ID,DINO_REVISION,('preprocessor_config.json',),'model.safetensors'))
    if task=='people':specs.append((DETECTOR,DETECTOR_REV,('preprocessor_config.json',),'model.safetensors'))
    return all(complete_snapshot(root/('models--'+repo.replace('/','--'))/'snapshots'/revision,required,weight) for repo,revision,required,weight in specs)

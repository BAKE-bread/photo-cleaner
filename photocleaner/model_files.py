"""Resolve pinned model files locally before allowing any Hub request."""
from pathlib import Path
import json

def complete_snapshot(path,required=(),weight='model.safetensors'):
    path=Path(path)
    if not all((path/name).is_file() for name in ('config.json',*required)):return False
    if (path/weight).is_file():return True
    index=path/(weight+'.index.json')
    try:
        shards=set(json.loads(index.read_text(encoding='utf8'))['weight_map'].values())
        return bool(shards) and all(Path(name).name==name and (path/name).is_file() for name in shards)
    except (OSError,ValueError,KeyError,TypeError):return False

def model_snapshot(repo,revision,config,emit=lambda _:None,required=(),weight='model.safetensors'):
    # A concrete directory also prevents Transformers from requesting optional
    # repository metadata, adapters or conversion information during loading.
    cache=Path(config.model_dir)/('models--'+repo.replace('/','--'))
    path=cache/'snapshots'/revision
    if complete_snapshot(path,required,weight):
        emit('本地模型就绪：'+repo)
        return str(path)
    if config.offline:raise RuntimeError(f'本地模型文件不完整：{repo}。请在可联网时取消离线选项以补齐必要文件。')
    emit('补齐必要模型文件：'+repo+'；完成后优先使用本地。')
    from huggingface_hub import snapshot_download
    try:
        result=snapshot_download(repo,revision=revision,cache_dir=config.model_dir,
            allow_patterns=['*.json','*.safetensors','*.bin','*.txt','*.jinja','*.model'],max_workers=1)
    except Exception as exc:
        # Another task/download may have completed the cache meanwhile. Network
        # failure never prevents use of an already complete pinned snapshot.
        if complete_snapshot(path,required,weight):return str(path)
        raise RuntimeError(f'缺少必要的本地模型文件：{repo}；联网补齐失败，已完成进度保留。') from exc
    if not complete_snapshot(result,required,weight):raise RuntimeError(f'模型文件仍不完整：{repo}。')
    return str(result)

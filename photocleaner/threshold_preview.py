"""Compare user gates on frozen evidence, without a scan, decode or model load.

This is a comparison of screening eligibility, not a file-operation plan.
Duplicate destinations are deliberately left to the existing execution runner.
Incomplete evidence is counted explicitly rather than guessed as a rejection.
"""
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import json
import sqlite3
import uuid

from .config import Config
from .decisions import apply_scores, explain_decision
from .storage import REASONS
from .thresholds import lower


CHANGES = ('新增符合', '不再符合', '继续符合', '继续保留', '待补算', '已执行')
THRESHOLD_FIELDS = {
    'photo': (
        ('min_side', '宽高下限（px）'), ('photo_threshold', '照片分数下限'),
        ('meme_threshold', '后期反应 / 文字分上限'),
        ('focus_sharpness_threshold', '细节加权清晰度下限'),
        ('focus_reblur_threshold', '细节加权再模糊上限'),
        ('pbdb_threshold', '分块锐度 PBDB 下限'),
        ('sharpness_threshold', '局部清晰度下限'), ('reblur_threshold', '全图再模糊上限'),
        ('sharpness_mid_threshold', '中位细节下限'), ('blockiness_threshold', '8px 网格上限'),
    ),
    'people': (('min_side', '宽高下限（px）'), ('people_threshold', '人物分数下限'), ('people_verify_threshold', '主体复核分数下限')),
    'meme': (('min_side', '宽高下限（px，0 不限）'), ('meme_select_threshold', '表情包分数下限')),
    'creative': (('min_side', '常规尺寸分界（px）'), ('creative_threshold', '创作总分下限')),
}


@dataclass(frozen=True)
class Eligibility:
    accepted: bool | None
    reason: str
    metrics: dict
    note: str = ''


def eligibility(row, config, evidence_key=None):
    """The same decision functions as Engine; deliberately no pixel fallback."""
    try:
        metrics = json.loads(row['metrics'] or '{}')
        if not isinstance(metrics, dict):
            raise ValueError('invalid metrics')
    except (ValueError, TypeError):
        return Eligibility(None, 'comparison_pending', {}, '已有指标不可读，完整预览可重新计算。')
    reason = metrics.get('inspection_reason', '')
    static = ('non_image_extension', 'unreadable_or_unsupported', 'animated_or_multiframe', 'people_aspect', 'empty_file')
    if row['reason'] in static and not metrics.get('category_scores') and not metrics.get('creative_gif'):
        return Eligibility(False, row['reason'], metrics)
    if reason in ('animated_or_multiframe', 'people_aspect', 'unreadable_or_unsupported'):
        return Eligibility(False, reason, metrics)
    if metrics.get('evidence_key') != (evidence_key or config.evidence_key()):
        return Eligibility(None, 'comparison_pending', metrics, '模型证据版本待更新；完整预览只补算需要更新的部分。')
    if config.scheme == 'creative' and metrics.get('creative_gif'):
        return Eligibility(True, 'selected', metrics)
    if not all(key in metrics for key in ('width', 'height')):
        return Eligibility(None, 'comparison_pending', metrics, '尺寸证据尚未完成。')
    if config.scheme != 'creative' and not lower(min(metrics['width'], metrics['height']), config.min_side, config):
        return Eligibility(False, 'too_small', metrics)
    if reason in ('too_many_pixels', 'error', 'changed'):
        return Eligibility(None, 'comparison_pending', metrics, '读取或像素安全检查需在完整预览中复核。')
    scores = metrics.get('category_scores')
    if not scores:
        return Eligibility(None, 'comparison_pending', metrics, '放宽后的范围包含尚未计算内容分数的图像。')
    if config.scheme == 'photo':
        from .quality import QUALITY_VERSION
        required = ('sharpness', 'reblur', 'sharpness_mid', 'pbdb', 'focus_reblur', 'focus_sharpness')
        if metrics.get('quality_version') != QUALITY_VERSION or not all(key in metrics for key in required):
            return Eligibility(None, 'comparison_pending', metrics, '画质证据尚未完成。')
    if config.scheme == 'people':
        from .decisions import people_decision
        accepted, _ = people_decision(scores, config)
        if not scores.get('semantic_model') or (accepted and not scores.get('art_check')):
            return Eligibility(None, 'comparison_pending', metrics, '新门槛下需补充人物主体核对。')
        if not accepted and config.people_rescue and 'rescue_scores' not in scores:
            from .people_rescue import candidate
            if candidate(scores):
                return Eligibility(None, 'comparison_pending', metrics, '新门槛下需补充人物类型判定。')
        if config.nearby:
            return Eligibility(None, 'comparison_pending', metrics, '已启用邻域辅助；完整预览会结合邻近图像重新核对。')
    reason = apply_scores(scores, config, metrics)
    return Eligibility(not reason, reason or 'selected', metrics)


def compare_thresholds(database, run_id, old_config, new_config, directory, stop=None):
    """Write a separate immutable snapshot; the source journal is read-only."""
    new_config.validate()
    if old_config.scheme != new_config.scheme or old_config.evidence_key() != new_config.evidence_key():
        raise ValueError('门槛影响对比适用于同一筛选方案与模型证据。')
    target_dir = Path(directory) / uuid.uuid4().hex
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / 'state.sqlite3'
    counts = dict.fromkeys(CHANGES, 0)
    states = {}
    evidence_key = new_config.evidence_key()
    config_key = new_config.key()
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.row_factory = sqlite3.Row
        src.execute('BEGIN')
        schema = src.execute("SELECT sql FROM sqlite_master WHERE name='files' AND type='table'").fetchone()[0]
        dst.execute(schema)
        columns = [r[1] for r in src.execute('PRAGMA table_info(files)')]
        insert = 'INSERT INTO files VALUES('+','.join('?' for _ in columns)+')'
        output = []
        for row in src.execute('SELECT * FROM files WHERE seen_run=? ORDER BY source', (run_id,)):
            if stop is not None and stop.is_set():
                raise InterruptedError('门槛对比已取消，原预览保留。')
            item = dict(row)
            if row['state'] in ('moved', 'copied', 'recycled', 'grouped', 'review', 'error'):
                metrics = json.loads(row['metrics'] or '{}')
                change = '已执行' if row['state'] in ('moved', 'copied', 'recycled', 'grouped') else '待补算'
                old_reason = new_reason = row['reason']
                old_note = new_note = REASONS.get(row['reason'], row['reason'])
            else:
                old = eligibility(row, old_config, evidence_key)
                new = eligibility(row, new_config, evidence_key)
                if old.accepted is None or new.accepted is None:
                    change = '待补算'
                elif new.accepted:
                    change = '继续符合' if old.accepted else '新增符合'
                else:
                    change = '不再符合' if old.accepted else '继续保留'
                metrics = new.metrics
                old_reason, new_reason = old.reason, new.reason
                old_note = old.note or explain_decision(old.reason, old.metrics, old_config)
                new_note = new.note or explain_decision(new.reason, new.metrics, new_config)
                item.update(state='queued' if new.accepted is None else 'selected' if new.accepted else 'rejected', reason=new.reason)
                # Eligibility comparison must not masquerade as a duplicate
                # move plan. Execution recomputes the destination disposition.
                item['destination'] = None
            metrics.update(threshold_change=change, threshold_old_reason=old_reason, threshold_new_reason=new_reason,
                           threshold_old_note=old_note, threshold_new_note=new_note)
            item['metrics'] = json.dumps(metrics, ensure_ascii=False)
            item['thumb'] = None
            item['config_key'] = config_key
            output.append(tuple(item[key] for key in columns))
            counts[change] += 1
            states[item['state']] = states.get(item['state'], 0) + 1
            if len(output) >= 512:
                dst.executemany(insert, output)
                output.clear()
        if output:
            dst.executemany(insert, output)
        dst.execute('CREATE INDEX comparison_scope ON files(seen_run,state,source)')
        dst.execute('CREATE TABLE runs(id TEXT PRIMARY KEY, config TEXT, mode TEXT)')
        dst.execute('INSERT INTO runs VALUES(?,?,?)', (run_id, json.dumps(asdict(new_config)), 'preview'))
        roots=[row[0] for row in src.execute('SELECT DISTINCT root FROM files WHERE seen_run=?',(run_id,))]
        for parent in Path(database).resolve().parents:
            if parent.name=='.photocleaner':
                roots.append(str(parent.parent));break
        dst.execute('CREATE TABLE debug_metadata(origin TEXT,roots TEXT,conditions TEXT)')
        dst.execute('INSERT INTO debug_metadata VALUES(?,?,?)',(str(database),json.dumps(roots),json.dumps({'comparison':True,'config':asdict(new_config)})))
        dst.commit()
    return {'run_id':run_id, 'mode':'preview', 'comparison':True, 'comparison_counts':counts,
            'counts':states, 'scanned':sum(counts.values()), 'config':asdict(new_config),
            'comparison_old_config':asdict(old_config), 'comparison_source':str(database),
            'report':str(target_dir / ('report_'+run_id+'.csv')), 'metadata':str(target_dir)}


def threshold_config(config, values):
    """Parse user edits with the same numeric precision as normal controls."""
    from .numbers import input_float, input_int
    allowed = {key for key, _ in THRESHOLD_FIELDS[config.scheme]}
    updates = {key: (input_int(value) if key=='min_side' else input_float(value)) for key, value in values.items() if key in allowed}
    proposed = replace(config, **updates)
    proposed.validate()
    return proposed

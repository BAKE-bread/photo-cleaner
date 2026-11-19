"""Read-only descriptions of the file operations already decided by a task.

Plans describe a journal snapshot. They never execute transfers or infer model
scores; the existing runners remain responsible for final file verification.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import sqlite3

from .result_status import STATUS_SQL


@dataclass(frozen=True)
class PlanItem:
    action: str
    count: int
    bytes: int
    destination: str


@dataclass(frozen=True)
class OperationPlan:
    items: tuple[PlanItem, ...]
    completed: int
    kept: int
    existing: int
    unresolved: int
    queued: int
    recovery: str
    space_note: str
    partial: bool

    @property
    def pending(self):
        return sum(item.count for item in self.items)


def planned_target(row, metrics, task, config, result):
    """One shared destination description for details, cards and the plan."""
    if row.get('destination'):
        return row['destination']
    if row['state'] != 'selected':
        return ''
    source = Path(row['source'])
    if task == 'group':
        from .grouping import group_directory
        parent = metrics.get('parent')
        category = metrics.get('category')
        return str(group_directory(parent, category) / source.name) if parent and category else '等待命名分组计划完成'
    if task == 'hash' and metrics.get('role') == 'recycle':
        return '回收站；保留文件：' + metrics.get('duplicate_of', '')
    if result.get('output_unspecified'):
        return '执行前选择输出目录'
    if result.get('output'):
        output = Path(result['output'])
    else:
        metadata = Path(result.get('metadata') or Path(result['report']).parent)
        output = next((p.parent for p in (metadata, *metadata.parents) if p.name == '.photocleaner'), metadata.parent)
    if task == 'hash':
        return str(output / source.name) + '（同名冲突时编号）'
    if row['reason'] == 'backup_duplicate':
        target = output / '重复副本备份' / source.name
    elif task == 'creative':
        from .creative import output_path
        target = output_path(output, source, Path(row['root']), row['relative'], metrics, config)
    elif task == 'flatten' or not config.retain_tree:
        target = output / source.name
    else:
        root = Path(row['root'])
        bucket = root.name + '_' + hashlib.sha256(str(root).encode()).hexdigest()[:8]
        target = output / bucket / row['relative']
    if task == 'compress':
        target = target.with_suffix('.jpg')
    elif task == 'repair':
        target = target.with_suffix(metrics.get('repair_suffix', '.png'))
    return str(target) + '（同名冲突时编号）'


def read_plan(database, run_id, task, config, result):
    """Aggregate in SQLite: memory use is independent of library size."""
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=10) as db:
        db.execute('BEGIN')
        rows = db.execute(
            "SELECT state,(" + STATUS_SQL + "),COALESCE(json_extract(metrics,'$.role'),''),"
            'COUNT(*),COALESCE(SUM(size),0) FROM files WHERE seen_run=? GROUP BY 1,2,3',
            (run_id,),
        ).fetchall()
    pending = {}
    completed = kept = existing = unresolved = queued = 0
    for state, display_state, role, count, byte_count in rows:
        if state == 'selected':
            action = '回收重复副本' if task == 'hash' and role == 'recycle' else '生成副本' if task in ('repair', 'compress') else '原目录内分组' if task == 'group' else '剪切到输出'
            old = pending.get(action, (0, 0))
            pending[action] = (old[0] + count, old[1] + byte_count)
        elif state in ('moved', 'copied', 'recycled'):
            completed += count
        elif display_state == 'existing' and not (task == 'hash' and result.get('output_is_input')):
            existing += count
        elif state in ('rejected', 'grouped'):
            kept += count
        elif state == 'queued':
            queued += count
        else:
            unresolved += count
    output = '执行前选择输出目录' if result.get('output_unspecified') else str(result.get('output') or getattr(config, 'std_path', '') or '')
    destination = {'回收重复副本': 'Windows 回收站', '原目录内分组': '各原目录的“来源_…”子目录'}
    items = tuple(PlanItem(action, count, byte_count, destination.get(action, output)) for action, (count, byte_count) in pending.items())
    if task == 'hash':
        if getattr(config, 'hash_mode', 'consolidate') == 'inplace':
            recovery = '保留文件维持原路径。回收副本可在 Windows 回收站还原到原位置。'
        elif config.hash_mode == 'std':
            recovery = '基准目录维持原样。回收副本在回收站中；其暂存前的原路径记录在 move_history.csv。'
        else:
            recovery = '代表文件保存在输出。回收副本可从回收站取回；原路径及暂存位置见 move_history.csv。'
    elif task in ('compress', 'repair'):
        recovery = '原件保留在输入目录。本次仅在输出生成新副本。' + ('生成后可进入压缩对比列表，单独选择回写。' if task == 'compress' else '')
    else:
        recovery = '执行后可用本功能的“恢复上次' + ('分组' if task == 'group' else '剪切') + '”恢复；请保留操作记录。'
    space_note = '体积列为待处理原文件的合计。压缩及转码的实际输出体积以生成结果为准。' if task in ('compress', 'repair') else '体积列为待处理文件的合计；跨磁盘剪切需要临时可用空间，实际执行逐文件核验。'
    return OperationPlan(items, completed, kept, existing, unresolved, queued, recovery, space_note,
                         bool(result.get('live') or result.get('stopped') or result.get('fatal_error') or result.get('scan_issues') or queued))


def preview_signature(task, config, roots, output):
    """Bind confirmation to conditions and paths, never to a mutable UI flag."""
    paths=list(map(Path,roots))
    if not (task=='hash' and config.hash_mode=='inplace'):
        paths=sorted((p for p in paths if not any(other in p.parents for other in paths)),key=str)
    return task, config.key(), tuple(map(str, paths)), str(Path(output)) if output else None


def matching_preview(result, task, config, roots, output):
    if not result or result.get('mode') != 'preview' or result.get('live') or result.get('stopped') or result.get('fatal_error') or result.get('scan_issues'):
        return False
    if result.get('counts', {}).get('queued') or result.get('output_unspecified'):
        return False
    from .config import Config
    from .tasks import TaskConfig
    cls = Config if task in ('photo', 'people', 'creative', 'meme') else TaskConfig
    try:
        old = cls(**{key: value for key, value in result['config'].items() if key in cls.__dataclass_fields__})
        recorded_output = result.get('output')
        if task == 'group' or (task == 'hash' and config.hash_mode in ('inplace', 'std')):
            recorded_output = None
        return preview_signature(task, old, result['roots'], recorded_output) == preview_signature(task, config, roots, output)
    except (KeyError, TypeError, ValueError):
        return False

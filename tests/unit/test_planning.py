from dataclasses import asdict, replace
from pathlib import Path
import json

from photocleaner.config import Config
from photocleaner.planning import matching_preview, planned_target, read_plan
from photocleaner.storage import Store
from photocleaner.tasks import TaskConfig


def database(tmp_path, records):
    path = tmp_path / 'state.sqlite3'
    store = Store(path)
    for n, (state, metrics, size) in enumerate(records):
        store.db.execute('INSERT INTO files(source,root,relative,size,mtime,state,reason,metrics,seen_run) VALUES(?,?,?,?,?,?,?,?,?)',
                         (str(tmp_path / f'{n}.png'), str(tmp_path), f'{n}.png', size, 0, state, 'selected', json.dumps(metrics), 'run'))
    store.db.commit()
    store.close()
    return path


def test_consolidation_separates_moving_and_recycling(tmp_path):
    db = database(tmp_path, [('selected', {'role':'move'}, 100)] * 5 + [('selected', {'role':'recycle'}, 100)])
    plan = read_plan(db, 'run', 'hash', TaskConfig(task='hash'), {'output':str(tmp_path / 'out')})
    assert {item.action:item.count for item in plan.items} == {'剪切到输出':5, '回收重复副本':1}
    assert plan.pending == 6
    assert sum(item.bytes for item in plan.items) == 600


def test_retained_inventory_and_completed_are_separate(tmp_path):
    db = database(tmp_path, [('rejected', {'in_output':True, 'requested':False}, 9), ('rejected', {}, 8), ('moved', {}, 7), ('queued', {}, 6), ('review', {}, 5)])
    plan = read_plan(db, 'run', 'hash', TaskConfig(task='hash'), {})
    assert (plan.existing, plan.kept, plan.completed, plan.queued, plan.unresolved) == (1, 1, 1, 1, 1)
    assert plan.partial
    plan = read_plan(db, 'run', 'hash', TaskConfig(task='hash'), {'output_is_input':True})
    assert (plan.existing, plan.kept) == (0, 2)


def test_copy_plan_preserves_originals_and_avoids_size_guess(tmp_path):
    db = database(tmp_path, [('selected', {}, 12345), ('copied', {}, 8)])
    plan = read_plan(db, 'run', 'compress', TaskConfig(task='compress'), {'output':str(tmp_path / 'out')})
    assert plan.items[0].action == '生成副本'
    assert '原件保留' in plan.recovery
    assert '实际输出体积' in plan.space_note
    assert plan.completed == 1 and not plan.partial


def test_preview_confirmation_binds_paths_thresholds_and_mode():
    roots = [Path('D:/a'), Path('E:/b')]
    cfg = Config()
    result = {'mode':'preview', 'config':asdict(cfg), 'roots':list(map(str, roots)), 'output':'F:/out'}
    assert matching_preview(result, 'photo', cfg, roots, Path('F:/out'))
    assert not matching_preview(result, 'photo', replace(cfg, photo_threshold=.8), roots, Path('F:/out'))
    assert not matching_preview({**result, 'stopped':True}, 'photo', cfg, roots, Path('F:/out'))
    assert not matching_preview({**result, 'live':True}, 'photo', cfg, roots, Path('F:/out'))
    assert not matching_preview({**result, 'output_unspecified':True}, 'photo', cfg, roots, Path('F:/out'))
    cfg = TaskConfig(task='hash', hash_mode='inplace')
    result = {'mode':'preview', 'config':asdict(cfg), 'roots':list(map(str, roots))}
    assert matching_preview(result, 'hash', cfg, roots, None)
    assert not matching_preview(result, 'hash', cfg, list(reversed(roots)), None)


def test_standard_roots_have_no_incidental_order_requirement():
    cfg = TaskConfig(task='hash', hash_mode='std', std_path='E:/b')
    result = {'mode':'preview', 'config':asdict(cfg), 'roots':['E:/b', 'D:/a']}
    assert matching_preview(result, 'hash', cfg, [Path('D:/a'), Path('E:/b')], None)


def test_shared_target_covers_backup_and_repair_suffix(tmp_path):
    row = {'source':str(tmp_path / 'src' / 'x.null'), 'root':str(tmp_path / 'src'), 'relative':'x.null', 'state':'selected', 'reason':'selected'}
    result = {'output':str(tmp_path / 'out')}
    target = planned_target(row, {'repair_suffix':'.jpg'}, 'repair', TaskConfig(task='repair', retain_tree=False), result)
    assert 'x.jpg' in target
    row['reason'] = 'backup_duplicate'
    target = planned_target(row, {}, 'photo', Config(), result)
    assert '重复副本备份' in target

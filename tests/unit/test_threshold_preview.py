from dataclasses import replace
import json
import sqlite3

import pytest

from photocleaner.config import Config
from photocleaner.quality import QUALITY_VERSION
from photocleaner.storage import Store
from photocleaner.threshold_preview import compare_thresholds, eligibility, threshold_config


def photo_metrics(config, score, sharpness=100):
    return {'width':800, 'height':600, 'format':'PNG', 'frames':1, 'evidence_key':config.evidence_key(),
            'quality_version':QUALITY_VERSION, 'inspection_reason':'', 'sharpness':sharpness, 'focus_sharpness':sharpness,
            'reblur':.2, 'focus_reblur':.2, 'sharpness_mid':100, 'pbdb':1000, 'blockiness':1.1,
            'category_scores':{'photo':score, 'meme':.02, 'art':0, 'document':0, 'photo_checks':{}}}


def seed(tmp_path, config, metrics):
    db = tmp_path / 'state.sqlite3'
    store = Store(db)
    for n, item in enumerate(metrics):
        decision = eligibility({'metrics':json.dumps(item), 'reason':'selected'}, config)
        store.db.execute('INSERT INTO files(source,root,relative,size,mtime,state,reason,metrics,seen_run) VALUES(?,?,?,?,?,?,?,?,?)',
                         (f'/unopened-images/{n}.png', '/unopened-images', f'{n}.png', 100, 0, 'selected' if decision.accepted else 'rejected', decision.reason, json.dumps(item), 'run'))
    store.db.commit()
    store.close()
    return db


def rows(database):
    with sqlite3.connect(database) as db:
        return db.execute('SELECT * FROM files ORDER BY source').fetchall()


def test_impact_adds_and_removes_using_existing_evidence_only(tmp_path, monkeypatch):
    old = Config(photo_threshold=.9, focus_sharpness_threshold=25)
    source = seed(tmp_path, old, [photo_metrics(old,.95), photo_metrics(old,.85), photo_metrics(old,.95,35), photo_metrics(old,.7)])
    before = rows(source)
    def forbidden(*args, **kwargs):
        raise AssertionError('Impact preview must not decode, hash, stat or infer original images')
    monkeypatch.setattr('PIL.Image.open', forbidden)
    monkeypatch.setattr('photocleaner.moving.file_hash', forbidden)
    monkeypatch.setattr('photocleaner.moving.fingerprint', forbidden)
    monkeypatch.setattr('photocleaner.vision.get_classifier', forbidden)
    result = compare_thresholds(source, 'run', old, replace(old, photo_threshold=.8, focus_sharpness_threshold=50), tmp_path / 'views')
    assert result['comparison_counts'] == {'新增符合':1, '不再符合':1, '继续符合':1, '继续保留':1, '待补算':0, '已执行':0}
    assert rows(source) == before
    output = rows(__import__('pathlib').Path(result['report']).parent / 'state.sqlite3')
    assert len(output) == 4
    assert all(item[11] is None for item in output)  # no duplicate thumbnail blob copies


def test_relaxed_dimension_without_content_is_pending(tmp_path):
    old = Config(min_side=400)
    metrics = {'width':300,'height':600,'format':'PNG','inspection_reason':'too_small','evidence_key':old.evidence_key()}
    source = seed(tmp_path, old, [metrics])
    result = compare_thresholds(source, 'run', old, replace(old, min_side=200), tmp_path / 'views')
    assert result['comparison_counts']['待补算'] == 1
    assert result['comparison_counts']['新增符合'] == 0


def test_category_exclusion_is_not_overridden_by_low_threshold():
    config = Config()
    metrics = photo_metrics(config, .99)
    metrics['category_scores']['content_rejected'] = True
    row = {'metrics':json.dumps(metrics), 'reason':'not_photo'}
    decision = eligibility(row, replace(config, photo_threshold=.01))
    assert decision.accepted is False and decision.reason == 'not_photo'


def test_stale_evidence_and_unfinished_rows_stay_explicit(tmp_path):
    config = Config()
    metrics = photo_metrics(config,.99)
    metrics['evidence_key'] = 'old-prompt-version'
    source = seed(tmp_path, config, [metrics])
    result = compare_thresholds(source, 'run', config, config, tmp_path / 'views')
    assert result['comparison_counts']['待补算'] == 1


def test_comparison_snapshots_are_independent_and_repeatable(tmp_path):
    config = Config(photo_threshold=.9)
    source = seed(tmp_path, config, [photo_metrics(config,.85)])
    first = compare_thresholds(source, 'run', config, replace(config, photo_threshold=.8), tmp_path / 'views')
    frozen = rows(__import__('pathlib').Path(first['report']).parent / 'state.sqlite3')
    second = compare_thresholds(source, 'run', config, replace(config, photo_threshold=.95), tmp_path / 'views')
    assert first['report'] != second['report']
    assert first['comparison_counts']['新增符合'] == 1
    assert second['comparison_counts']['继续保留'] == 1
    assert rows(__import__('pathlib').Path(first['report']).parent / 'state.sqlite3') == frozen


def test_input_precision_and_validation():
    config = threshold_config(Config(), {'photo_threshold':'.856','min_side':'401'})
    assert config.photo_threshold == .86 and config.min_side == 401
    with pytest.raises(ValueError):
        threshold_config(config, {'min_side':'400.2'})
    with pytest.raises(ValueError):
        threshold_config(config, {'photo_threshold':'NaN'})


def test_creative_gif_routing_remains_unchanged():
    config = Config(scheme='creative')
    row = {'metrics':json.dumps({'format':'GIF','creative_gif':True,'evidence_key':config.evidence_key()}), 'reason':'selected'}
    assert eligibility(row, replace(config,min_side=9999)).accepted is True


@pytest.mark.parametrize('scheme', ['people','meme','creative'])
def test_other_content_schemes_reuse_their_existing_decisions(tmp_path,scheme):
    config=Config(scheme=scheme,people_rescue=False,people_threshold=.97,meme_select_threshold=.9,creative_threshold=.9)
    scores={
        'people':{'semantic_model':True,'portrait':.9,'other':.1,'people':.94,'verification_score':.99,'art_check':True},
        'meme':{'reaction':.85,'ordinary':.15,'meme_score':.85,'qwen_meme_score':.9,'clip_meme_score':.9,'meme_context_score':.99},
        'creative':{'comic':.84,'other':.16,'media_scores':{'artwork':.84,'camera':0.,'photo_collage':0.}},
    }[scheme]
    metrics={'width':800,'height':600,'evidence_key':config.evidence_key(),'inspection_reason':'','category_scores':scores}
    source=seed(tmp_path,config,[metrics])
    proposed=replace(config,**{'people':{'people_threshold':.93},'meme':{'meme_select_threshold':.8},'creative':{'creative_threshold':.8}}[scheme])
    result=compare_thresholds(source,'run',config,proposed,tmp_path/'views')
    assert result['comparison_counts']['新增符合']==1
    assert result['comparison_counts']['待补算']==0


def test_lower_people_gate_requires_missing_art_verification():
    config=Config(scheme='people',people_rescue=False,people_threshold=.93)
    metrics={'width':800,'height':600,'evidence_key':config.evidence_key(),'inspection_reason':'',
             'category_scores':{'semantic_model':True,'portrait':.9,'other':.1,'people':.94,'verification_score':.99}}
    row={'reason':'not_people','metrics':json.dumps(metrics)}
    assert eligibility(row,config).accepted is None


def test_inclusive_switch_changes_only_the_exact_boundary():
    config=Config(min_side=400)
    metrics=photo_metrics(config,.99)
    metrics['width']=400
    row={'reason':'selected','metrics':json.dumps(metrics)}
    assert eligibility(row,config).accepted is True
    assert eligibility(row,replace(config,inclusive=False)).reason=='too_small'

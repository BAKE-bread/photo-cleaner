"""Reapply user gates to immutable model/quality evidence, without decoding."""
from .numbers import number

def people_decision(scores,config):
    """Keep supplementary evidence cached while independently toggling its use."""
    from .people import decide_person
    effective=scores if config.people_rescue else {**scores,'rescue_accepted':False}
    accepted,updated=decide_person(effective,config.people_threshold,config.people_verify_threshold,config.inclusive)
    updated['category_scores']=scores
    return accepted,updated

def apply_scores(scores,config,metrics):
    from .thresholds import lower,upper
    if config.scheme in ('meme','creative','people'):
        if config.scheme=='people':
            accepted,updated=people_decision(scores,config)
        else:
            if config.scheme=='meme':from .meme import decide
            else:from .creative import decide
            accepted,updated=decide(scores,config)
        metrics.update(updated)
        if config.scheme=='creative':
            from .creative import SMALL_THEME,OTHER_THEME
            small=not lower(min(metrics.get('width',config.min_side+1),metrics.get('height',config.min_side+1)),config.min_side,config)
            metrics['creative_small']=small
            if updated.get('creative_other'):
                accepted=accepted and not small
                metrics['creative_output_group']=OTHER_THEME if accepted else ''
                return '' if accepted else 'not_creative'
            metrics['creative_output_group']=SMALL_THEME if accepted and small else ''
            if accepted and small:metrics['creative_theme']=SMALL_THEME+' / '+updated['creative_theme']
        return '' if accepted else {'people':'not_people','meme':'not_meme','creative':'not_creative'}[config.scheme]
    from .quality import quality_reason
    metrics['quality_reason']=quality_reason(metrics,config)
    metrics.update(category_scores=scores,photo_score=round(float(scores['photo']),6),meme_score=round(float(scores.get('meme',0)),6),decision_route=scores.get('decision_route','专门模型'))
    for key,value in scores.items():
        if key.startswith('photo_'):metrics[key]=value
    for key in ('screenshot','document','art','photo_collage'):
        if key in scores:metrics[key+'_score']=scores[key]
    for key in ('screen_check','physical_check','camera_check','presentation_check','photo_rescued','content_rejected'):
        if key in scores:metrics[key]=scores[key]
    best=max((k for k in ('photo','blurred','blocky','screenshot','poster','art','document','meme','uncertain') if k in scores),key=scores.get)
    if scores.get('photo_rescued',False):best='photo'
    metrics['best_category']=best
    if scores.get('content_rejected',False):return 'not_photo'
    if best in ('blurred','blocky'):return best
    if not upper(scores.get('meme',0),config.meme_threshold,config):return 'meme'
    if not lower(scores['photo'],config.photo_threshold,config) or best!='photo':return 'not_photo'
    return metrics['quality_reason']

def explain_decision(reason,metrics,config):
    """Explain the first controlling gate, without modifying scores or decisions."""
    from .storage import REASONS
    from .thresholds import symbols
    from .thresholds import lower
    lo,hi,fail_lo,fail_hi=symbols(config)
    scores=metrics.get('category_scores',{})
    if config.scheme=='photo' and scores and 'photo_checks' not in scores:
        return '旧实拍策略记录：'+REASONS.get(reason,reason)+'；重新预览以执行新版用途核对'
    if reason in ('selected',''):
        if config.scheme=='creative':
            if metrics.get('creative_gif'):return '文件规则通过：GIF 归入 08；不使用尺寸/语义门槛'
            if metrics.get('creative_other'):return '候选分流：达到尺寸与创作证据门槛，非实拍；归入 09 单独复查'
            if metrics.get('creative_small'):return f'类别与分数通过；宽或高 {fail_lo} 尺寸分界，归入 07'
        return REASONS['selected']
    if reason=='not_photo':
        if scores.get('photo_rejection'):return '类别不符：'+scores['photo_rejection']+'；先判断拍摄用途，再检查画质门槛'
        best=metrics.get('best_category','未确认')
        labels={'screenshot':'软件/网页界面','document':'文档/表格','art':'绘画/平面创作','poster':'海报','uncertain':'不确定','meme':'表情包'}
        if scores.get('screen_check',0)>=.90:return f"类别不符：页头界面/文档核对 {number(scores['screen_check'])} ≥ 0.90；照片分数不能覆盖此排除"
        if scores.get('content_rejected') and best=='photo':
            media=scores.get('media_scores',{});best={'artwork':'art','software':'document','camera':'photo'}.get(max(('camera','artwork','software'),key=lambda k:media.get(k,0)),'uncertain')
        if scores.get('content_rejected') or best!='photo':return '类别不符：'+labels.get(best,best)+'；照片分数不能覆盖类别排除'
        return f"门槛未过：照片分数 {number(metrics.get('photo_score',0))} {fail_lo} {number(config.photo_threshold)}"
    if reason=='not_creative':
        from .creative import THEMES,EXCLUDED
        best=metrics.get('creative_category','other')
        if best not in THEMES:return '类别不符：'+EXCLUDED.get(best,best)+'；媒介优先排除' if metrics.get('media_rejected') else '类别不符：'+EXCLUDED.get(best,best)
        if best=='software' and not config.include_software:return '选项未启用：软件/网页/游戏界面截图'
        if metrics.get('media_rejected'):return '类别不符：媒介核对未通过；创作主题分数不能覆盖媒介排除'
        return f"门槛未过：创作总分 {number(metrics.get('creative_score') or 0)} {fail_lo} {number(config.creative_threshold)}"
    if reason=='not_people':
        from .people import TARGETS,PERSON_LABELS
        if scores.get('scene_score',0)>=.98 and scores.get('scene_category') in ('ritual','scenery','environment'):
            return '类别不符：场景排除 — '+{'ritual':'祭典/仪式/游行','scenery':'旅游/景物环境','environment':'会场/街道环境'}[scores['scene_category']]
        if scores.get('illustration_rejected'):return '类别不符：插画/二次元画面核对排除'
        code=scores.get('verification_label','')
        names={'ritual':'祭典/仪式/游行','scenery':'旅游/景物环境','product':'商品/手持物品','illustration':'绘画/虚拟画面','environment':'会场/街道环境'}
        if code.startswith('scene:'):return '类别不符：场景排除 — '+names.get(code[6:],code[6:])
        best=metrics.get('person_category')
        if best not in TARGETS:return '类别不符：'+PERSON_LABELS.get(best,str(best))
        if scores.get('verification_rejected'):return '类别不符：主体复核未通过；画面焦点不满足人物活动要求'
        if not lower(metrics.get('people_score',0),config.people_threshold,config):return f"门槛未过：人物分数 {number(metrics.get('people_score',0))} {fail_lo} {number(config.people_threshold)}；已启用的补判也未通过"
        return f"门槛未过：主体复核 {number(metrics.get('verification_score') or 0)} {fail_lo} {number(config.people_verify_threshold)}；已启用的补判也未通过"
    if reason=='not_meme':
        if scores.get('meme_context_score') is not None and scores['meme_context_score']<.9 and metrics.get('meme_score')==0:return f"证据核对未过：反应用途 {number(scores['meme_context_score'])} < 0.9，综合分归零；单项语义分不能覆盖"
        if scores.get('clip_margin') is not None and scores['clip_margin']<0:return '证据核对未过：CLIP 反应图证据低于普通图像，未进入后续语义评分'
        if metrics.get('meme_category') in ('ordinary','uncertain'):return '类别不符：普通图像/不确定；语义表情包分数只是证据之一'
        if not lower(metrics.get('meme_score',0),config.meme_select_threshold,config):return f"门槛未过：表情包综合分数 {number(metrics.get('meme_score',0))} {fail_lo} {number(config.meme_select_threshold)}；原核对及已执行的贴纸补判未达到此门槛"
    if reason=='low_detail_score':return f"历史画质门槛未过：旧拟合细节分 {number(metrics.get('detail_score'))} {fail_lo} {number(config.detail_threshold)}；不代表实拍概率"
    if reason in ('blurred','soft_detail','low_mid_detail','blocky','soft_focus_detail','low_pbdb','low_focus_sharpness'):
        key,limit,title,relation={'blurred':('sharpness',config.sharpness_threshold,'局部清晰度',fail_lo),'low_mid_detail':('sharpness_mid',config.sharpness_mid_threshold,'中位细节',fail_lo),'blocky':('blockiness',config.blockiness_threshold,'8px 网格',fail_hi),'soft_detail':('reblur',config.reblur_threshold,'全图再模糊',fail_hi),'soft_focus_detail':('focus_reblur',config.focus_reblur_threshold,'细节加权再模糊',fail_hi),'low_focus_sharpness':('focus_sharpness',config.focus_sharpness_threshold,'细节加权清晰度',fail_lo),'low_pbdb':('pbdb',config.pbdb_threshold,'分块锐度 PBDB',fail_lo)}[reason]
        return f"画质门槛未过：{title} {number(metrics.get(key))} {relation} {number(limit)}"
    if reason=='too_small':return f'尺寸门槛未过：宽或高 {fail_lo} {config.min_side} px'
    return REASONS.get(reason,reason)

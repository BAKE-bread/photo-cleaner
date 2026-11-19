"""Columns contain only the active task's evidence; full raw metrics remain readable."""
from .numbers import number
FIELDS={
 'name':('文件名',230,None),'dimensions':('尺寸 px',110,None),'format':('格式',70,'format'),'size':('文件 MB',95,None),
 'category':('类别 / 分组',190,None),'method':('命名规律',180,'method'),'parent':('原目录',280,'parent'),
 'photo':('照片分数',100,'photo_score'),'meme':('表情包分数',105,'meme_score'),
 'screenshot':('界面分数',100,'screenshot_score'),'document':('文档分数',100,'document_score'),
 'art':('平面创作分数',110,'art_score'),'collage':('照片拼图分数',110,'photo_collage_score'),
 'quality':('清晰度',100,'sharpness'),'blockiness':('8px 网格指标',115,'blockiness'),
 'reblur':('再模糊指标',110,'reblur'),'sharpness_mid':('中位细节',100,'sharpness_mid'),
 'detail_coverage':('细节覆盖',100,'detail_coverage'),'noise_level':('噪声估计',100,'noise_level'),
 'grid_caution':('规则网格提示',120,'grid_caution'),'integrity':('完整性',110,'integrity'),
 'people':('人物分数',100,'people_score'),'verify':('复核分数',100,'verification_score'),
 'content':('内容分数',100,'content_score'),'basis':('入选依据',240,'decision_basis'),
 'creative':('创作总分',100,'creative_score'),'media_art':('媒介创作分数',115,'media_art_score'),
 'media_photo':('媒介实拍分数',115,'media_photo_score'),'media_collage':('媒介拼图分数',115,'media_collage_score'),
 'media_document':('媒介文档分数',115,'media_document_score'),'media_software':('媒介界面分数',115,'media_software_score'),
 'media_meme':('媒介表情包分数',125,'media_meme_score'),'software_enabled':('纳入软件截图',110,'software_enabled'),
 'media_rejected':('媒介不通过',110,'media_rejected'),'route':('判定路径',200,'decision_route'),
 'screen_check':('界面/文档核对',125,'screen_check'),'physical_check':('实体拍摄证据',125,'physical_check'),
 'creative_category_score':('创作主题分数',120,'creative_category_score'),'excluded_category_score':('排除类别分数',120,'excluded_category_score'),'content_rejected':('内容核对不通过',130,'content_rejected'),
 'creative_small':('小尺寸分流',110,'creative_small'),'creative_gif':('GIF 文件分流',110,'creative_gif'),
 'camera_check':('拍摄补核分数',120,'camera_check'),'qwen_meme_score':('语义表情包分数',130,'qwen_meme_score'),
 'presentation_check':('拍摄载体补核',120,'presentation_check'),'meme_context_score':('反应用途核对',120,'meme_context_score'),
 'clip_margin':('CLIP 正负差值',125,'clip_margin'),'clip_meme_score':('CLIP 反应图分数',130,'clip_meme_score'),
 'sha':('SHA256',300,'sha256'),'role':('处理动作',100,'role'),'duplicate_of':('代表文件',300,'duplicate_of'),
 'in_output':('已在输出',90,'in_output'),'requested':('属于输入',90,'requested'),
 'priority_rank':('目录保留优先级',140,'priority_rank'),
 'jpg_quality':('JPG 质量',95,'jpg_quality'),'size_mb':('原图 MB',100,'size_mb'),'output_bytes':('JPG 字节数',120,'output_bytes'),
 'container_note':('容器处理',180,'container_note'),
 'repair_action':('修复方式',190,'repair_action'),'repair_format':('输出格式',100,'repair_format'),
 'windows_decode':('Windows 系统解码',170,'windows_decode'),'filename_check':('文件名检查',130,'filename_check'),
 'repair_basis':('检查依据',300,'repair_basis'),'output_windows_decode':('副本系统解码',150,'output_windows_decode'),
 'state':('状态',100,None),'reason':('判定说明',260,None),'target':('目标路径 / 计划',380,None),
}
for key,title in [('reaction','反应贴纸'),('macro','配字改图'),('comic_joke','梗图短段子'),('joke_screen','段子截图'),('ordinary','普通图像'),('uncertain','不确定')]:
    FIELDS[key]=(title+'分数',115,key+'_score')
FIELDS.update({
 'detail_score':('旧拟合细节分（参考）',170,'detail_score'),
 'pbdb':('分块锐度 PBDB',145,'pbdb'),
 'focus_reblur':('细节加权再模糊',145,'focus_reblur'),
 'focus_sharpness':('细节加权清晰度',145,'focus_sharpness'),
 'photo_paper':('实体纸张拍摄复核',155,'photo_paper_score'),
 'photo_caption':('拍摄用途核对',120,'photo_caption_score'),
 'photo_presentation':('拍摄呈现核对',120,'photo_presentation_score'),
 'photo_navigation':('界面残留核对',120,'photo_navigation_score'),
 'photo_minor':('局部遮挡/装饰',120,'photo_minor_score'),
 'photo_local':('遮挡外实拍证据',125,'photo_local_score'),
 'photo_printed':('实体印刷复核',120,'photo_printed_score'),
 'photo_native':('节目原框/装饰',120,'photo_native_score'),
 'post_text':('后期反应/文字分',125,'meme_score'),
 'creative_evidence':('创作证据分',115,'creative_evidence_score'),
 'creative_other':('其他创作分流',115,'creative_other'),
 'sticker_rescue':('独立贴纸复核',120,'sticker_rescue_score'),
 'file_size_rule':('生效体积条件',240,'file_size_rule'),
 'is_std':('基准图像',100,'is_std'),
 'threshold_change':('门槛变化',125,'threshold_change'),
})
TASK_FIELDS={
 'photo':['photo','photo_caption','photo_presentation','post_text','photo_navigation','photo_minor','photo_local','photo_printed','photo_paper','photo_native','content_rejected','pbdb','focus_sharpness','focus_reblur','quality','reblur','sharpness_mid','blockiness','detail_coverage','noise_level','grid_caution','integrity','route'],
 'people':['people','verify','content','category','basis'],
 'creative':['creative','creative_evidence','creative_other','creative_category_score','excluded_category_score','category','creative_small','creative_gif','media_art','media_photo','media_document','media_software','media_meme','meme_context_score','sticker_rescue','screen_check','physical_check','camera_check','presentation_check','media_rejected','software_enabled','route'],
 'meme':['meme','category','qwen_meme_score','clip_margin','clip_meme_score','meme_context_score','sticker_rescue','reaction','macro','comic_joke','joke_screen','ordinary','uncertain','route'],
 'group':['method','category','parent'],'small':[],'file_size':['file_size_rule'],
 'flatten':['sha','duplicate_of'],'hash':['sha','role','duplicate_of','priority_rank','in_output','requested','is_std'],
 'compress':['size_mb','output_bytes','jpg_quality','container_note'],
 'repair':['windows_decode','filename_check','repair_basis','repair_action','repair_format','output_windows_decode'],
}
def legacy_columns_for(task):
    common=['name','size'] if task in ('flatten','hash') else ['name','dimensions','format','size']
    return common+TASK_FIELDS.get(task,[])+['state','reason','target']

def columns_for(task):
    # File, outcome, primary evidence, file properties, secondary evidence,
    # destination. This shared hierarchy keeps decisions close to the name.
    primary={
        'photo':['photo','post_text','focus_sharpness','focus_reblur'],
        'people':['people','verify','content'],
        'creative':['creative','creative_evidence'],
        'meme':['meme','qwen_meme_score','clip_meme_score'],
        'group':['method'],'hash':['role'],'compress':['size_mb','output_bytes'],
        'file_size':['file_size_rule'],
    }.get(task,[])
    fields=TASK_FIELDS.get(task,[])
    outcome=['category'] if 'category' in fields else []
    properties=['size'] if task in ('flatten','hash','file_size') else ['dimensions','size','format']
    first=['name','state',*outcome,*primary,'reason',*properties]
    return list(dict.fromkeys(first+[k for k in fields if k not in first]+['target']))

def saved_order(columns,saved,task=None):
    if task is not None and saved==legacy_columns_for(task):return list(columns)
    return list(dict.fromkeys(k for k in saved if k in columns))+[k for k in columns if k not in saved]

def numeric_expression(metric):
    """Old creative reports used zero for uncomputed taxonomy; SQL agrees with UI."""
    if metric=='@size_mb':return 'size/1000000.0','1'
    field="json_extract(metrics,'$."+metric+"')"
    numeric="json_type(metrics,'$."+metric+"') IN ('real','integer','true','false')"
    if metric=='creative_score':
        field="CASE WHEN json_extract(metrics,'$.category_scores.media_rejected') THEN NULL ELSE "+field+' END'
    elif metric in ('creative_category_score','excluded_category_score'):
        target="json_extract(metrics,'$.creative_category') IN ('comic','illustration','design','information','render','software')"
        if metric=='creative_category_score':field='CASE WHEN '+target+' THEN '+field+' END'
        else:field='CASE WHEN NOT ('+target+") THEN COALESCE("+field+",json_extract(metrics,'$.creative_category_score')) END"
    if metric.startswith(('creative_','excluded_')):numeric='('+field+') IS NOT NULL'
    return field,numeric

def cell_value(key,row,m):
    if key=='post_text' and not m.get('category_scores',{}).get('photo_checks'):return '—'
    if key=='name':return __import__('pathlib').Path(row['source']).name
    if key=='dimensions':return f"{number(m['width'])} × {number(m['height'])}" if 'width' in m and 'height' in m else '—'
    if key=='size':return number(row['size']/1e6)
    if key=='role':return {'keep':'保留','recycle':'回收副本','move':'移入输出','compare':'待比对'}.get(m.get('role'),'—')
    if key=='category':return m.get('category') or m.get('meme_category_label') or m.get('creative_theme') or m.get('person_category_label') or '—'
    value=m.get(FIELDS[key][2])
    if value is None:return '—'
    if isinstance(value,bool):return '是' if value else '否'
    if isinstance(value,(float,int)):return number(value)
    return str(value)

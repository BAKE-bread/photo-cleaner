"""Immutable, disk-backed debug result views; drafts never mutate an applied view."""
from pathlib import Path
import json, math, sqlite3, uuid
from contextlib import contextmanager,closing
from .debug_tools import numeric_fields, image_clause, OPS
from .result_fields import numeric_expression
from .numbers import metric_input

CATEGORY_SQL="""COALESCE(json_extract(metrics,'$.category'),json_extract(metrics,'$.meme_category_label'),
    CASE WHEN json_extract(metrics,'$.creative_theme')='不属创作图' THEN
        CASE json_extract(metrics,'$.creative_category') WHEN 'photo' THEN '实拍照片' WHEN 'photo_collage' THEN '实拍照片拼图' WHEN 'meme' THEN '表情包/反应图' ELSE '其他/无法归类' END
    ELSE json_extract(metrics,'$.creative_theme') END,json_extract(metrics,'$.person_category_label'),'—')"""

def expression_sql(task,node,depth=0):
    if depth>40:raise ValueError('条件组嵌套过深，请简化。')
    if not node:return '1',[]
    if node.get('kind') in ('all','any','not'):
        children=[expression_sql(task,c,depth+1) for c in node.get('children',[])]
        if not children:return ('0' if node['kind']=='any' else '1'),[]
        join=' AND ' if node['kind']=='all' else ' OR '
        sql='('+join.join('(COALESCE(('+c[0]+'),0))' for c in children)+')'
        return ('NOT '+sql if node['kind']=='not' else sql),[v for c in children for v in c[1]]
    title=node.get('metric');op=node.get('op');value=node.get('value','')
    text={'文件名':'basename(source)','类别 / 分组':CATEGORY_SQL,'判定代码':'reason'}
    if title in text:
        field=text[title]
        if op in ('包含','不包含'):return ('NOT ' if op=='不包含' else '')+'(instr(lower('+field+'),lower(?))>0)',[str(value)]
        if op not in ('=','≠'):raise ValueError('文字条件请选择包含、不包含、= 或 ≠。')
        return '('+field+') '+OPS[op]+' ?',[str(value)]
    metric=numeric_fields(task).get(title)
    if not metric:raise ValueError('请选择本功能的有效指标。')
    field,numeric=numeric_expression(metric)
    if op=='缺失':return 'NOT COALESCE(('+numeric+'),0)',[]
    if op=='有值':return 'COALESCE(('+numeric+'),0)',[]
    if op not in OPS:raise ValueError('数值条件请选择数值比较关系。')
    number=metric_input(value,metric)
    if not math.isfinite(number):raise ValueError('比较值须为有限数值。')
    return '('+numeric+') AND ('+field+') '+OPS[op]+' ?',[number]

@contextmanager
def connect_read(database,previous=None):
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    db.create_function('basename',1,lambda p:Path(p).name)
    if previous:db.execute('ATTACH DATABASE ? AS prior',(Path(previous).resolve().as_uri()+'?mode=ro',))
    try:yield db
    finally:db.close()

def range_sql(task, rule):
    if not rule or not rule.get('metric'):return '1',[]
    metric=numeric_fields(task).get(rule['metric'])
    if not metric:raise ValueError('请选择本功能的数值指标。')
    field,numeric=numeric_expression(metric)
    clauses=[numeric];args=[]
    for key,operator in [('low','≥'),('high','≤')]:
        value=rule.get(key,'')
        if str(value).strip()=='':continue
        try:value=metric_input(value,metric)
        except (ValueError,TypeError):raise ValueError('范围边界须为数值；留空表示不限。')
        if not math.isfinite(value):raise ValueError('范围边界须为有限数值。')
        op=rule.get(key+'_op',operator)
        if op not in OPS:raise ValueError('无效的范围比较关系。')
        clauses.append(field+' '+OPS[op]+' ?');args.append(value)
    return ' AND '.join(clauses),args

def view_clause(run_id, task, draft):
    clauses=["seen_run=?","state<>'queued'",image_clause()];args=[run_id]
    if draft.get('state'):
        from .result_status import STATUS_SQL
        clauses.append('('+STATUS_SQL+')=?');args.append(draft['state'])
    if draft.get('category'):clauses.append(CATEGORY_SQL+'=?');args.append(draft['category'])
    if 'expression' in draft:
        expr,values=expression_sql(task,draft['expression']);clauses.append('('+expr+')');args.extend(values)
        if draft.get('sources') is not None:clauses.append('source IN (SELECT source FROM chosen)')
        matched='COALESCE(('+(' AND '.join(clauses[3:]) or '1')+'),0)';base=' AND '.join(clauses[:3])
        operation=draft.get('operation','replace')
        if operation!='replace':
            if not draft.get('previous'):raise ValueError('请先应用一次筛选，再组合当前结果。')
            old='source IN (SELECT source FROM prior.files)'
            combined={'intersect':f'({old}) AND ({matched})','union':f'({old}) OR ({matched})','subtract':f'({old}) AND NOT ({matched})','xor':f'({old}) != ({matched})'}
            if operation not in combined:raise ValueError('未知结果组合方式。')
            return base+' AND ('+combined[operation]+')',args
        return ' AND '.join(clauses),args
    for key in ('A','B'):
        clause,values=range_sql(task,draft.get(key));clauses.append('('+clause+')');args.extend(values)
    return ' AND '.join(clauses),args

def create_view(database, run_id, task, draft, directory):
    """Freeze whole row values in one read transaction, including raw metrics."""
    clause,args=view_clause(run_id,task,draft)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    target=directory/(uuid.uuid4().hex+'.sqlite3')
    with connect_read(database,draft.get('previous')) as src,closing(sqlite3.connect(target)) as dst:
        src.execute('BEGIN')
        if draft.get('sources') is not None:
            src.execute('CREATE TEMP TABLE chosen(source TEXT PRIMARY KEY)');src.executemany('INSERT OR IGNORE INTO chosen VALUES(?)',((p,) for p in draft['sources']))
        schema=src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='files'").fetchone()[0]
        dst.execute(schema)
        columns=[r[1] for r in src.execute('PRAGMA table_info(files)')]
        query='INSERT INTO files VALUES('+','.join('?' for _ in columns)+')'
        # The view freezes metadata/evidence, not a second copy of thumbnail
        # blobs used by pixel deduplication. Hover previews use the actual file.
        selected_columns=','.join('NULL AS thumb' if name=='thumb' else name for name in columns)
        rows=src.execute('SELECT '+selected_columns+' FROM files WHERE '+clause,args)
        while batch:=rows.fetchmany(2000):dst.executemany(query,batch)
        dst.execute('CREATE INDEX view_order ON files(seen_run,source)')
        dst.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,config TEXT)')
        dst.executemany('INSERT INTO runs VALUES(?,?)',src.execute('SELECT id,config FROM runs WHERE id=?',(run_id,)))
        roots=[r[0] for r in src.execute('SELECT DISTINCT root FROM files WHERE seen_run=?',(run_id,))]
        for p in Path(database).resolve().parents:
            if p.name=='.photocleaner':roots.append(str(p.parent));break
        dst.execute('CREATE TABLE debug_metadata(origin TEXT,roots TEXT,conditions TEXT)')
        dst.execute('INSERT INTO debug_metadata VALUES(?,?,?)',(str(database),json.dumps(roots),json.dumps(draft,ensure_ascii=False)))
        count=dst.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        dst.commit()
    return target,count

def prospective_count(database,run_id,task,draft):
    clause,args=view_clause(run_id,task,draft)
    with connect_read(database,draft.get('previous')) as db:return db.execute('SELECT COUNT(*) FROM files WHERE '+clause,args).fetchone()[0]

def prospective_change(database,run_id,task,draft,current=None):
    clause,args=view_clause(run_id,task,draft)
    with connect_read(database,draft.get('previous')) as db:
        db.execute('BEGIN')
        if current:
            db.execute('ATTACH DATABASE ? AS active',(Path(current).resolve().as_uri()+'?mode=ro',))
            member='source IN (SELECT source FROM active.files)'
            old=db.execute('SELECT COUNT(*) FROM active.files').fetchone()[0]
        else:
            member='1';old=db.execute("SELECT COUNT(*) FROM files WHERE seen_run=? AND state<>'queued' AND "+image_clause(),(run_id,)).fetchone()[0]
        count,overlap=db.execute('SELECT COUNT(*),COALESCE(SUM('+member+'),0) FROM files WHERE '+clause,args).fetchone()
        return {'count':count,'added':count-overlap,'removed':old-overlap}

def distribution(database,run_id,task,rule,bins=16):
    """Distribution of one observed metric on the current applied snapshot."""
    metric=numeric_fields(task).get(rule.get('metric'))
    if not metric:return {'count':0,'matched':0,'missing':0,'bins':[]}
    field,numeric=numeric_expression(metric)
    where="seen_run=? AND state<>'queued' AND "+image_clause()
    clause,args=range_sql(task,rule)
    with connect_read(database) as db:
        total=db.execute('SELECT COUNT(*) FROM files WHERE '+where,(run_id,)).fetchone()[0]
        count,lo,hi=db.execute('SELECT COUNT(*),MIN('+field+'),MAX('+field+') FROM files WHERE '+where+' AND '+numeric,(run_id,)).fetchone()
        matched=db.execute('SELECT COUNT(*) FROM files WHERE '+where+' AND ('+clause+')',(run_id,*args)).fetchone()[0]
        values=[0]*bins
        if count:
            bucket='0' if hi==lo else 'MIN('+str(bins-1)+',CAST(('+field+'-?)/? AS INTEGER))'
            params=() if hi==lo else (lo,(hi-lo)/bins)
            for index,n in db.execute('SELECT '+bucket+',COUNT(*) FROM files WHERE '+where+' AND '+numeric+' GROUP BY 1',(*params,run_id)):values[index]=n
    return {'count':count,'matched':matched,'missing':total-count,'min':lo,'max':hi,'bins':values}

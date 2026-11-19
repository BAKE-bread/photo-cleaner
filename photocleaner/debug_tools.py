"""Read-only metric selection and verified copies for user-curated test groups."""
from pathlib import Path
import sqlite3,json,csv,math,time,uuid
from .result_fields import FIELDS,columns_for,numeric_expression
from .storage import excel_safe
from .directory_lists import clean_path
from .numbers import metric_input

OPS={'>':'>','≥':'>=','<':'<','≤':'<=','=':'=','≠':'!='}
IMAGE_SUFFIXES={'.jpg','.jpeg','.jpe','.png','.bmp','.gif','.webp','.avif','.heic','.heif','.tif','.tiff','.mpo'}
def image_row(row):return row['state']!='queued' and (json.loads(row['metrics']).get('width',0)>0 or Path(row['source']).suffix.lower() in IMAGE_SUFFIXES)
def image_clause():return "(json_extract(metrics,'$.width')>0 OR "+' OR '.join("lower(source) GLOB '*"+suffix+"'" for suffix in sorted(IMAGE_SUFFIXES))+')'
TEXT_KEYS={'format','category','method','parent','basis','integrity','route','sha','role','duplicate_of','container_note','file_size_rule','repair_action','repair_format','windows_decode','filename_check','repair_basis','output_windows_decode'}
def numeric_fields(task):
    return {'宽 px':'width','高 px':'height','文件 MB':'@size_mb',**{FIELDS[k][0]:FIELDS[k][2] for k in columns_for(task) if FIELDS[k][2] and k not in TEXT_KEYS}}

def matching_sources(database,run_id,task,title,op,value,state=None,category=None):
    metric=numeric_fields(task).get(title)
    if not metric or op not in OPS:raise ValueError('请选择本功能的数值指标和比较关系。')
    value=metric_input(value,metric)
    if not math.isfinite(value):raise ValueError('比较值必须为有限数值。')
    field,numeric=numeric_expression(metric)
    clause="seen_run=? AND state<>'queued' AND "+image_clause()+' AND '+numeric+' AND '+field+' '+OPS[op]+' ?'
    args=[run_id,value]
    if state:
        from .result_status import STATUS_SQL
        clause+=' AND ('+STATUS_SQL+')=?';args.append(state)
    if category:
        from .debug_views import CATEGORY_SQL
        clause+=' AND '+CATEGORY_SQL+'=?';args.append(category)
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        return {r[0] for r in db.execute('SELECT source FROM files WHERE '+clause,args)}

def combine_selection(selected,matched,operation):
    if operation=='替换':return set(matched)
    if operation=='追加':return selected|matched
    if operation=='交集':return selected&matched
    if operation=='移除':return selected-matched
    raise ValueError('未知选图操作。')

def export_selection(database,run_id,selected,output,stop,emit=lambda _:None,history=()):
    from .search_copy import copy_results
    out=Path(clean_path(output)).expanduser().absolute()
    if out.resolve()!=out:raise ValueError('测试输出不能包含链接或目录联接。')
    database=Path(database).resolve()
    db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        roots=[Path(r[0]) for r in db.execute('SELECT DISTINCT root FROM files WHERE seen_run=?',(run_id,))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='debug_metadata'").fetchone():
            roots=[Path(p) for p in json.loads(db.execute('SELECT roots FROM debug_metadata').fetchone()[0])]
        for p in database.parents:
            if p.name=='.photocleaner':
                if p.parent not in roots:roots.append(p.parent)
                break
        if any(out==p or out.is_relative_to(p) or p.is_relative_to(out) for p in roots):raise ValueError('测试输出须在输入及原输出目录之外，避免测试副本进入后续扫描。')
        db.execute('CREATE TEMP TABLE chosen(source TEXT PRIMARY KEY)');db.executemany('INSERT OR IGNORE INTO chosen VALUES(?)',((p,) for p in selected))
        query="SELECT f.* FROM files f JOIN chosen c ON f.source=c.source WHERE seen_run=? AND state<>'queued' AND "+image_clause().replace('lower(source)','lower(f.source)')+' ORDER BY source'
        count=db.execute('SELECT COUNT(*) FROM ('+query+')',(run_id,)).fetchone()[0]
        if not count:raise ValueError('所选项中没有已判定且可复制的图像。')
        record=db.execute('SELECT config FROM runs WHERE id=?',(run_id,)).fetchone();config=json.loads(record[0]) if record else {}
        class Rows:
            def __len__(self):return count
            def __iter__(self):
                for row in db.execute(query,(run_id,)):
                    row=dict(row);row['path']=row['destination'] if row['state'] in ('moved','copied','grouped') else row['source']
                    row['sha']=json.loads(row['metrics']).get('sha256')
                    yield row
        out.mkdir(parents=True,exist_ok=True)
        tag=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
        manifest=out/f'debug_manifest_{tag}.jsonl';csv_path=manifest.with_suffix('.csv')
        fields=[FIELDS[k] for k in columns_for(config.get('scheme',config.get('task','photo'))) if FIELDS[k][2]]
        with manifest.open('x',encoding='utf8') as handle,csv_path.open('x',newline='',encoding='utf-8-sig') as csv_file:
            writer=csv.writer(csv_file);writer.writerow(['原来源','实际复制来源','测试副本','复制状态','SHA256','错误',*[f[0] for f in fields],'完整指标 JSON'])
            def observe(row,state,destination,digest,error):
                metrics=json.loads(row['metrics'])
                entry={'run_id':run_id,'source':row['source'],'path':row['path'],'destination':destination,'copy_state':state,'sha256':digest,'size':row['size'],'mtime_ns':row['mtime'],'decision_state':row['state'],'decision_reason':row['reason'],'metrics':metrics,'config':config,'selection_history':list(history),'error':error}
                handle.write(json.dumps(entry,ensure_ascii=False)+'\n');handle.flush()
                values=(row['source'],row['path'],destination,state,digest,error,*[metrics.get(f[2]) for f in fields],row['metrics'])
                writer.writerow([excel_safe(str(v if v is not None else '')) for v in values]);csv_file.flush()
            result=copy_results(Rows(),roots,out,stop,emit,retain_tree=False,observe=observe)
        result.update(manifest=str(manifest),metrics_csv=str(csv_path));return result
    finally:db.close()

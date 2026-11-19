"""Read-only paged result details, shared across utility and filtering tasks."""
import json,queue,threading,sqlite3
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageOps,ImageTk
from .storage import REASONS
from .tasks import TASK_NAMES
from .result_fields import FIELDS,columns_for,saved_order,cell_value,numeric_expression
from .debug_ui import DebugSelection
from .debug_views import CATEGORY_SQL
from .numbers import number,display_json

def fit_tree_columns(tree,limits=None):
    """Fit this page's displayed strings, using the actual Tk font at this DPI."""
    from tkinter import font
    style=ttk.Style(tree);body=font.Font(tree,font=style.lookup(tree.cget('style') or 'Treeview','font') or 'TkDefaultFont')
    heading=font.Font(tree,font=style.lookup('Treeview.Heading','font') or 'TkDefaultFont')
    from .dpi import window_dpi
    scale=window_dpi(tree)/96;pad=round(36*scale)
    signature=(tuple(sorted(body.actual().items())),tuple(sorted(heading.actual().items())),scale)
    if getattr(tree,'_measure_signature',None)!=signature:tree._measure_signature=signature;tree._measure_cache={}
    cache=tree._measure_cache
    if len(cache)>20000:cache.clear()
    def measure(f,text,kind):
        key=(kind,text)
        if key not in cache:cache[key]=f.measure(text)
        return cache[key]
    limits=limits or {};children=tree.get_children()
    for col in tree['columns']:
        text_column=col in ('name','path','category','format','state','reason','target','route','basis','parent','method','keep','sha','duplicate_of','container_note','file_size_rule')
        values=[str(tree.set(i,col)) for i in children]
        data_width=max([0,*[measure(body,value,'body') for value in values]])
        minimum=round(80*scale) if text_column else max(round(95*scale),data_width+pad,measure(body,'−0.000000','body')+pad)
        width=max(measure(heading,tree.heading(col,'text'),'heading'),data_width)+pad
        maximum=limits.get(col,520 if col in ('reason','target','state') else 360 if col in ('name','path','keep','parent') else None)
        if maximum:width=min(width,round(maximum*scale))
        tree.column(col,width=max(minimum,width),minwidth=minimum,stretch=False)

STATES={'selected':'待执行','moved':'已剪切','rejected':'留在原处','existing':'基准/输出已有（无需操作）','recycled':'已移入回收站','copied':'已生成副本','grouped':'已在正确组','queued':'未判定','error':'失败','review':'需复查'}

SORTS={'文件名':'source','照片分数':"CAST(json_extract(metrics,'$.photo_score') AS REAL)",'人物分数':"CAST(json_extract(metrics,'$.people_score') AS REAL)",
       '复核分数':"CAST(json_extract(metrics,'$.verification_score') AS REAL)",'创作分数':"CAST(json_extract(metrics,'$.creative_score') AS REAL)",
       '清晰度':"CAST(json_extract(metrics,'$.sharpness') AS REAL)",'块效应':"CAST(json_extract(metrics,'$.blockiness') AS REAL)"}
for key,(title,width,metric) in FIELDS.items():
    if metric:SORTS.setdefault(title,"json_extract(metrics,'$."+metric+"')")
for key in ('creative','creative_category_score','excluded_category_score'):
    SORTS[FIELDS[key][0]]=numeric_expression(FIELDS[key][2])[0]
SORTS.update({'文件 MB':'size','尺寸 px':"json_extract(metrics,'$.width')*json_extract(metrics,'$.height')"})
SORTS['类别 / 分组']=CATEGORY_SQL
from .result_status import STATUS_SQL,display_state
SORTS['状态']=STATUS_SQL;SORTS['判定说明']='reason'

def result_order(sort,direction):
    field=SORTS.get(sort,'source');direction='DESC' if direction=='降序' else 'ASC'
    return f'({field}) IS NULL ASC,{field} {direction},source ASC'

class ImageHover:
    """Decode on a worker; tkinter is used only from the UI thread."""
    def __init__(self,tree,path_for_id):
        self.tree=tree;self.path_for_id=path_for_id;self.current=None;self.tip=None;self.timer=None;self.events=queue.Queue();self.serial=0;self.active=False
        tree.bind('<Motion>',self.motion,add='+');tree.bind('<Leave>',lambda _:self.hide(),add='+')
        tree.bind('<Destroy>',lambda _:self.hide(),add='+')
        for event in ('<MouseWheel>','<ButtonPress-1>','<Configure>'):tree.bind(event,lambda _:self.hide(),add='+')
    def hide(self):
        self.serial+=1;self.current=None
        if self.timer is not None:
            self.tree.after_cancel(self.timer);self.timer=None
        if self.tip is not None:self.tip.destroy();self.tip=None
    def motion(self,event):
        column=self.tree.identify_column(event.x);display=list(self.tree['displaycolumns'])
        if display==['#all']:display=list(self.tree['columns'])
        index=int(column[1:])-1 if column else -1
        key=self.tree.identify_row(event.y) if 0<=index<len(display) and display[index] in ('name','path') else ''
        if key==self.current:return
        self.hide();self.current=key
        if key:self.timer=self.tree.after(400,lambda:self.load(key,display[index]))
    def load(self,key,column):
        self.timer=None
        if key!=self.current:return
        if self.active:
            self.timer=self.tree.after(100,lambda:self.load(key,column));return
        bbox=self.tree.bbox(key,column)
        if not bbox:return
        # Keep the entire thumbnail inside the filename column. A tooltip
        # following the cursor used to cover the adjacent numeric evidence.
        bx,by,bw,bh=bbox;left=max(0,bx)+4;right=min(self.tree.winfo_width(),bx+bw)-4
        preview_width=min(260,right-left)
        if preview_width<48:return
        self.active=True;serial=self.serial;path=self.path_for_id(key)
        def worker():
            try:
                from .search_model import load_search_image
                im,_,_=load_search_image(path);im.thumbnail((preview_width,220));self.events.put((serial,im,None))
            except Exception as exc:self.events.put((serial,None,str(exc)))
        threading.Thread(target=worker,daemon=True).start()
        def poll():
            if not self.tree.winfo_exists():return
            try:token,im,error=self.events.get_nowait()
            except queue.Empty:self.tree.after(50,poll);return
            self.active=False
            if token!=self.serial or error:
                if im:im.close()
                return
            self.tip=tk.Toplevel(self.tree);self.tip.overrideredirect(True)
            pic=ImageTk.PhotoImage(im);im.close();label=ttk.Label(self.tip,image=pic);label.image=pic;label.pack()
            self.tip.update_idletasks()
            x=self.tree.winfo_rootx()+left;y=self.tree.winfo_rooty()+by+bh+4
            if y+self.tip.winfo_reqheight()>self.tree.winfo_screenheight()-8:y=self.tree.winfo_rooty()+by-self.tip.winfo_reqheight()-4
            self.tip.geometry(f'+{max(0,x)}+{max(0,y)}')
        poll()

class ResultWindow(DebugSelection):
    PAGE=200
    def __init__(self,pane,result):
        self.pane=pane;self.result=result;self.offset=0;self.total=0;self.rows={};self.events=queue.Queue();self.generation=0
        self.database=Path(result['report']).parent/'state.sqlite3'
        self.update_config()
        self.refresh_at=0
        self.columns=columns_for(pane.task);self.drag=None;self.dragged=False
        self.states=dict(STATES)
        if result.get('comparison'):
            self.columns.insert(1,'threshold_change')
            self.states.update(selected='符合新条件',rejected='按新条件保留',queued='需补充证据')
        if pane.task=='hash' and result.get('hash_mode')=='inplace':
            self.states['rejected']='原位保留';self.states.pop('existing',None)
        elif pane.task=='hash' and (result.get('hash_mode')=='std' or not result.get('output_is_input',False)):
            self.states['rejected']='其他输入留在原处'
            self.states['existing']='基准已有（无需操作）' if result.get('hash_mode')=='std' else '输出已有（无需操作）'
        else:self.states.pop('existing',None)
        if pane.task=='hash':
            if getattr(self.config,'hash_mode','consolidate') in ('std','inplace'):self.columns=[k for k in self.columns if k not in ('in_output','requested')]
            if getattr(self.config,'hash_mode','consolidate')!='std':self.columns.remove('is_std')
            if getattr(self.config,'hash_mode','consolidate')!='inplace':self.columns.remove('priority_rank')
        self.window=tk.Toplevel(pane.app.root);self.window.title(TASK_NAMES[pane.task]+' · '+('门槛对比明细' if result.get('comparison') else '预览明细' if result['mode']=='preview' else '执行明细'))
        from .ui_scroll import cancel_callbacks_on_destroy
        cancel_callbacks_on_destroy(self.window)
        s=pane.app.dpi/96
        from .dpi import initial_geometry
        self.window.geometry(initial_geometry(self.window,pane.app.dpi,details=True))
        frame=ttk.Frame(self.window,padding=18);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=TASK_NAMES[pane.task]+' · 结果明细',font='SubtitleFont').pack(anchor='w')
        hint=('类别列显示各原目录的目标分组。' if pane.task=='group' else '悬停文件名看缩略图，双击放大。')+' 拖动表头换序，单击排序；调试选图可组合条件并复制样本。'
        from .ui_text import responsive_label
        responsive_label(ttk.Label(frame,text=hint,style='Muted.TLabel')).pack(fill='x',pady=(8,12))
        view_bar=ttk.Frame(frame);view_bar.pack(fill='x',pady=(0,8))
        self.display_mode=tk.StringVar(self.window,value='table')
        for text,value in [('表格明细','table'),('缩略图复核','gallery')]:
            ttk.Radiobutton(view_bar,text=text,value=value,variable=self.display_mode,command=self.switch_display).pack(side='left',padx=(0,12))
        ttk.Label(view_bar,text='缩略图显示图像项；状态、分组和排序共用。',style='Muted.TLabel').pack(side='left')
        if not result.get('comparison'):
            ttk.Button(view_bar,text='文件去向',command=pane.show_plan).pack(side='right')
        self.view_bar=view_bar
        self.change=tk.StringVar(self.window,value='全部变化')
        if result.get('comparison'):
            from .threshold_preview import CHANGES
            ttk.Label(view_bar,text='对比范围').pack(side='left',padx=(12,4))
            box=ttk.Combobox(view_bar,textvariable=self.change,values=['全部变化',*CHANGES],state='readonly',width=12)
            box.pack(side='left');box.bind('<<ComboboxSelected>>',self.reset)
            c=result.get('comparison_counts',{})
            responsive_label(ttk.Label(frame,text=f"筛选资格对比 · 新增符合 {c.get('新增符合',0):,} · 不再符合 {c.get('不再符合',0):,} · 待补算 {c.get('待补算',0):,}；应用条件后，完整预览会核对重复副本与文件去向。",style='Muted.TLabel')).pack(fill='x',pady=(0,8))
        controls=ttk.Frame(frame);controls.pack(fill='x')
        self.state=tk.StringVar(self.window,value='全部');self.category=tk.StringVar(self.window,value='全部分组')
        ttk.Label(controls,text='状态').pack(side='left')
        box=ttk.Combobox(controls,textvariable=self.state,values=['全部',*self.states.values()],state='readonly',width=23);box.pack(side='left',padx=8);box.bind('<<ComboboxSelected>>',self.reset);self.state_box=box
        if 'category' in self.columns:
            self.groups=ttk.Combobox(controls,textvariable=self.category,values=['全部分组'],state='readonly',width=28);self.groups.pack(side='left');self.groups.bind('<<ComboboxSelected>>',self.reset)
        self.sort=tk.StringVar(self.window,value='文件名');self.direction=tk.StringVar(self.window,value='升序')
        available=[FIELDS[k][0] for k in self.columns if FIELDS[k][0] in SORTS]
        for variable,values,width in [(self.sort,available,15),(self.direction,['升序','降序'],7)]:
            box=ttk.Combobox(controls,textvariable=variable,values=values,state='readonly',width=width);box.pack(side='left',padx=6);box.bind('<<ComboboxSelected>>',self.reset)
        ttk.Button(controls,text='全部指标',command=self.all_metrics).pack(side='left',padx=6)
        ttk.Button(controls,text='恢复列顺序',command=self.reset_columns).pack(side='right')
        find=ttk.Frame(frame);find.pack(fill='x',pady=(6,0))
        ttk.Label(find,text='文件名查找').pack(side='left')
        self.find_text=tk.StringVar(self.window);self.find_source=None
        entry=ttk.Entry(find,textvariable=self.find_text,width=42);entry.pack(side='left',padx=8);entry.bind('<Return>',lambda _:self.find_filename())
        ttk.Button(find,text='查找下一项',command=self.find_filename).pack(side='left')
        ttk.Label(find,text='支持部分文件名；在当前视图跨页定位。',style='Muted.TLabel').pack(side='left',padx=8)
        self.auto_width=tk.BooleanVar(self.window,value=True)
        ttk.Checkbutton(find,text='自动列宽',variable=self.auto_width,command=self.fit_columns).pack(side='right')
        ttk.Button(find,text='适合内容',command=self.fit_columns).pack(side='right',padx=6)
        area=ttk.Frame(frame);area.pack(fill='both',expand=True,pady=12);self.area=area
        from tkinter import font
        ttk.Style(self.window).configure('Result.Treeview',rowheight=max(round(30*s),font.nametofont('BodyFont').metrics('linespace')+round(14*s)),font='BodyFont')
        self.tree=ttk.Treeview(area,columns=self.columns,displaycolumns=saved_order(self.columns,getattr(pane,'result_columns',[]),pane.task),show='headings',selectmode='browse',height=4,style='Result.Treeview')
        for col in self.columns:
            title,width,_=FIELDS[col]
            self.tree.heading(col,text=title,command=lambda key=title:self.sort_column(key));self.tree.column(col,width=round(width*s),minwidth=round(95*s),stretch=False,anchor='w' if col in ('name','category','format','state','reason','target','route','basis','parent','method') else 'center')
        self.tree.bind('<ButtonPress-1>',self.heading_press,add='+');self.tree.bind('<B1-Motion>',self.heading_motion,add='+');self.tree.bind('<ButtonRelease-1>',self.heading_release,add='+')
        self.tree.grid(row=0,column=0,sticky='nsew');area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1)
        from .ui_scroll import AutoScrollbar
        ys=AutoScrollbar(area,command=self.tree.yview);ys.grid(row=0,column=1,sticky='ns');xs=AutoScrollbar(area,orient='horizontal',command=self.tree.xview);xs.grid(row=1,column=0,sticky='ew');self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.table_scrollbars=(ys,xs)
        from .result_gallery import ThumbnailGrid
        self.gallery=ThumbnailGrid(area,self.gallery_select,self.open_row,s)
        self.tree.tag_configure('selected',background='#eee9fc');self.tree.tag_configure('error',background='#ffe6e5');self.tree.tag_configure('review',background='#fff0d5')
        self.tree.tag_configure('found',background='#fff4cc')
        self.tree.bind('<<TreeviewSelect>>',self.details);self.tree.bind('<Double-1>',self.enlarge)
        self.hover=ImageHover(self.tree,self.image_path)
        self.detail=tk.StringVar(self.window);self.detail_label=responsive_label(ttk.Label(frame,textvariable=self.detail,style='Muted.TLabel'));self.detail_label.pack(fill='x')
        footer=ttk.Frame(frame);footer.pack(fill='x',pady=(12,0));self.status=tk.StringVar(self.window,value='读取明细…');ttk.Label(footer,textvariable=self.status).pack(side='left')
        for title,fn in [('上一页',lambda:self.page(-1)),('下一页',lambda:self.page(1)),('关闭',self.window.destroy)]:ttk.Button(footer,text=title,command=fn).pack(side='right',padx=4)
        ttk.Button(footer,text='刷新明细',command=self.refresh).pack(side='right',padx=4)
        self.setup_debug(frame,controls)
        self.tree.bind('<<TreeviewSelect>>',lambda _:self.gallery.highlight(self.tree.selection()),add='+')
        # Reserve the footer/detail space before giving the remaining height
        # to the table. Large debug controls must not push navigation offscreen.
        self.footer=footer;area.pack_forget();self.detail_label.pack_configure(side='bottom')
        footer.pack_configure(side='bottom',before=self.detail_label)
        area.pack(fill='both',expand=True,pady=12)
        self.refresh();self.window.after(60,self.poll)
    def update_config(self):
        from .config import Config
        from .tasks import TaskConfig
        raw=self.result.get('config')
        if not raw:
            try:
                with sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True) as db:
                    row=db.execute('SELECT config FROM runs WHERE id=?',(self.result['run_id'],)).fetchone()
                    raw=json.loads(row[0]) if row else {}
            except (OSError,ValueError,sqlite3.Error):raw={}
        cls=Config if self.pane.photo_task else TaskConfig
        raw={**({'scheme':self.pane.task} if self.pane.photo_task else {'task':self.pane.task}),**raw}
        self.config=cls(**{k:v for k,v in raw.items() if k in cls.__dataclass_fields__})
    def reset(self,event=None):
        if getattr(self,'view_database',None) and event is not None and event.widget in [getattr(self,'groups',None),getattr(self,'state_box',None)]:self.queue_feedback();return
        self.offset=0
        self.refresh()
    def query_scope(self):
        clause='seen_run=?';args=[self.result['run_id']]
        if not self.view_database:
            if self.state.get()!='全部':clause+=' AND ('+STATUS_SQL+')=?';args.append(next(k for k,v in self.states.items() if v==self.state.get()))
            if self.category.get()!='全部分组':clause+=' AND '+CATEGORY_SQL+'=?';args.append(self.category.get())
        if self.result.get('comparison') and self.change.get()!='全部变化':
            clause+=" AND json_extract(metrics,'$.threshold_change')=?";args.append(self.change.get())
        if self.display_mode.get()=='gallery':
            from .debug_tools import image_clause
            clause+=' AND '+image_clause()
        return clause,args
    def find_filename(self):
        term=self.find_text.get().strip()
        if not term:return
        self._find_token=getattr(self,'_find_token',0)+1
        if getattr(self,'_find_busy',False):self._find_again=True;return
        self._find_busy=True;self._find_again=False;token=self._find_token
        database=self.view_database or self.database;clause,args=self.query_scope();order=result_order(self.sort.get(),self.direction.get());previous=self.find_source
        signature=(str(database),clause,tuple(args),order,term)
        self.status.set('正在当前视图中查找文件名…')
        def work():
            try:
                with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
                    # basename is local and read-only; do not match directory names.
                    db.create_function('basename',1,lambda p:Path(p).name.casefold())
                    matches=db.execute('SELECT source,pos FROM (SELECT source,ROW_NUMBER() OVER (ORDER BY '+order+')-1 pos FROM files WHERE '+clause+') WHERE instr(basename(source),?)>0 ORDER BY pos',(*args,term.casefold())).fetchall()
                meta={'count':len(matches),'token':token,'signature':signature}
                if not matches:self.view_events.put(('find',None,meta,None));return
                index=next((i+1 for i,r in enumerate(matches) if r[0]==previous),0)%len(matches)
                self.view_events.put(('find',matches[index],meta,None))
            except Exception as exc:self.view_events.put(('find',None,{'token':token,'signature':signature},str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def sort_column(self,key):
        if self.dragged:return
        if key not in SORTS:return
        self.direction.set('降序' if self.sort.get()==key and self.direction.get()=='升序' else '升序');self.sort.set(key);self.reset()
    def page(self,direction):
        target=self.offset+direction*self.PAGE
        if 0<=target<self.total:self.offset=target;self.refresh()
    def refresh(self):
        if getattr(self,'_refresh_busy',False):
            self.generation+=1;self._refresh_again=True;return
        self._refresh_busy=True;self._refresh_again=False
        self.generation+=1;generation=self.generation;offset=self.offset;order=result_order(self.sort.get(),self.direction.get());database=self.view_database or self.database;clause,args=self.query_scope()
        def work():
            try:
                db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
                try:
                    total=db.execute('SELECT COUNT(*) FROM files WHERE '+clause,args).fetchone()[0]
                    rows=[dict(r) for r in db.execute('SELECT * FROM files WHERE '+clause+' ORDER BY '+order+' LIMIT ? OFFSET ?',(*args,self.PAGE,offset))]
                    groups=[r[0] for r in db.execute('SELECT DISTINCT '+CATEGORY_SQL+' FROM files WHERE seen_run=?',(self.result['run_id'],)) if r[0]] if 'category' in self.columns else []
                finally:db.close()
                self.events.put((generation,rows,total,groups,None))
            except Exception as exc:self.events.put((generation,[],0,[],str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        if not self.window.winfo_exists():return
        try:
            while True:
                token,rows,total,groups,error=self.events.get_nowait()
                self._refresh_busy=False
                if token!=self.generation:
                    if self._refresh_again:self.refresh()
                    continue
                self.updating_selection=True
                self.hover.hide();self.tree.delete(*self.tree.get_children());self.rows={};self.total=total
                if hasattr(self,'groups') and not self.view_database:self.groups.configure(values=['全部分组',*sorted(groups)])
                for n,r in enumerate(rows):
                    m=json.loads(r['metrics']);r['parsed']=m;key=str(n);self.rows[key]=r
                    if self.pane.task=='creative' and m.get('category_scores') and not self.result.get('comparison'):
                        from .decisions import apply_scores
                        apply_scores(m['category_scores'],self.view_config or self.config,m)
                    target=r['destination'] or self.planned_target(r,m)
                    r['display_target']=target
                    from .decisions import explain_decision
                    reason=explain_decision(r['reason'],m,self.view_config or self.config) if self.pane.photo_task else REASONS.get(r['reason'],r['reason'])
                    if self.result.get('comparison'):reason=m.get('threshold_new_note',reason)
                    if r['reason']=='selected':reason={'group':'按命名规律归位','flatten':'扁平移出','compress':'满足压缩条件，生成 JPG 副本','repair':'发现兼容修复候选；生成副本，原件保留'}.get(self.pane.task,reason)
                    values={'state':self.states.get(display_state(r,m),r['state']),'reason':reason,'target':target}
                    self.tree.insert('','end',iid=key,values=[values[col] if col in values else cell_value(col,r,m) for col in self.columns],tags=(r['state'],))
                self.restore_selection()
                for key,row in self.rows.items():
                    if row['source']==self.find_source:self.tree.see(key);self.tree.focus(key);self.tree.item(key,tags=(*self.tree.item(key,'tags'),'found'))
                self.updating_selection=False
                if self.display_mode.get()=='gallery':self.render_gallery()
                if self.auto_width.get():self.fit_columns()
                self.status.set(error or ('已应用筛选 · ' if self.view_database else '')+f'共 {number(total,group=True)} 项 · 当前 {self.offset+1 if rows else 0}–{number(self.offset+len(rows))}'+(' · 预览中，每 3 秒更新已完成部分' if not self.view_database and self.pane.running and self.pane.pending_mode=='preview' else ''))
        except queue.Empty:pass
        self.poll_debug()
        import time
        if not self.result.get('comparison') and not self.view_database and self.pane.running and self.pane.pending_mode=='preview' and not getattr(self,'_refresh_busy',False) and time.monotonic()-self.refresh_at>=3:
            self.refresh_at=time.monotonic();self.refresh()
        self.window.after(60,self.poll)
    def image_path(self,key):
        r=self.rows.get(key,{})
        if r.get('state')=='review' and r.get('parsed',{}).get('retained_path'):return r['parsed']['retained_path']
        return (r.get('destination') or r.get('source')) if r.get('state') in ('moved','copied','grouped','recycled') else r.get('source')
    def switch_display(self):
        gallery=self.display_mode.get()=='gallery'
        self.PAGE=self.gallery.PAGE if gallery else 200
        self.offset=self.offset//self.PAGE*self.PAGE
        self.hover.hide()
        if gallery:
            self.tree.grid_remove()
            for scroll in self.table_scrollbars:scroll.grid_remove()
            self.gallery.frame.grid(row=0,column=0,columnspan=2,sticky='nsew')
        else:
            self.gallery.frame.grid_remove();self.tree.grid()
            # Scrollbars manage their own visibility after Treeview reports
            # its new range; explicitly request that calculation on return.
            self.tree.yview_moveto(self.tree.yview()[0]);self.tree.xview_moveto(self.tree.xview()[0])
        self.refresh()
    def render_gallery(self):
        from .decisions import explain_decision
        entries=[]
        score_key={'photo':'photo','people':'people','meme':'meme','creative':'creative'}.get(self.pane.task)
        for key,row in self.rows.items():
            m=row['parsed'];evidence=self.states.get(display_state(row,m),row['state'])
            if score_key:evidence+=' · '+FIELDS[score_key][0]+' '+cell_value(score_key,row,m)
            if m.get('threshold_change'):evidence=m['threshold_change']+' · '+evidence
            reason=explain_decision(row['reason'],m,self.view_config or self.config) if self.pane.photo_task else REASONS.get(row['reason'],row['reason'])
            entries.append({'key':key,'path':self.image_path(key),'expected':(row['size'],row['mtime']) if row['state'] not in ('moved','copied','grouped','recycled','review') else None,
                            'name':Path(row['source']).name,'evidence':evidence,'reason':reason[:55]})
        self.gallery.render(entries,self.tree.selection())
    def gallery_select(self,key,event):
        if key not in self.rows:return
        selected=set(self.tree.selection())
        if self.debug_mode.get() and event.state&1 and getattr(self,'gallery_anchor',None) in self.rows:
            keys=list(self.rows);a,b=sorted((keys.index(self.gallery_anchor),keys.index(key)))
            selected.update(keys[a:b+1])
        elif self.debug_mode.get() and event.state&4:
            if key in selected:selected.remove(key)
            else:selected.add(key)
        else:selected={key}
        self.gallery_anchor=key;self.tree.selection_set(list(selected));self.tree.focus(key)
    def open_row(self,key):
        path=self.image_path(key)
        if path:
            from .result_gallery import open_preview
            row=self.rows[key]
            expected=(row['size'],row['mtime']) if row['state'] not in ('moved','copied','grouped','recycled','review') else None
            open_preview(self.window,path,expected)
    def planned_target(self,row,metrics):
        if self.result.get('comparison') and row['state']=='selected':return '资格对比；应用条件后在完整预览中核对文件去向'
        from .planning import planned_target
        return planned_target(row,metrics,self.pane.task,self.view_config or self.config,self.result)
    def details(self,event=None):
        selected=self.tree.selection()
        if not selected:return
        r=self.rows[selected[0]];m=r['parsed']
        values=[f"{number(m['width'])} × {number(m['height'])} px"] if 'width' in m and 'height' in m else []
        for column in self.columns:
            title,_,key=FIELDS[column]
            if key in m:values.append(f'{title}：{number(m[key]) if isinstance(m[key],(int,float)) else m[key]}')
        self.detail.set('源：'+r['source']+'\n目标：'+(r.get('display_target') or '留在原处 / 尚未形成计划')+'\n'+' · '.join(values))
        if self.result.get('comparison'):
            self.detail.set(self.detail.get()+'\n原条件：'+m.get('threshold_old_note','')+'\n新条件：'+m.get('threshold_new_note',''))
    def all_metrics(self):
        selected=self.tree.selection()
        if not selected:self.status.set('先选择一行，即可查看全部指标。');return
        row=self.rows[selected[0]];win=tk.Toplevel(self.window);win.title(Path(row['source']).name+' · 全部指标');win.geometry('800x600')
        text=tk.Text(win,wrap='char',font='BodyFont',padx=16,pady=16)
        scroll=ttk.Scrollbar(win,command=text.yview);scroll.pack(side='right',fill='y');text.pack(fill='both',expand=True);text.configure(yscrollcommand=scroll.set)
        text.insert('1.0',display_json({'source':row['source'],'size':row['size'],'metrics':row['parsed']}));text.configure(state='disabled')
    def fit_columns(self):
        if hasattr(self,'tree'):fit_tree_columns(self.tree)
    def heading_press(self,event):
        self.dragged=False;self.drag=None
        if self.tree.identify_region(event.x,event.y)=='separator':self.auto_width.set(False)
        if self.tree.identify_region(event.x,event.y)=='heading':self.drag=(self.tree.identify_column(event.x),event.x)
    def heading_motion(self,event):
        if self.drag and abs(event.x-self.drag[1])>=8:self.dragged=True;self.tree.configure(cursor='fleur')
    def heading_release(self,event):
        if self.drag and self.dragged and self.tree.identify_region(event.x,event.y)=='heading':
            order=list(self.tree['displaycolumns']);a=int(self.drag[0][1:])-1;b=int(self.tree.identify_column(event.x)[1:])-1
            if 0<=a<len(order) and 0<=b<len(order):
                order.insert(b,order.pop(a));self.tree.configure(displaycolumns=order);self.pane.result_columns=order
        self.tree.configure(cursor='');self.drag=None
        self.window.after_idle(lambda:setattr(self,'dragged',False))
    def reset_columns(self):self.tree.configure(displaycolumns=self.columns);self.pane.result_columns=list(self.columns)
    def enlarge(self,event=None):
        if event is not None and self.tree.identify_region(event.x,event.y)!='cell':return
        selected=self.tree.selection()
        if not selected:return
        self.open_row(selected[0])

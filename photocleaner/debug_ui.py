"""Optional debug controls on the existing, paged result viewer."""
from .numbers import number
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import threading,queue
from pathlib import Path
from .debug_tools import numeric_fields,matching_sources,combine_selection,OPS,export_selection,image_row
from .debug_view_ui import ViewControls

class DebugSelection(ViewControls):
    def setup_debug(self,parent,controls):
        self.selected_sources=set();self.selection_history=[];self.updating_selection=False;self.export_pending=False
        self.debug_events=queue.Queue();self.debug_mode=tk.BooleanVar(self.window,value=False);self.condition_busy=False
        ttk.Checkbutton(controls,text='调试选图',variable=self.debug_mode,command=self.toggle_debug).pack(side='left',padx=6)
        if self.pane.photo_task and not self.result.get('comparison'):ttk.Button(self.view_bar,text='门槛影响',command=lambda:self.when_idle(self.threshold_dialog)).pack(side='right',padx=6)
        # Bound the editor height so histogram/readout improvements cannot
        # squeeze the result table out of a restored/small window.
        self.debug_host=ttk.Frame(parent)
        self.debug_canvas=tk.Canvas(self.debug_host,highlightthickness=0,height=220)
        from .ui_scroll import AutoScrollbar
        scroll=AutoScrollbar(self.debug_host,command=self.debug_canvas.yview);scroll.pack(side='right',fill='y')
        self.debug_canvas.configure(yscrollcommand=scroll.set);self.debug_canvas.pack(side='left',fill='both',expand=True)
        self.debug_frame=ttk.Frame(self.debug_canvas)
        self.debug_item=self.debug_canvas.create_window((0,0),window=self.debug_frame,anchor='nw')
        def fit_debug(event=None):
            self.debug_canvas.itemconfigure(self.debug_item,width=max(1,self.debug_canvas.winfo_width()))
            self.debug_canvas.configure(scrollregion=self.debug_canvas.bbox('all'),height=min(self.debug_frame.winfo_reqheight(),max(170,round(self.window.winfo_height()*.34))))
        self.debug_canvas.bind('<Configure>',fit_debug);self.debug_frame.bind('<Configure>',fit_debug)
        self.window.bind('<Configure>',lambda e:fit_debug() if e.widget is self.window else None,add='+')
        self.setup_views(self.debug_frame)
        self.selection_text=tk.StringVar(self.window,value='已选 0 张；Ctrl 多选，Shift 连选，可跨页保留。')
        row=ttk.Frame(self.debug_frame)
        metrics=list(numeric_fields(self.pane.task));preferred,reference={'photo':('清晰度','80'),'people':('人物分数','.97'),'creative':('创作总分','.85'),'meme':('语义表情包分数','.8')}.get(self.pane.task,(metrics[0] if metrics else '','400'))
        self.metric=tk.StringVar(self.window,value=preferred)
        self.operator=tk.StringVar(self.window,value='>');self.compare_value=tk.StringVar(self.window,value=reference);self.operation=tk.StringVar(self.window,value='替换')
        for var,values,width in [(self.metric,metrics,17),(self.operator,list(OPS),4)]:ttk.Combobox(row,textvariable=var,values=values,width=width,state='readonly').pack(side='left',padx=(0,6))
        ttk.Entry(row,textvariable=self.compare_value,width=9).pack(side='left',padx=(0,6))
        ttk.Combobox(row,textvariable=self.operation,values=['替换','追加','交集','移除'],state='readonly',width=6).pack(side='left',padx=(0,6))
        self.condition_btn=ttk.Button(row,text='按条件勾选',command=self.select_condition);self.condition_btn.pack(side='left',padx=(0,6))
        toolbar=ttk.Frame(self.debug_frame);toolbar.pack(fill='x',pady=4)
        ttk.Button(toolbar,text='勾选整个视图',command=self.select_view).pack(side='left',padx=(0,6))
        ttk.Button(toolbar,text='本页全选',command=self.select_page).pack(side='left',padx=(0,6))
        ttk.Button(toolbar,text='清空选中',command=self.clear_selection).pack(side='left')
        from .ui_text import responsive_label
        helper=responsive_label(ttk.Label(self.debug_frame,text='按条件勾选只改变高亮集合；上方条件树和“应用筛选”改变完整表格范围。复制只复制勾选图像。',style='Muted.TLabel'))
        def toggle_conditions():
            if row.winfo_manager():row.pack_forget();helper.pack_forget();more.configure(text='更多勾选条件')
            else:row.pack(fill='x',before=toolbar);helper.pack(fill='x',before=toolbar,pady=(4,0));more.configure(text='收起勾选条件')
        more=ttk.Button(toolbar,text='更多勾选条件',command=toggle_conditions);more.pack(side='left',padx=6)
        self.toggle_conditions=toggle_conditions
        ttk.Label(toolbar,textvariable=self.selection_text,style='Muted.TLabel').pack(side='left',padx=8)
        out=ttk.Frame(self.debug_frame);out.pack(fill='x')
        self.test_output=tk.StringVar(self.window,value=self.pane.debug_output)
        ttk.Label(out,text='测试输出').pack(side='left',padx=(0,8))
        entry=ttk.Entry(out,textvariable=self.test_output);entry.pack(side='left',expand=True,fill='x')
        from .directory_lists import path_entry
        path_entry(entry,self.test_output)
        def browse():
            path=filedialog.askdirectory(parent=self.window,title='选择测试输出，须在输入和原输出之外')
            if path:self.test_output.set(path)
        ttk.Button(out,text='选择',command=browse).pack(side='left',padx=6)
        self.copy_debug_btn=ttk.Button(out,text='复制选中及指标',command=self.copy_selected);self.copy_debug_btn.pack(side='left')
        self.tree.bind('<<TreeviewSelect>>',self.record_selection,add='+')
        self.tree.bind('<Control-a>',lambda e:self.select_page())
        self.cell_drag=None
        self.tree.bind('<ButtonPress-1>',self.range_start,add='+');self.tree.bind('<B1-Motion>',self.range_motion,add='+')
        self.tree.bind('<ButtonRelease-1>',lambda e:setattr(self,'cell_drag',None),add='+')
        def wheel(event):
            first,last=self.debug_canvas.yview()
            if last-first<.999:self.debug_canvas.yview_scroll(-int(event.delta/120),'units')
            return 'break'
        def bind_scroll(widget):
            widget.bind('<MouseWheel>',wheel,add='+')
            for child in widget.winfo_children():bind_scroll(child)
        bind_scroll(self.debug_host)

    def range_start(self,event):
        self.cell_drag=None
        if self.debug_mode.get() and self.tree.identify_region(event.x,event.y)=='cell':
            key=self.tree.identify_row(event.y)
            if key:self.cell_drag=(key,self.tree.selection() if event.state&4 else ())
    def range_motion(self,event):
        if not self.cell_drag or not self.debug_mode.get():return
        target=self.tree.identify_row(event.y);children=list(self.tree.get_children())
        if target not in children or self.cell_drag[0] not in children:return
        a,b=sorted((children.index(self.cell_drag[0]),children.index(target)))
        self.tree.selection_set(list(dict.fromkeys([*self.cell_drag[1],*children[a:b+1]])))
        return 'break'

    def toggle_debug(self):
        active=self.debug_mode.get();self.tree.configure(selectmode='extended' if active else 'browse')
        if active:self.debug_host.pack(fill='x',before=self.area,pady=(8,0))
        else:self.debug_host.pack_forget()

    def record_selection(self,event=None):
        if self.updating_selection or not self.debug_mode.get():return
        visible={r['source'] for r in self.rows.values()}
        valid=[k for k in self.tree.selection() if k in self.rows and image_row(self.rows[k])]
        invalid=[k for k in self.tree.selection() if k not in valid]
        if invalid:
            self.updating_selection=True
            try:self.tree.selection_remove(invalid)
            finally:self.updating_selection=False
            self.status.set('调试集合只接受已判定图像；未判定项或非图像未加入。')
        chosen={self.rows[k]['source'] for k in valid}
        self.selected_sources.difference_update(visible);self.selected_sources.update(chosen);self.selection_count()

    def selection_count(self):self.selection_text.set(f'已选 {number(len(self.selected_sources),group=True)} 张；Ctrl 多选，Shift 连选，跨页保留。')
    def restore_selection(self):
        self.updating_selection=True
        try:self.tree.selection_set([k for k,r in self.rows.items() if r['source'] in self.selected_sources])
        finally:self.updating_selection=False
        self.selection_count()
    def select_page(self):
        if not self.debug_mode.get():return
        self.selected_sources.update(r['source'] for r in self.rows.values() if image_row(r))
        self.restore_selection();return 'break'
    def clear_selection(self):self.selected_sources.clear();self.selection_history.clear();self.restore_selection()
    def select_view(self):
        import sqlite3
        db=self.view_database or self.database
        with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as connection:
            from .debug_tools import image_clause
            clause,args=self.query_scope()
            self.selected_sources.update(r[0] for r in connection.execute("SELECT source FROM files WHERE "+clause+" AND state<>'queued' AND "+image_clause(),args))
        self.restore_selection()
    def select_condition(self):
        if self.condition_busy:return
        self.condition_busy=True;self.condition_btn.configure(state='disabled');self.copy_debug_btn.configure(state='disabled')
        STATES=self.states
        title,op,value,operation=self.metric.get(),self.operator.get(),self.compare_value.get(),self.operation.get()
        state=next((k for k,v in STATES.items() if v==self.state.get()),None)
        category=self.category.get() if self.category.get()!='全部分组' else None
        database=self.view_database or self.database
        if self.view_database:state=category=None
        def work():
            try:
                matched=matching_sources(database,self.result['run_id'],self.pane.task,title,op,value,state,category)
                self.debug_events.put((matched,operation,{'metric':title,'op':op,'value':value,'operation':operation,'state':state,'category':category},None))
            except Exception as exc:self.debug_events.put((set(),operation,{},str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def poll_debug(self):
        self.poll_views()
        try:
            while True:
                matched,operation,history,error=self.debug_events.get_nowait()
                self.condition_busy=False;self.condition_btn.configure(state='normal')
                if not self.export_pending:self.copy_debug_btn.configure(state='normal')
                if error:self.status.set(error);continue
                self.selected_sources=combine_selection(self.selected_sources,matched,operation);self.selection_history.append(history)
                self.restore_selection();self.status.set(f'本次条件匹配 {number(len(matched),group=True)} 张；当前选中 {number(len(self.selected_sources),group=True)} 张')
        except queue.Empty:pass

    def when_idle(self,callback):
        if self.pane.running:
            if self.pane.pending_mode!='preview':messagebox.showinfo('请先暂停执行','文件执行期间请先安全暂停，再调整阈值或复制测试图像。',parent=self.window);return
            self.pane.pause();self.status.set('等待预览安全暂停；已完成证据会保留。')
            def wait():
                if not self.window.winfo_exists():self.export_pending=False;return
                if self.pane.running:self.window.after(100,wait)
                else:callback()
            wait()
        else:callback()

    def copy_selected(self):
        if self.export_pending or self.condition_busy or not self.selected_sources:return
        if not self.test_output.get().strip():messagebox.showinfo('选择测试输出','请先选择测试输出文件夹。',parent=self.window);return
        if self.pane.running and self.pane.pending_mode!='preview':self.when_idle(lambda:None);return
        selected=set(self.selected_sources);history=list(self.selection_history);output=self.test_output.get()
        self.export_pending=True;self.copy_debug_btn.configure(state='disabled')
        def start():
            pane=self.pane
            try:
                from .engine import normalize_inputs
                roots=normalize_inputs(self.result.get('roots') or pane.input_list.get(0,'end'))
                from .directory_lists import clean_path
                out=Path(clean_path(output)).expanduser().absolute();pane.app.check_conflicts(pane,roots,out,'copy')
            except (OSError,ValueError) as exc:self.export_finished({'error':str(exc)});return
            pane.debug_output=output;pane.debug_previous=(pane.pending_mode,pane.active_output)
            pane.pending_mode='debug_copy';pane.active_roots=roots;pane.active_output=out;pane.stop.clear();pane.set_running(True)
            def work():
                try:
                    result=export_selection(self.view_database or self.database,self.result['run_id'],selected,output,pane.stop,lambda e:pane.app.post(pane.task,e),history)
                    pane.app.post(pane.task,{'kind':'debug_copy_done',**result})
                except Exception as exc:pane.app.post(pane.task,{'kind':'debug_copy_done','error':str(exc)})
            pane.worker=threading.Thread(target=work,daemon=False);pane.worker.start()
        self.when_idle(start)
    def export_finished(self,event):
        self.export_pending=False
        if not self.window.winfo_exists():return
        self.copy_debug_btn.configure(state='normal')
        text=event.get('error') or f"测试复制{'已暂停' if event.get('stopped') else '完成'}：新增 {number(event.get('copied',0))}，已有相同副本 {number(event.get('skipped',0))}，失败 {number(event.get('errors',0))}。CSV/JSONL 已保存到测试输出。"
        self.status.set(text[:120]);self.pane.status.set(text)

    def threshold_dialog(self):
        if self.pane.running:return
        existing=getattr(self,'threshold_window',None)
        if existing and existing.window.winfo_exists():existing.window.lift();return
        from .threshold_ui import ThresholdWindow
        self.threshold_window=ThresholdWindow(self)

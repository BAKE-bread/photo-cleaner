"""General condition tree and explicit draft/application/history workflow."""
import copy,queue,threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from .debug_tools import numeric_fields,OPS
from .debug_views import create_view,distribution,prospective_change
from .numbers import number,metric_input

OPERATIONS={'重新筛选全部':'replace','在当前结果中继续筛':'intersect','并入匹配结果':'union','从当前结果排除':'subtract','只保留不重叠结果':'xor'}
GROUPS={'all':'全部满足','any':'任一满足','not':'排除任一匹配'}

class ViewControls:
    def setup_views(self,parent):
        self.view_database=None;self.view_config=None;self.applied_draft=None;self.view_number=0;self.view_events=queue.Queue();self.feedback_token=0;self.feedback_timer=None
        self.history=[(None,None,None,0)];self.history_position=0;self.rule_nodes={};self.last_distribution=None;self.draft_previous=None;self.reuse_origin=False
        row=ttk.Frame(parent);row.pack(fill='x',pady=2)
        self.rule_metric=tk.StringVar(self.window,value=next(iter(numeric_fields(self.pane.task))))
        self.rule_operator=tk.StringVar(self.window,value='≥');self.rule_value=tk.StringVar(self.window,value='0')
        ttk.Combobox(row,textvariable=self.rule_metric,values=[*numeric_fields(self.pane.task),'文件名','类别 / 分组','判定代码'],state='readonly',width=19).pack(side='left')
        self.rule_operator_box=ttk.Combobox(row,textvariable=self.rule_operator,values=[*OPS,'缺失','有值'],state='readonly',width=6);self.rule_operator_box.pack(side='left',padx=5)
        ttk.Entry(row,textvariable=self.rule_value,width=10).pack(side='left')
        ttk.Button(row,text='加入条件',command=self.add_condition).pack(side='left',padx=5)
        ttk.Button(row,text='更新所选',command=self.update_condition).pack(side='left',padx=2)
        for title,kind in [('全部组','all'),('任一组','any'),('排除组','not')]:ttk.Button(row,text=title,command=lambda k=kind:self.add_group(k)).pack(side='left',padx=2)
        ttk.Button(row,text='移除',command=self.remove_rule).pack(side='left',padx=2)
        ttk.Button(row,text='上移',command=lambda:self.move_rule(-1)).pack(side='left',padx=2)
        ttk.Button(row,text='下移',command=lambda:self.move_rule(1)).pack(side='left',padx=2)
        middle=ttk.Frame(parent);middle.pack(fill='x',pady=4);middle.columnconfigure(0,weight=3);middle.columnconfigure(1,weight=2)
        self.rule_tree=ttk.Treeview(middle,show='tree',height=3,selectmode='browse');self.rule_tree.grid(row=0,column=0,sticky='nsew',padx=(0,8))
        scroll=ttk.Scrollbar(middle,command=self.rule_tree.yview);scroll.grid(row=0,column=0,sticky='nse');self.rule_tree.configure(yscrollcommand=scroll.set)
        self.root_rule=self.rule_tree.insert('','end',text='全部满足（空组表示不限制）',open=True);self.rule_nodes[self.root_rule]={'kind':'all'};self.rule_tree.selection_set(self.root_rule)
        feedback=ttk.LabelFrame(middle,text='指标分布 · 当前已应用结果',padding=(8,5));feedback.grid(row=0,column=1,sticky='nsew')
        self.feedback=tk.StringVar(self.window,value='编辑条件不会改变表格；选择条件/组后可继续加入。')
        from .ui_text import responsive_label
        responsive_label(ttk.Label(feedback,textvariable=self.feedback,style='Muted.TLabel')).pack(fill='x')
        from tkinter import font
        self.histogram_line=font.nametofont('SmallFont').metrics('linespace')
        self.histogram=tk.Canvas(feedback,height=max(86,self.histogram_line*3+24),highlightthickness=1,highlightbackground='#c4cbd8',background='#f1f3f8');self.histogram.pack(fill='x',pady=4)
        self.bin_hint=tk.StringVar(self.window,value='数值分布基于当前已应用结果；缺失不作 0。')
        hint_box=ttk.Frame(feedback,height=self.histogram_line*3+8);hint_box.pack(fill='x');hint_box.pack_propagate(False)
        responsive_label(ttk.Label(hint_box,textvariable=self.bin_hint,style='Muted.TLabel',anchor='nw')).pack(fill='both',expand=True)
        self.histogram.bind('<Configure>',lambda _:self.draw_histogram());self.histogram.bind('<Motion>',self.histogram_hover)
        self.rule_tree.bind('<<TreeviewSelect>>',self.rule_selected);self.rule_tree.bind('<Double-1>',self.load_rule)
        self.rule_tree.bind('<Button-3>',self.rule_menu)
        actions=ttk.Frame(self.debug_host if hasattr(self,'debug_host') else parent)
        if hasattr(self,'debug_canvas'):actions.pack(side='bottom',fill='x',pady=3,before=self.debug_canvas)
        else:actions.pack(fill='x',pady=3)
        self.view_operation=tk.StringVar(self.window,value='重新筛选全部')
        operation_box=ttk.Combobox(actions,textvariable=self.view_operation,values=list(OPERATIONS),width=21,state='readonly');operation_box.pack(side='left')
        operation_box.bind('<<ComboboxSelected>>',self.new_operation)
        self.apply_view_btn=ttk.Button(actions,text='应用筛选',command=self.apply_view,style='Primary.TButton');self.apply_view_btn.pack(side='left',padx=5)
        ttk.Button(actions,text='恢复已应用条件',command=self.reset_draft).pack(side='left',padx=3)
        ttk.Button(actions,text='撤销筛选',command=lambda:self.navigate_history(-1)).pack(side='left',padx=3)
        ttk.Button(actions,text='重做',command=lambda:self.navigate_history(1)).pack(side='left',padx=3)
        ttk.Button(actions,text='原始结果',command=self.clear_view).pack(side='left',padx=3)
        self.applied_text=tk.StringVar(self.window,value='尚未应用筛选')
        ttk.Label(actions,textvariable=self.applied_text,style='Muted.TLabel').pack(side='left',padx=6)
        for var in (self.rule_metric,self.rule_operator,self.rule_value,self.view_operation):var.trace_add('write',self.queue_feedback)
        self.rule_metric.trace_add('write',self.sync_rule_operator)

    def sync_rule_operator(self,*_):
        values=['包含','不包含','=','≠'] if self.rule_metric.get() in ('文件名','类别 / 分组','判定代码') else [*OPS,'缺失','有值']
        self.rule_operator_box.configure(values=values)
        if self.rule_operator.get() not in values:self.rule_operator.set(values[0])
    def rule_menu(self,event):
        node=self.rule_tree.identify_row(event.y)
        if not node:return
        self.rule_tree.selection_set(node);menu=tk.Menu(self.window,tearoff=False)
        if self.rule_nodes[node].get('kind') in GROUPS:
            for kind,title in GROUPS.items():menu.add_command(label='改为'+title,command=lambda k=kind:self.change_group(node,k))
        if node!=self.root_rule and self.rule_tree.parent(node)!=self.root_rule:menu.add_command(label='移出当前组',command=lambda:self.promote_rule(node))
        menu.add_command(label='移除',command=self.remove_rule);menu.tk_popup(event.x_root,event.y_root)
    def change_group(self,node,kind):
        self.rule_nodes[node]={'kind':kind};self.rule_tree.item(node,text=GROUPS[kind]);self.queue_feedback()
    def promote_rule(self,node):
        parent=self.rule_tree.parent(node);grand=self.rule_tree.parent(parent)
        if grand:self.rule_tree.move(node,grand,self.rule_tree.index(parent)+1);self.queue_feedback()

    def group_parent(self):
        node=next(iter(self.rule_tree.selection()),self.root_rule)
        return node if self.rule_nodes[node].get('kind') in GROUPS else self.rule_tree.parent(node)
    def add_condition(self):
        rule={'kind':'condition','metric':self.rule_metric.get(),'op':self.rule_operator.get(),'value':self.rule_value.get()}
        parent=self.group_parent();node=self.rule_tree.insert(parent,'end',text=self.rule_text(rule));self.rule_nodes[node]=rule;self.rule_tree.item(parent,open=True);self.rule_tree.selection_set(node);self.queue_feedback()
    def add_group(self,kind):
        parent=self.group_parent();node=self.rule_tree.insert(parent,'end',text=GROUPS[kind],open=True);self.rule_nodes[node]={'kind':kind};self.rule_tree.item(parent,open=True);self.rule_tree.selection_set(node);self.queue_feedback()
    def update_condition(self):
        node=next(iter(self.rule_tree.selection()),None)
        if self.rule_nodes.get(node,{}).get('kind')!='condition':
            self.feedback.set('请选择要更新的具体条件；组可加入多个子条件。');return
        rule={'kind':'condition','metric':self.rule_metric.get(),'op':self.rule_operator.get(),'value':self.rule_value.get()}
        self.rule_nodes[node]=rule;self.rule_tree.item(node,text=self.rule_text(rule));self.queue_feedback()
    def remove_rule(self):
        for node in self.rule_tree.selection():
            if node==self.root_rule:
                for child in self.rule_tree.get_children(node):self.rule_tree.delete(child)
            else:self.rule_tree.delete(node)
        self.rule_tree.selection_set(self.root_rule);self.queue_feedback()
    def move_rule(self,offset):
        node=next(iter(self.rule_tree.selection()),None)
        if node and node!=self.root_rule:
            parent=self.rule_tree.parent(node);index=self.rule_tree.index(node);self.rule_tree.move(node,parent,max(0,index+offset));self.queue_feedback()
    def rule_text(self,rule):
        if rule.get('kind') in GROUPS:return GROUPS[rule['kind']]
        value=rule['value'];metric=numeric_fields(self.pane.task).get(rule['metric'])
        if metric and rule['op'] not in ('缺失','有值'):
            try:value=number(metric_input(value,metric))
            except ValueError:pass
        return f"{rule['metric']} {rule['op']} {value}"
    def load_rule(self,event=None):
        node=next(iter(self.rule_tree.selection()),None);rule=self.rule_nodes.get(node,{})
        if rule.get('kind')=='condition':
            self.rule_metric.set(rule['metric']);self.rule_operator.set(rule['op']);self.rule_value.set(rule['value'])
    def rule_selected(self,event=None):self.load_rule();self.queue_feedback()
    def new_operation(self,event=None):
        self.reuse_origin=False;self.queue_feedback()
    def expression(self,node=None):
        node=node or self.root_rule;value=dict(self.rule_nodes[node])
        if value.get('kind') in GROUPS:value['children']=[self.expression(c) for c in self.rule_tree.get_children(node)]
        elif value.get('op') not in ('缺失','有值'):
            metric=numeric_fields(self.pane.task).get(value.get('metric'))
            if metric:
                try:value['value']=str(metric_input(value['value'],metric))
                except ValueError:pass
        return value
    def restore_expression(self,expression):
        self.rule_tree.delete(*self.rule_tree.get_children());self.rule_nodes={}
        def insert(parent,rule):
            node=self.rule_tree.insert(parent,'end',text=self.rule_text(rule),open=True);self.rule_nodes[node]={k:v for k,v in rule.items() if k!='children'}
            for c in rule.get('children',[]):insert(node,c)
            return node
        self.root_rule=insert('',expression);self.rule_tree.selection_set(self.root_rule)
    def draft(self):
        STATES=self.states
        previous=self.draft_previous if self.reuse_origin else str(self.view_database) if self.view_database else None
        return {'expression':self.expression(),'operation':OPERATIONS[self.view_operation.get()],'previous':previous,'state':next((k for k,v in STATES.items() if v==self.state.get()),None),'category':self.category.get() if self.category.get()!='全部分组' else None}
    def apply_view(self):
        if self.condition_busy:return
        draft=copy.deepcopy(self.draft());self.pending_view_config=self.config;self.condition_busy=True;self.apply_view_btn.configure(state='disabled');self.copy_debug_btn.configure(state='disabled');self.status.set('正在应用筛选，生成完整结果…')
        def work():
            try:
                from .config import PROJECT
                self.view_events.put(('applied',create_view(self.database,self.result['run_id'],self.pane.task,draft,PROJECT/'.jobs'/'debug_views'),draft,None))
            except Exception as exc:self.view_events.put(('applied',None,draft,str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def reset_draft(self):
        if not self.applied_draft:return
        STATES=self.states
        self.restore_expression(self.applied_draft['expression']);self.state.set(STATES.get(self.applied_draft.get('state'),'全部'));self.category.set(self.applied_draft.get('category') or '全部分组')
        self.view_operation.set(next(k for k,v in OPERATIONS.items() if v==self.applied_draft.get('operation','replace')))
        self.draft_previous=self.applied_draft.get('previous');self.reuse_origin=True;self.queue_feedback()
    def navigate_history(self,direction):
        position=self.history_position+direction
        if self.condition_busy or not 0<=position<len(self.history):return
        self.history_position=position;self.view_database,self.applied_draft,self.view_config,count=self.history[position];self.offset=0;self.find_source=None
        if self.view_database:self.applied_text.set(f'已应用 {number(count,group=True)} 项');self.reset_draft()
        else:
            self.applied_text.set('原始结果');self.restore_expression({'kind':'all','children':[]});self.state.set('全部');self.category.set('全部分组');self.view_operation.set('重新筛选全部');self.reuse_origin=False
        self.refresh();self.queue_feedback()
    def clear_view(self):
        if self.condition_busy:return
        self.history_position=0;self.view_database=None;self.view_config=None;self.applied_draft=None;self.offset=0;self.find_source=None;self.applied_text.set('原始结果');self.view_operation.set('重新筛选全部');self.reuse_origin=False;self.refresh();self.queue_feedback()
    def queue_feedback(self,*_):
        self.feedback_token+=1
        if self.feedback_timer:self.window.after_cancel(self.feedback_timer)
        self.feedback_timer=self.window.after(300,self.update_feedback)
    def observation_rule(self):
        title=self.rule_metric.get();op=self.rule_operator.get();value=self.rule_value.get();rule={'metric':title if title in numeric_fields(self.pane.task) else ''}
        if op in ('>','≥'):rule.update(low=value,low_op=op)
        elif op in ('<','≤'):rule.update(high=value,high_op=op)
        elif op=='=':rule.update(low=value,high=value)
        return rule
    def update_feedback(self):
        self.feedback_timer=None
        if getattr(self,'_feedback_busy',False):self._feedback_again=True;return
        self._feedback_busy=True;self._feedback_again=False
        token=self.feedback_token;draft=self.draft();observe=self.observation_rule();db=self.view_database or self.database;view_database=self.view_database
        self.feedback.set('正在计算草稿匹配数；表格保持已应用结果。')
        def work():
            try:self.view_events.put(('feedback',(prospective_change(self.database,self.result['run_id'],self.pane.task,draft,view_database),distribution(db,self.result['run_id'],self.pane.task,observe)),token,None))
            except Exception as exc:self.view_events.put(('feedback',None,token,str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def poll_views(self):
        try:
            while True:
                kind,value,meta,error=self.view_events.get_nowait()
                if kind=='find':
                    self._find_busy=False
                    from .result_ui import result_order
                    clause,args=self.query_scope()
                    signature=(str(self.view_database or self.database),clause,tuple(args),result_order(self.sort.get(),self.direction.get()),self.find_text.get().strip())
                    if meta.get('token')==self._find_token and meta.get('signature')==signature:
                        if error:self.status.set(error)
                        elif value is None:self.status.set('当前视图没有匹配文件名。')
                        else:self.find_source,position=value;self.offset=position//self.PAGE*self.PAGE;self.refresh();self.detail.set(f"文件名匹配 {number(meta['count'],group=True)} 项；再次查找定位下一项。")
                    if self._find_again:self._find_again=False;self.find_filename()
                elif kind=='applied':
                    self.condition_busy=False;self.apply_view_btn.configure(state='normal');self.copy_debug_btn.configure(state='normal')
                    if error:self.status.set(error);continue
                    self.view_database,count=value;self.applied_draft=meta;self.view_config=self.pending_view_config;self.view_number+=1;self.offset=0;self.find_source=None
                    self.draft_previous=meta.get('previous');self.reuse_origin=True
                    self.history=self.history[:self.history_position+1];self.history.append((self.view_database,copy.deepcopy(meta),self.view_config,count));self.history_position=len(self.history)-1
                    import sqlite3
                    with sqlite3.connect(self.view_database) as db:self.selected_sources.intersection_update(r[0] for r in db.execute('SELECT source FROM files'))
                    self.selection_history.append({'conditions':meta,'count':count});self.applied_text.set(f'已应用 {number(count,group=True)} 项');self.refresh();self.queue_feedback()
                elif kind=='feedback':
                    self._feedback_busy=False
                    if meta==self.feedback_token:
                        if error:self.feedback.set(error);self.last_distribution=None
                        else:
                            change,self.last_distribution=value;count=change['count'];self.feedback.set(f"草稿应用后 {number(count,group=True)} 项；新增 {number(change['added'],group=True)} / 移出 {number(change['removed'],group=True)}。"+('结果为空；请调整条件。' if not count else '')+'表格仅在应用后更新。')
                        self.draw_histogram()
                    if self._feedback_again:self._feedback_again=False;self.queue_feedback()
        except queue.Empty:pass
    def draw_histogram(self):
        canvas=self.histogram;canvas.delete('all');dist=self.last_distribution
        if not dist or not dist.get('count'):
            self.bin_hint.set('当前结果没有该指标的有效数值。'+(f" 缺失 {number(dist.get('missing',0))} 项。" if dist else ''))
            return
        w=max(1,canvas.winfo_width());h=canvas.winfo_height();values=dist['bins'];step=w/len(values);peak=max(values) or 1
        rule=self.observation_rule();lo,hi=dist['min'],dist['max']
        metric=numeric_fields(self.pane.task).get(rule.get('metric'))
        try:lower=metric_input(rule['low'],metric) if rule.get('low') else -float('inf');upper=metric_input(rule['high'],metric) if rule.get('high') else float('inf')
        except ValueError:lower=upper=float('inf')
        base=h-self.histogram_line-12;plot=max(1,base-6)
        for n,count in enumerate(values):
            middle=lo+(hi-lo)*(n+.5)/len(values);canvas.create_rectangle(n*step+1,base-plot*count/peak,(n+1)*step-1,base,fill='#7466bb' if lower<=middle<=upper else '#bdc3d0',outline='')
        canvas.create_text(4,base+5,text=number(lo),font='SmallFont',anchor='nw');canvas.create_text(w-4,base+5,text=number(hi),font='SmallFont',anchor='ne')
        self.bin_hint.set(f"已应用范围内：有值 {number(dist['count'],group=True)}，缺失 {number(dist['missing'],group=True)}；所观察单项匹配 {number(dist['matched'],group=True)}。")
    def histogram_hover(self,event):
        d=self.last_distribution
        if not d or not d.get('count'):return
        i=min(len(d['bins'])-1,max(0,int(event.x/max(1,self.histogram.winfo_width())*len(d['bins']))));step=(d['max']-d['min'])/len(d['bins']);lo=d['min']+i*step
        self.bin_hint.set(f"{number(lo)}–{number(lo+step)}：{number(d['bins'][i],group=True)} 张（区间近似；匹配数量精确）。")

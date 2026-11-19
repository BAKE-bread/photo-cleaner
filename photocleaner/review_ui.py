"""Two explicit, paged review screens. No implicit changes on opening."""
from .numbers import number
from datetime import datetime
from pathlib import Path
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox,ttk
from .review import compression_rows,comparison_gate,numbered_rows,scan_numbered,single_root
from .engine import normalize_paths
from .moving import fingerprint


class ReviewWindow:
    PAGE=300
    def __init__(self,pane,kind):
        self.pane=pane;self.app=pane.app;self.kind=kind;self.root=single_root(pane.input_list.get(0,'end'))
        self.output=pane.output.get().strip();self.database=None;self.offset=0;self.total=0;self.rows={};self.busy=False;self.events=queue.Queue()
        if kind=='numbered' and self.output:
            candidate=Path(self.output).expanduser().resolve();self.output=str(candidate) if self.root in candidate.parents else ''
        if self.output:normalize_paths([self.root],self.output)
        if kind=='compression' and not self.output:raise ValueError('请选择已有压缩副本的输出目录。')
        self.window=tk.Toplevel(self.app.root);self.window.title('压缩对比与回写' if kind=='compression' else '清理自动编号重复图像')
        from .ui_scroll import cancel_callbacks_on_destroy
        cancel_callbacks_on_destroy(self.window)
        scale=self.app.dpi/96
        from .dpi import initial_geometry
        self.window.geometry(initial_geometry(self.window,self.app.dpi,details=True))
        frame=ttk.Frame(self.window,padding=12);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='压缩对比' if kind=='compression' else '编号副本清理',font='TitleFont').pack(anchor='w')
        ttk.Label(frame,text=f'输入：{self.root}',style='Muted.TLabel',wraplength=1050).pack(anchor='w',pady=(6,8))
        rule='允许回写：至少节省 0.5 MB · 原图至少 72 小时未修改 · PNG 保留两版' if kind=='compression' else '仅列出编号关系且字节完全一致的副本 · 代表图像保留'
        ttk.Label(frame,text=rule,wraplength=1050).pack(anchor='w')
        legend=ttk.Frame(frame);legend.pack(fill='x',pady=(8,8))
        for title,bg,fg in [('可操作','#e4f2e5','#123c19'),('条件未满足','#fff2d2','#654800'),('文件变化','#ffe1e1','#7a1515'),('已操作','#eaeaea','#454545')]:
            tk.Label(legend,text=title,font='SmallFont',bg=bg,fg=fg,padx=10,pady=4).pack(side='left',padx=(0,8))
        ttk.Label(legend,text='关闭前可撤回，关闭后旧版进入回收站',style='Muted.TLabel').pack(side='left',padx=8)
        self.status=tk.StringVar(value='准备读取列表。');ttk.Label(frame,textvariable=self.status,wraplength=1050).pack(anchor='w')
        box=ttk.Frame(frame);box.pack(fill='both',expand=True,pady=6)
        cols=('path','old','new','save','percent','size','mtime','state') if kind=='compression' else ('path','keep','old','state')
        labels=('原图相对路径','原图 MB','JPG MB','减少 MB','减少比例','原图 → JPG 像素 / 格式','原图修改时间','操作条件') if kind=='compression' else ('编号副本相对路径','将保留的文件','副本 MB','状态')
        widths=(260,85,85,85,85,270,160,290) if kind=='compression' else (330,330,100,280)
        style=ttk.Style();style.configure('Review.Treeview',rowheight=round(27*scale),font='BodyFont')
        self.tree=ttk.Treeview(box,columns=cols,show='headings',selectmode='extended',style='Review.Treeview')
        for c,label,width in zip(cols,labels,widths):self.tree.heading(c,text=label);self.tree.column(c,width=round(width*scale),minwidth=round(95*scale),stretch=c in ('path','state','keep'),anchor='w' if c in ('path','state','keep','size','mtime') else 'center')
        self.tree.tag_configure('eligible',background='#e4f2e5',foreground='#123c19');self.tree.tag_configure('blocked',background='#fff2d2',foreground='#654800')
        self.tree.tag_configure('error',background='#ffe1e1',foreground='#7a1515');self.tree.tag_configure('applied',background='#eaeaea',foreground='#454545')
        ys=ttk.Scrollbar(box,command=self.tree.yview);xs=ttk.Scrollbar(box,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set,xscrollcommand=xs.set);ys.pack(side='right',fill='y');xs.pack(side='bottom',fill='x');self.tree.pack(fill='both',expand=True)
        self.details=tk.Text(frame,height=4,wrap='word',font='SmallFont',state='disabled',background='#f0f2f7',foreground='#475569',relief='flat',padx=10,pady=8);self.details.pack(fill='x')
        self.tree.bind('<<TreeviewSelect>>',self._details)
        from .result_ui import ImageHover,fit_tree_columns
        self.fit_columns=lambda:fit_tree_columns(self.tree)
        self.hover=ImageHover(self.tree,lambda key:self.rows.get(key,{}).get('source'))
        nav=ttk.Frame(frame);nav.pack(fill='x',pady=6);self.buttons=[]
        ttk.Button(nav,text='适合内容',command=self.fit_columns).pack(side='right')
        for title,fn in [('上一页',lambda:self.page(-1)),('下一页',lambda:self.page(1)),('刷新当前页',self.refresh),('选择本页可操作项',self.select_eligible)]:
            b=ttk.Button(nav,text=title,command=fn);b.pack(side='left',padx=(0,5));self.buttons.append(b)
        ops=ttk.Frame(frame);ops.pack(fill='x')
        for title,fn in [('回写选中项' if kind=='compression' else '清理选中副本',lambda:self.apply(False)),('回写全部允许项' if kind=='compression' else '清理全部副本',lambda:self.apply(True)),('撤回待关闭操作',self.undo),('打开原图',lambda:self.open_file('source')),('打开 JPG 副本' if kind=='compression' else '打开保留图像',lambda:self.open_file('compressed' if kind=='compression' else 'representative'))]:
            b=ttk.Button(ops,text=title,command=fn,style='Primary.TButton' if title in ('回写选中项','清理选中副本') else 'TButton');b.pack(side='left',padx=(0,5));self.buttons.append(b)
        if kind=='numbered':
            b=ttk.Button(nav,text='重新扫描',command=self.rescan);b.pack(side='left');self.buttons.append(b)
        self.window.protocol('WM_DELETE_WINDOW',self.close);self.window.after(100,self.poll)
        self.rescan() if kind=='numbered' else self.refresh()

    def _start(self,fn,callback,mode='preview'):
        if self.busy or self.pane.running:return
        active_output=Path(self.output).resolve() if self.kind=='compression' else None
        try:self.app.check_conflicts(self.pane,[self.root],active_output,mode)
        except (OSError,ValueError) as exc:messagebox.showerror('无法开始',str(exc),parent=self.window);return
        self.busy=True;self.pane.active_roots=[self.root];self.pane.active_output=active_output
        self.saved_pending_mode=self.pane.pending_mode
        self.pane.pending_mode=mode;self.pane.stop.clear();self.pane.set_running(True)
        for b in self.buttons:b.configure(state='disabled')
        def work():
            try:self.events.put((callback,fn(),None))
            except Exception as exc:self.events.put((callback,None,str(exc)))
        self.pane.worker=threading.Thread(target=work,daemon=False);self.pane.worker.start()

    def poll(self):
        try:
            callback,result,error=self.events.get_nowait();self.busy=False;self.pane.pending_mode=self.saved_pending_mode;self.pane.set_running(False)
            for b in self.buttons:b.configure(state='normal')
            if error:self.status.set('未完成：'+error);self.app.log(self.pane.task,error)
            else:callback(result)
        except queue.Empty:pass
        if self.window.winfo_exists():self.window.after(100,self.poll)

    def emit(self,event):self.app.post(self.pane.task,event)
    def rescan(self):
        if self.kind!='numbered':return
        self.status.set('扫描编号副本：先按命名和体积分组，再对可能重复的图像计算哈希。')
        def finished(result):
            self.database=result['database'];self.offset=0
            if result['stopped']:self.status.set('已暂停扫描；尚未生成清理清单，请重新扫描后操作。');self.database=None;return
            self.app.log(self.pane.task,f"编号副本预览 | 图像 {number(result['scanned'],group=True)} | 哈希检查 {number(result['checked'],group=True)} | 可清理 {number(result['candidates'],group=True)}")
            self.refresh()
        self._start(lambda:scan_numbered(self.root,self.output or None,self.pane.stop,self.emit),finished)

    def refresh(self):
        if self.kind=='numbered' and not self.database:return
        def load():
            rows,total=compression_rows(self.root,self.output,self.offset,self.PAGE) if self.kind=='compression' else numbered_rows(self.database,self.offset,self.PAGE)
            states=self.app.safe_actions.statuses(self.root,[r['source'] for r in rows])
            result=[]
            for row in rows:
                state=states.get(row['source'],{})
                if self.kind=='compression':
                    row=comparison_gate(row)
                    if state.get('kind') in ('png','replace') and state.get('new_hash')==row['digest'] and state.get('phase')=='applied':
                        row.update(eligible=False,tone='applied',reason='已回写，本会话已结束' if state.get('closed') else '已回写，可撤回')
                else:
                    applied=state.get('kind')=='numbered' and state.get('phase')=='applied'
                    row.update(eligible=not applied,tone='applied' if applied else 'eligible',reason='已清理，本会话已结束' if applied and state.get('closed') else '已暂存，可撤回' if applied else '内容完全相同；允许清理')
                    if not applied:
                        try:
                            if fingerprint(Path(row['source']))!=(row['size'],row['mtime']) or not Path(row['representative']).is_file():raise OSError('副本或保留图像已变化，请重新扫描。')
                        except OSError as exc:row.update(eligible=False,tone='error',reason=str(exc))
                result.append(row)
            return result,total
        self._start(load,self._render)

    def _render(self,result):
        rows,self.total=result;self.tree.delete(*self.tree.get_children());self.rows={}
        for i,row in enumerate(rows):
            id=str(i);self.rows[id]=row
            if self.kind=='compression':
                saving=row.get('saving');out=row.get('output_size')
                vals=(row['relative'],f"{number(row['size']/1e6)}",f'{number(out/1e6)}' if out is not None else '',
                    f'{number(saving/1e6)}' if saving is not None else '',f'{number((saving/row["size"])*100)}%' if saving is not None and row['size'] else '',
                    f"{number(row['width'])}×{number(row['height'])} {'MPO 主图' if row.get('primary_only') else row['format']} → {row.get('output_width') or row['width']}×{row.get('output_height') or row['height']} JPG",
                    datetime.fromtimestamp(row['mtime']/1e9).strftime('%Y-%m-%d %H:%M:%S'),row['reason'])
            else:vals=(str(Path(row['source']).relative_to(self.root)),str(Path(row['representative']).relative_to(self.root)),f"{number(row['size']/1e6)}",row['reason'])
            self.tree.insert('','end',iid=id,values=vals,tags=(row['tone'],))
        self.fit_columns()
        self.status.set(f'共 {number(self.total,group=True)} 项 | 当前 {number(self.offset+1 if rows else 0,group=True)}–{number(self.offset+len(rows),group=True)} | 本页允许 {number(sum(r["eligible"] for r in rows),group=True)} 项。可多选，详细路径显示在下方。')
        if not self.total:self.status.set('当前输入/输出目录没有成功的压缩副本，请先生成 JPG 副本。' if self.kind=='compression' else '未发现符合命名及完全相同内容条件的编号副本。')

    def page(self,direction):
        offset=self.offset+direction*self.PAGE
        if 0<=offset<self.total:self.offset=offset;self.refresh()
    def select_eligible(self):self.tree.selection_set([i for i,r in self.rows.items() if r['eligible']])
    def _details(self,event=None):
        selected=self.tree.selection();row=self.rows.get(selected[0]) if selected else None
        if not row:return
        text=f"原图：{row['source']}\n"+('JPG 副本：'+row['compressed'] if self.kind=='compression' else '保留图像：'+row['representative'])+'\n'+row['reason']
        if row.get('primary_only'):text+='；回写 JPG 会去掉 MPO 的副图，旧 MPO 原图可以撤回。'
        if self.kind=='compression':
            text+=f"\nJPG 质量：{row.get('quality') or '历史记录未保存'}；MB 按 1,000,000 字节计算。"
            if row.get('saving') is not None:text+=f" 减少 {number(row['saving'],group=True)} 字节，回写门槛为 500,000 字节。"
            if row.get('output_mtime'):text+=' JPG 修改时间：'+datetime.fromtimestamp(row['output_mtime']/1e9).strftime('%Y-%m-%d %H:%M:%S')
        self.details.configure(state='normal');self.details.delete('1.0','end');self.details.insert('end',text);self.details.configure(state='disabled')

    def apply(self,all_rows):
        if all_rows and not self.total:messagebox.showinfo('没有可操作项','当前列表没有项目。',parent=self.window);return
        selected=[dict(self.rows[i]) for i in self.tree.selection() if self.rows[i]['eligible']]
        if not all_rows and not selected:messagebox.showinfo('没有可操作项','请选择允许操作的行。',parent=self.window);return
        target='全部当前允许的项目（执行时逐项重新校验）' if all_rows else f'选中的 {number(len(selected))} 项'
        text=f'{"回写" if self.kind=="compression" else "清理"}{target}？\n旧原图或重复副本先暂存，可在关闭主程序前撤回；正常关闭后可在回收站找到。'
        if self.kind=='compression':text+='\nPNG 保留原图并添加 JPG；其他原图被 JPG 替换。MPO 的 JPG 只含主图。输出副本继续保留。'
        if not messagebox.askyesno('执行已预览操作',text,parent=self.window):return
        def work():
            done=errors=skipped=0
            def batches():
                if not all_rows:yield selected;return
                offset=0
                while True:
                    rows,total=compression_rows(self.root,self.output,offset,self.PAGE) if self.kind=='compression' else numbered_rows(self.database,offset,self.PAGE)
                    if not rows:break
                    yield rows;offset+=len(rows)
            for rows in batches():
                states=self.app.safe_actions.statuses(self.root,[r['source'] for r in rows])
                for row in rows:
                    if self.pane.stop.is_set():return done,errors,skipped
                    try:
                        state=states.get(row['source'],{})
                        if state.get('phase')=='applied' and (self.kind=='numbered' or state.get('new_hash')==row.get('digest')):skipped+=1;continue
                        if self.kind=='compression':
                            if not comparison_gate(row)['eligible']:skipped+=1;continue
                            path=self.app.safe_actions.apply_compression(self.root,row)
                        else:path=self.app.safe_actions.remove_numbered(self.root,row)
                        done+=1;self.emit({'kind':'log','message':f'已暂存旧版，可撤回：{path}'})
                    except (OSError,ValueError) as exc:errors+=1;self.emit({'kind':'log','message':f'跳过：{row["source"]}；{exc}'})
            return done,errors,skipped
        def finished(result):
            self.app.log(self.pane.task,f'安全操作完成 | 已操作 {result[0]} | 错误 {result[1]} | 条件不符或已操作 {result[2]}')
            self.refresh()
        self._start(work,finished,'move')

    def undo(self):
        self._start(lambda:self.app.safe_actions.undo(self.root,self.kind,lambda s:self.emit({'kind':'log','message':s}),self.pane.stop),lambda result:(self.app.log(self.pane.task,f'撤回 | 已恢复 {result["restored"]} | 错误 {number(result["errors"])}'),self.refresh()),'move')
    def open_file(self,key):
        selected=self.tree.selection()
        if not selected:return
        try:os.startfile(self.rows[selected[0]][key])
        except OSError as exc:messagebox.showerror('无法打开',str(exc),parent=self.window)
    def close(self):
        if self.busy:self.pane.stop.set();self.status.set('已请求暂停，等待当前文件安全完成后再关闭此列表。');return
        self.pane.review_window=None;self.window.destroy()

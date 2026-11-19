"""Independent search pages and paged, explicitly confirmed image exports."""
from .numbers import number,input_int
import io,os,threading,math
from pathlib import Path
from dataclasses import asdict
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
from PIL import Image,ImageTk
from .desktop import TaskPane
from .tasks import TASK_NAMES
from .search_model import SearchConfig,load_search_image
from .search import SearchEngine
from .search_copy import copy_results
from .ui_text import Paragraph,responsive_label
from .ui_theme import BG,SURFACE,INK,MUTED,LINE,ACCENT
from .directory_lists import clean_path,path_entry


class SearchPane(TaskPane):
    def _build(self,settings):
        d=lambda x:round(x*self.app.dpi/96);previous=settings.get('config',{})
        self.results_window=None;self.result_data=None;self.last_report=settings.get('last_report')
        header=ttk.Frame(self.frame,padding=(d(24),d(20),d(24),d(12)));header.pack(fill='x',before=self.canvas.master)
        ttk.Label(header,text=TASK_NAMES[self.task],font='TitleFont').pack(side='left')
        ttk.Button(header,text='使用说明',style='Ghost.TButton',command=self.show_help).pack(side='right')
        Paragraph(self.body,'模型：SigLIP 2 Base + DINOv2 Small（共用一份索引）；文字精细核对另用 Qwen3-VL-2B。',background=BG).pack(fill='x',pady=(0,d(8)))
        self.body.configure(padding=(d(24),0,d(24),d(12)));self.canvas.configure(background=BG)
        def card(title):
            border=tk.Frame(self.body,background=LINE);border.pack(fill='x',pady=(0,d(12)))
            box=ttk.Frame(border,style='Card.TFrame',padding=d(16));box.pack(fill='x',padx=1,pady=1)
            ttk.Label(box,text=title,style='CardTitle.TLabel').pack(anchor='w',pady=(0,d(8)))
            return box
        source=card('搜索范围')
        Paragraph(source,'递归搜索多个输入目录。原图只读，候选选中并确认后才复制。').pack(fill='x')
        row=ttk.Frame(source,style='Card.TFrame');row.pack(fill='x',pady=d(6))
        self.input_list=tk.Listbox(row,height=3,selectmode='extended',exportselection=False,font='BodyFont',bg='#f8f9fc',fg=INK,bd=0)
        self.input_list.pack(side='left',fill='x',expand=True);self.fields.append(self.input_list)
        from .ui_scroll import AutoScrollbar
        ys=AutoScrollbar(row,command=self.input_list.yview);ys.pack(side='right',fill='y');self.input_list.configure(yscrollcommand=ys.set)
        for path in settings.get('inputs',[]):self.input_list.insert('end',path)
        bar=ttk.Frame(source,style='Card.TFrame');bar.pack(fill='x')
        for label,fn in [('添加目录',self._add),('移除',self._remove),('复制',self.copy_inputs),('粘贴追加',self.paste_inputs)]:
            button=ttk.Button(bar,text=label,command=fn,style='Ghost.TButton');button.pack(side='left',padx=(0,d(6)));self.fields.append(button)
        self.input_list.bind('<Control-c>',lambda _:self.copy_inputs());self.input_list.bind('<Control-v>',lambda _:self.paste_inputs())
        ttk.Label(source,text='复制输出目录（可先留空，在候选窗口中选择）',style='CardMuted.TLabel').pack(anchor='w',pady=(d(10),d(4)))
        dest=ttk.Frame(source,style='Card.TFrame');dest.pack(fill='x')
        entry=ttk.Entry(dest,textvariable=self.output);entry.pack(side='left',fill='x',expand=True);self.fields.append(entry)
        path_entry(entry,self.output)
        b=ttk.Button(dest,text='选择',command=self._browse);b.pack(side='right',padx=(d(6),0));self.fields.append(b)
        self.retain_tree=tk.BooleanVar(value=previous.get('retain_tree',False))
        b=ttk.Checkbutton(source,text='复制输出保留来源层级（不勾选则扁平输出并去重）',variable=self.retain_tree);b.pack(anchor='w',pady=(d(6),0));self.fields.append(b)
        query=card('搜索描述' if self.task=='text_search' else '参考图像与搜索方式')
        self.reference=tk.StringVar(value=previous.get('reference',''))
        self.mode=tk.StringVar(value=previous.get('mode','similar') if previous.get('mode') in ('similar','style') else 'similar')
        self.combine=tk.StringVar(value=previous.get('combine','any'))
        self.refine=tk.BooleanVar(value=previous.get('refine',True))
        if self.task=='text_search':
            Paragraph(query,'同一目标的属性用一行顿号连接；不同目标分行选“任一条”，拆分属性选“综合全部”。角色名、精确左右和细小关系不保证识别，请补充可见外观。').pack(fill='x')
            ttk.Button(query,text='描述示例与识别边界',command=self.show_help,style='Ghost.TButton').pack(anchor='w')
            self.prompts=tk.Text(query,height=4,font='BodyFont',wrap='word',bg='#f8f9fc',fg=INK,relief='flat',padx=d(8),pady=d(8));self.prompts.pack(fill='x',pady=d(6));self.fields.append(self.prompts)
            self.prompts.insert('1.0','\n'.join(previous.get('prompts',[])))
            options=ttk.Frame(query,style='Card.TFrame');options.pack(fill='x')
            for title,value in [('任一条描述符合','any'),('综合全部描述','all')]:
                b=ttk.Radiobutton(options,text=title,variable=self.combine,value=value);b.pack(side='left',padx=(0,d(16)));self.fields.append(b)
            b=ttk.Checkbutton(query,text='精细核对前 60 项的人物/场景特征（推荐；较慢）',variable=self.refine);b.pack(anchor='w',pady=(d(5),0));self.fields.append(b)
        else:
            ref=ttk.Frame(query,style='Card.TFrame');ref.pack(fill='x')
            e=ttk.Entry(ref,textvariable=self.reference);e.pack(side='left',fill='x',expand=True);self.fields.append(e)
            path_entry(e,self.reference)
            from .file_drop import bind_file_drop
            e.after_idle(lambda:bind_file_drop(e,self.drop_reference))
            Paragraph(query,'可从资源管理器拖入一个图像到上方路径框；也可粘贴带引号的文件路径。').pack(fill='x')
            b=ttk.Button(ref,text='选择图像',command=self.choose_reference);b.pack(side='right',padx=(d(6),0));self.fields.append(b)
            for title,value in [('相似或相同：构图、主体和重复图像','similar'),('相近风格：优先人物系列、Cosplay 与绘画','style')]:
                b=ttk.Radiobutton(query,text=title,variable=self.mode,value=value);b.pack(anchor='w',pady=d(3));self.fields.append(b)
            Paragraph(query,'风格搜索结合视觉与外观特征排序，降低纯环境和工业设计图的优先级；不能确认是否同一天拍摄。').pack(fill='x',pady=(d(6),0))
        settings_box=card('候选与运行设置');row=ttk.Frame(settings_box,style='Card.TFrame');row.pack(fill='x')
        self.limit=tk.StringVar(value=str(previous.get('limit',200)));self.device=tk.StringVar(value=previous.get('device','auto'));self.offline=tk.BooleanVar(self.app.root,value=__import__('photocleaner.defaults',fromlist=['models_cached']).models_cached(self.task) or previous.get('offline',False))
        ttk.Label(row,text='候选上限',style='Card.TLabel').pack(side='left');e=ttk.Entry(row,textvariable=self.limit,width=7);e.pack(side='left',padx=(d(6),d(16)));self.fields.append(e)
        ttk.Label(row,text='推理设备',style='Card.TLabel').pack(side='left');e=ttk.Combobox(row,textvariable=self.device,values=['auto','cuda','cpu'],state='readonly',width=8);e.pack(side='left',padx=(d(6),d(16)));self.fields.append(e);self.device_box=e
        b=ttk.Checkbutton(row,text='离线模型',variable=self.offline);b.pack(side='left');self.fields.append(b)
        self.workers=tk.StringVar(value=str(previous.get('workers',8)))
        ttk.Label(row,text='CPU 解码线程',style='Card.TLabel').pack(side='left',padx=(d(12),d(5)))
        b=ttk.Spinbox(row,from_=1,to=16,textvariable=self.workers,width=4);b.pack(side='left');self.fields.append(b)
        b=ttk.Button(settings_box,text='合并其他索引库…',command=self.merge_index,style='Ghost.TButton');b.pack(anchor='w',pady=(d(6),0));self.fields.append(b)
        Paragraph(settings_box,'首次建立索引较慢，之后换描述或参考图可复用。新增/修改文件后点“更新索引并搜索”；暂停后普通搜索会继续未完成的索引。').pack(fill='x',pady=(d(6),0))
        footer=ttk.Frame(self.frame,padding=(d(24),d(10),d(24),d(16)));footer.pack(fill='x')
        self.progress=ttk.Progressbar(footer);self.progress.pack(fill='x',pady=(0,d(8)))
        self.eta_text=tk.StringVar(value='就绪 · 首次搜索将建立本地索引')
        responsive_label(ttk.Label(footer,textvariable=self.eta_text,style='Muted.TLabel')).pack(fill='x')
        responsive_label(ttk.Label(footer,textvariable=self.status,style='Muted.TLabel')).pack(fill='x',pady=(0,d(8)))
        actions=ttk.Frame(footer);actions.pack(fill='x');self.action_buttons=[]
        for title,fn,style in [('搜索 / 继续',lambda:self.start_search(False),'Primary.TButton'),('更新索引并搜索',lambda:self.start_search(True),'TButton'),('查看候选',self.show_results,'TButton')]:
            b=ttk.Button(actions,text=title,command=fn,style=style);b.pack(side='left',padx=(0,d(8)));self.action_buttons.append(b)
        self.pause_btn=ttk.Button(actions,text='暂停',command=self.pause,state='disabled');self.pause_btn.pack(side='left')
        self.status.set('先搜索，再在缩略图候选中勾选并确认复制。')

    def config(self):
        return SearchConfig(mode='text' if self.task=='text_search' else self.mode.get(),
            prompts=tuple(p.strip() for p in self.prompts.get('1.0','end').splitlines() if p.strip()) if self.task=='text_search' else (),
              combine=self.combine.get(),reference=clean_path(self.reference.get()),limit=input_int(self.limit.get()),refine=self.refine.get(),retain_tree=self.retain_tree.get(),workers=input_int(self.workers.get()),device=self.device.get(),offline=self.offline.get())
    def settings(self):
        return {'inputs':self.input_list.get(0,'end'),'output':self.output.get(),'config':asdict(self.config()),'last_report':self.last_report}
    def set_running(self,active):
        self.running=active;self.app.activity_changed()
        for w in self.fields+self.action_buttons:w.configure(state='disabled' if active else 'readonly' if w is self.device_box else 'normal')
        self.pause_btn.configure(state='normal' if active else 'disabled')
        if active:self.progress.configure(mode='indeterminate');self.progress.start(15)
        else:self.progress.stop();self.progress.configure(mode='determinate')
        if self.results_window:self.results_window.update_selection()
    def choose_reference(self):
        path=filedialog.askopenfilename(title='选择参考图像',filetypes=[('图像','*.jpg *.jpeg *.png *.webp *.bmp *.avif *.heic *.tif *.tiff'),('所有文件','*.*')])
        if path:self.reference.set(path)
    def drop_reference(self,paths):
        if self.running:return
        paths=[clean_path(p) for p in paths]
        if len(paths)!=1 or not Path(paths[0]).is_file():self.status.set('请拖入一个图像文件作为参考。');return
        self.reference.set(paths[0]);self.status.set('参考图已设置；搜索时会验证图像。')
    def merge_index(self):
        if self.running:return
        path=filedialog.askopenfilename(title='合并兼容的搜索索引',filetypes=[('索引数据库','*.sqlite3')])
        if not path:return
        self.stop.clear();self.set_running(True)
        def work():
            try:
                from .config import PROJECT
                from .storage import output_lock
                from .search import SearchIndex
                with output_lock(Path(self.app.search_index_dir.get())):
                    index=SearchIndex(Path(self.app.search_index_dir.get()))
                    try:count=index.merge(path)
                    finally:index.close()
                self.app.post(self.task,{'kind':'index_merged','count':count})
            except Exception as exc:self.app.post(self.task,{'kind':'failure','message':str(exc)})
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()
    def show_help(self):
        from .help_ui import show_help
        return show_help(self,'添加搜索目录和查询，预览勾选候选后复制。详细说明可编辑 help 中对应文件。')
    def start_search(self,refresh):
        if self.running:return
        try:
            config=self.config();config.validate()
            from .engine import normalize_inputs,normalize_paths
            inputs=list(self.input_list.get(0,'end'));roots=normalize_inputs(inputs);output=self.output.get().strip()
            out=normalize_paths(inputs,output)[1] if output else None
            self.app.check_conflicts(self,roots,out,'preview')
        except (OSError,ValueError) as exc:messagebox.showerror('无法搜索',str(exc));return
        self.active_roots=roots;self.active_output=out;self.pending_mode='preview';self.stop.clear()
        if self.results_window:self.results_window.window.destroy();self.results_window=None
        self.result_data=None;self.set_running(True);self.app.save();self.status.set('正在更新索引。' if refresh else '正在准备索引与搜索。')
        def work():
            try:SearchEngine(config,lambda e:self.app.post(self.task,e),self.stop,index_dir=self.app.search_index_dir.get()).run(inputs,output,refresh)
            except Exception as exc:self.app.post(self.task,{'kind':'failure','message':str(exc)})
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()
    def handle(self,event):
        kind=event['kind']
        if kind=='index_merged':self.set_running(False);self.status.set(f"已合并 {number(event['count'],group=True)} 条图像特征；添加实际目录后点击更新索引。");return
        if kind=='log':self.app.log(self.task,event['message'])
        elif kind=='scan':self.status.set(f"{'扫描完成' if event.get('complete') else '扫描中'} · {number(event['scanned'],group=True)} 张图像路径")
        elif kind=='progress':
            self.progress.stop();self.progress.configure(mode='determinate',maximum=max(1,event['total']),value=event['processed']);self.status.set(f"{event.get('phase','处理')} · {number(event['processed'],group=True)} / {number(event['total'],group=True)}")
        elif kind=='estimate':
            from .runtime import duration
            self.eta_text.set(f"{event.get('phase','处理')}预计剩余 {duration(event['remaining_seconds'])} · 约 {number(event['rate'])} 张/秒")
        elif kind in ('search_done','copy_done','failure'):
            self.set_running(False)
            if kind=='failure':self.status.set('未完成：'+event['message']);self.app.log(self.task,event['message'])
            elif kind=='search_done':
                self.result_data=event;self.status.set(f"{'已暂停，可继续' if event.get('stopped') else '搜索完成'} · 索引 {number(event['indexed'],group=True)} 张 · 候选 {number(len(event['results']),group=True)} 张 · 无法读取等跳过 {number(event.get('skipped',0))} 张")
                self.eta_text.set('索引保存在本地；文件有变化时请更新索引')
                if event['results']:self.show_results()
            else:
                self.last_report=event['report'];self.status.set(f"复制{'已暂停' if event['stopped'] else '完成'} · 新复制 {number(event['copied'])} · 已有副本跳过 {number(event['skipped'])} · 未完成 {number(event['errors'])}；原图保留")
                self.app.log(self.task,self.status.get()+' | '+event['report'])
                if self.results_window:self.results_window.note.set(self.status.get())
            self.app.save()
    def show_results(self):
        if not self.result_data or not self.result_data.get('results'):return
        if self.results_window and self.results_window.window.winfo_exists():self.results_window.window.lift();return
        self.results_window=ResultWindow(self,self.result_data)

class ResultWindow:
    page_size=12
    def __init__(self,pane,data):
        self.pane=pane;self.data=data;self.rows=data['results'];self.selected=set();self.page=0;self.photos=[];self.checks=[]
        self._closed=False;self._generation=0;self._thumb_cache=OrderedDict();self._thumbnail_jobs={};self._preview_jobs={};self._card_frames=[];self._layout_after=None
        self._image_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='candidate-image')
        self.window=tk.Toplevel(pane.app.root);self.window.title(TASK_NAMES[pane.task]+' · 候选预览')
        from .ui_scroll import cancel_callbacks_on_destroy
        cancel_callbacks_on_destroy(self.window)
        self.window.bind('<Destroy>',self.dispose,add='+')
        d=lambda x:round(x*pane.app.dpi/96)
        from .dpi import initial_geometry
        self.window.geometry(initial_geometry(self.window,pane.app.dpi,details=True))
        self.window.minsize(d(760),d(550));self.scale=pane.app.dpi/96;self.columns=4;self.cards={};frame=ttk.Frame(self.window,padding=d(18));frame.pack(fill='both',expand=True)
        self.note=tk.StringVar(value=f"{number(len(self.rows),group=True)} 个顺序候选 · 单击图像勾选，双击放大；分数仅用于相关性排序")
        responsive_label(ttk.Label(frame,textvariable=self.note,style='Muted.TLabel')).pack(fill='x',pady=(0,d(8)))
        nav=ttk.Frame(frame);nav.pack(fill='x');self.page_text=tk.StringVar()
        for text,fn in [('上一页',lambda:self.turn(-1)),('下一页',lambda:self.turn(1)),('勾选本页',self.select_page),('清空全部勾选',self.clear)]:ttk.Button(nav,text=text,command=fn).pack(side='left',padx=(0,d(6)))
        ttk.Label(nav,textvariable=self.page_text).pack(side='right')
        box=ttk.Frame(frame);box.pack(fill='both',expand=True,pady=d(10));self.canvas=tk.Canvas(box,highlightthickness=0,bg=BG)
        from .ui_scroll import AutoScrollbar,fit_canvas
        ys=AutoScrollbar(box,command=self.canvas.yview);ys.pack(side='right',fill='y');self.canvas.pack(side='left',fill='both',expand=True);self.canvas.configure(yscrollcommand=ys.set)
        self.body=ttk.Frame(self.canvas);self.item=self.canvas.create_window(0,0,window=self.body,anchor='nw')
        self.canvas.bind('<Configure>',self.resize)
        fit_canvas(self.canvas,self.body,self.item)
        self.canvas.bind('<MouseWheel>',self.wheel)
        bottom=ttk.Frame(frame);bottom.pack(fill='x')
        self.copy_button=ttk.Button(bottom,text='复制所选图像',command=self.copy,style='Primary.TButton');self.copy_button.pack(side='right')
        self.output_button=ttk.Button(bottom,text='选择输出目录',command=pane._browse);self.output_button.pack(side='right',padx=d(8))
        responsive_label(ttk.Label(bottom,textvariable=pane.output,style='Muted.TLabel')).pack(side='left',fill='x',expand=True)
        self.window.protocol('WM_DELETE_WINDOW',self.close);self.render();self.window.after(20,self.poll_images)
    def wheel(self,e):
        from .ui_scroll import can_scroll
        if can_scroll(self.canvas):self.canvas.yview_scroll(-int(e.delta/120),'units')
        return 'break'
    def resize(self,e):
        cols=4 if e.width>=960*self.scale else 3 if e.width>=700*self.scale else 2
        if cols!=self.columns:
            self.columns=cols
            if self._layout_after is not None:self.window.after_cancel(self._layout_after)
            self._layout_after=self.window.after(70,self.layout_cards)
    def layout_cards(self):
        self._layout_after=None
        if self._closed:return
        for col in range(4):self.body.columnconfigure(col,weight=1 if col<self.columns else 0,uniform='candidate' if col<self.columns else '',minsize=0)
        for n,frame in enumerate(self._card_frames):frame.grid(row=n//self.columns,column=n%self.columns,sticky='nsew',padx=4,pady=5)
    def visible(self):return self.rows[self.page*self.page_size:(self.page+1)*self.page_size]
    def turn(self,delta):
        page=max(0,min(math.ceil(len(self.rows)/self.page_size)-1,self.page+delta))
        if page!=self.page:self.page=page;self.render()
    def select_page(self):
        if not self.pane.running:self.selected.update(r['path'] for r in self.visible());self.sync_selection()
    def clear(self):
        if not self.pane.running:self.selected.clear();self.sync_selection()
    def toggle(self,path):
        if self.pane.running:return
        if path in self.selected:self.selected.remove(path)
        else:self.selected.add(path)
        self.sync_selection()
    def sync_selection(self):
        for path,(border,var) in self.cards.items():
            selected=path in self.selected;var.set(selected);border.configure(bg=ACCENT if selected else LINE)
        self.update_selection()
    def update_selection(self):
        self.copy_button.configure(text=f'复制所选 {number(len(self.selected))} 张…',state='normal' if self.selected and not self.pane.running else 'disabled')
        self.output_button.configure(state='disabled' if self.pane.running else 'normal')
    def render(self):
        if self._closed or not self.window.winfo_exists():return
        self._generation+=1;generation=self._generation
        if getattr(self,'click_id',None):self.window.after_cancel(self.click_id);self.click_id=None
        # Cancel obsolete queued work. At most two old decodes can still be
        # running; their results are closed instead of painting a newer page.
        for future in list(self._thumbnail_jobs):
            if future.cancel():self._thumbnail_jobs.pop(future)
            elif future.done():
                self._thumbnail_jobs.pop(future)
                try:
                    im,_=future.result()
                    if im is not None:im.close()
                except Exception:pass
        for child in self.body.winfo_children():child.destroy()
        self.photos=[];self.checks=[];self.cards={};self._card_frames=[]
        for n,row in enumerate(self.visible()):
            chosen=row['path'] in self.selected
            border=tk.Frame(self.body,bg=ACCENT if chosen else LINE,padx=2,pady=2);border.grid(row=n//self.columns,column=n%self.columns,sticky='nsew',padx=4,pady=5)
            self._card_frames.append(border)
            card=ttk.Frame(border,style='Card.TFrame',padding=8);card.pack(fill='both',expand=True)
            size=(round(200*self.scale),round(156*self.scale))
            slot=tk.Frame(card,width=size[0],height=size[1],bg=SURFACE);slot.pack();slot.pack_propagate(False)
            label=tk.Label(slot,text='正在读取缩略图',font='SmallFont',bg=SURFACE,fg=MUTED,cursor='hand2');label.pack(fill='both',expand=True)
            photo=self._thumb_cache.get(row['path'])
            if photo is not None:
                self._thumb_cache.move_to_end(row['path']);label.configure(image=photo,text='');label.image=photo
            else:
                from .search_render import decode_thumbnail
                future=self._image_pool.submit(decode_thumbnail,row.get('thumb'),size)
                self._thumbnail_jobs[future]=(generation,row['path'],label)
            # Delay the single-click toggle so double-click never destroys the
            # widget before Tk can dispatch the full-preview action.
            label.bind('<Button-1>',lambda e,r=row:self.click_later(r))
            label.bind('<Double-Button-1>',lambda e,r=row:self.enlarge(r))
            var=tk.BooleanVar(self.window,value=chosen);self.checks.append(var);self.cards[row['path']]=(border,var)
            b=ttk.Checkbutton(card,text=f"{number(self.page*self.page_size+n+1)}. 勾选此图",variable=var,command=lambda p=row['path']:self.toggle(p));b.pack(fill='x')
            responsive_label(ttk.Label(card,text=Path(row['path']).name,wraplength=round(180*self.scale),style='Card.TLabel')).pack(fill='x')
            detail=f"{number(row['width'])}×{number(row['height'])} · "+('字节相同' if row['exact'] else f"相关分 {number(row['score'])}")
            if row.get('refined'):detail+=' · 已核对'
            ttk.Label(card,text=detail,style='CardMuted.TLabel').pack(anchor='w')
            parent=Path(row['path']).parent;caption=str(parent)
            for root in map(Path,self.data['roots']):
                if parent.is_relative_to(root):
                    relative=parent.relative_to(root);caption=root.name+('/'+str(relative) if str(relative)!='.' else '');break
            if len(caption)>64:caption=caption[:24]+'…'+caption[-36:]
            responsive_label(ttk.Label(card,text=caption,wraplength=round(180*self.scale),style='CardMuted.TLabel')).pack(fill='x')
            for w in (border,card,slot,label,*card.winfo_children()):w.bind('<MouseWheel>',self.wheel)
        self.layout_cards()
        self.page_text.set(f'{self.page+1} / {math.ceil(len(self.rows)/self.page_size)} 页');self.canvas.yview_moveto(0);self.update_selection()
    def poll_images(self):
        if self._closed:return
        handled=0
        for future,(generation,path,label) in list(self._thumbnail_jobs.items()):
            if not future.done():continue
            self._thumbnail_jobs.pop(future);handled+=1
            try:im,error=future.result()
            except Exception as exc:im,error=None,str(exc)
            if generation==self._generation and label.winfo_exists():
                if im is None:label.configure(text='缩略图不可用')
                else:
                    photo=ImageTk.PhotoImage(im,master=self.window);self._thumb_cache[path]=photo;self._thumb_cache.move_to_end(path)
                    label.configure(image=photo,text='');label.image=photo
                    while len(self._thumb_cache)>36:self._thumb_cache.popitem(last=False)
            if im is not None:im.close()
            if handled>=4:break
        for future,(win,label,path) in list(self._preview_jobs.items()):
            if not future.done():continue
            self._preview_jobs.pop(future)
            try:im,error=future.result()
            except Exception as exc:im,error=None,str(exc)
            if win.winfo_exists():
                if im is None:label.configure(text=error or '无法读取图像。')
                else:
                    photo=ImageTk.PhotoImage(im,master=win);label.configure(image=photo,text='');label.image=photo
            if im is not None:im.close()
        self.window.after(20,self.poll_images)
    def dispose(self,event):
        if event.widget is not self.window or self._closed:return
        self._closed=True
        for future in (*self._thumbnail_jobs,*self._preview_jobs):
            if not future.cancel() and future.done():
                try:
                    im,_=future.result()
                    if im is not None:im.close()
                except Exception:pass
        self._thumbnail_jobs.clear();self._preview_jobs.clear();self._thumb_cache.clear();self.photos=[]
        self._image_pool.shutdown(wait=False,cancel_futures=True)
    def click_later(self,row):
        if getattr(self,'click_id',None):self.window.after_cancel(self.click_id)
        self.click_id=self.window.after(260,lambda:self.toggle(row['path']))
    def enlarge(self,row):
        if getattr(self,'click_id',None):self.window.after_cancel(self.click_id);self.click_id=None
        win=tk.Toplevel(self.window);win.title(Path(row['path']).name)
        win.geometry(f'{min(round(700*self.scale),round(win.winfo_screenwidth()*.9))}x{min(round(600*self.scale),round(win.winfo_screenheight()*.86))}')
        label=tk.Label(win,text='正在读取图像…',font='BodyFont',bg=SURFACE);label.pack(fill='both',expand=True,padx=12,pady=12)
        from .search_render import decode_preview
        future=self._image_pool.submit(decode_preview,row['path'],(row['size'],row['mtime']));self._preview_jobs[future]=(win,label,row['path'])
        responsive_label(ttk.Label(win,text=row['path'])).pack(fill='x');ttk.Button(win,text='用默认程序打开原图',command=lambda:os.startfile(row['path'])).pack(pady=8)
    def copy(self):
        pane=self.pane
        if pane.running or not self.selected:return
        rows=[dict(r) for r in self.rows if r['path'] in self.selected];output=pane.output.get().strip()
        try:
            if not output:raise ValueError('请先选择复制输出目录。')
            from .engine import normalize_paths
            roots,out=normalize_paths(self.data['roots'],output);pane.app.check_conflicts(pane,roots,out,'copy')
        except (OSError,ValueError) as exc:messagebox.showerror('无法复制',str(exc),parent=self.window);return
        byte_count=sum(row.get('size',0) for row in rows)
        if not messagebox.askyesno('文件去向 · 复制所选图像',f'复制所选 {len(rows):,} 张图像，原文件合计 {number(byte_count/1_000_000)} MB。\n输出：{out}\n\n原图保留；已有兼容副本会复用，同名不同内容自动编号。复制记录保存在输出的 .photocleaner。是否开始？',parent=self.window):return
        retain_tree=pane.retain_tree.get()
        pane.active_roots=roots;pane.active_output=out;pane.pending_mode='copy';pane.stop.clear();pane.set_running(True)
        def work():
            try:pane.app.post(pane.task,{'kind':'copy_done',**copy_results(rows,roots,out,pane.stop,lambda e:pane.app.post(pane.task,e),retain_tree=retain_tree)})
            except Exception as exc:pane.app.post(pane.task,{'kind':'failure','message':str(exc)})
        pane.worker=threading.Thread(target=work,daemon=False);pane.worker.start()
    def close(self):
        if self.pane.running:self.note.set('请等待操作完成，或在主窗口暂停后关闭候选。');return
        if getattr(self,'click_id',None):self.window.after_cancel(self.click_id)
        self.window.destroy();self.pane.results_window=None

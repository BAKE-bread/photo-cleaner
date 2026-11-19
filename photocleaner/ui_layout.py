"""Compact workspace layout for independent task pages."""
from .numbers import number,input_float
from dataclasses import asdict
import tkinter as tk
from tkinter import ttk
from .config import Config
from .tasks import TaskConfig,TASK_NAMES
from .ui_theme import BG,SURFACE,INK,MUTED,ACCENT,LINE

SUBTITLES={
 'photo':'结合主体内容和画质选出实拍照片，可选择输出层级。',
 'people':'选出以人物、Cosplay 或现场活动为焦点的图像。',
 'creative':'选出漫画、绘画、平面设计、三维创作和软件截图。',
 'meme':'按反应、段子、梗图与贴纸用途筛选，清晰和包浆图均可。',
 'flatten':'汇集各层级文件，选择主文件和重复副本的去向。',
 'small':'将任意一边不超过尺寸阈值的图像移出。',
 'file_size':'按文件占用空间筛选图像，可组合多个体积条件。',
 'group':'按文件名规律归类，支持追加图像后再次分组。',
 'compress':'生成不缩放的 JPG 副本，再按需对比和回写。',
 'hash':'按 SHA256 查找相同文件，可原位保留、归集到输出或对照基准目录。',
 'repair':'检查 Windows 解码与文件名，补全后缀或生成兼容副本，原件保留。',
}

def build_task(pane,settings):
    p=pane;app=p.app;d=lambda v:round(v*app.dpi/96)
    # Header and primary actions stay visible when settings are scrolled.
    header=ttk.Frame(p.frame,padding=(d(24),d(20),d(24),d(14)))
    header.pack(fill='x',before=p.canvas.master)
    title=ttk.Frame(header);title.pack(fill='x')
    ttk.Label(title,text=TASK_NAMES[p.task],font='TitleFont').pack(side='left')
    ttk.Button(title,text='使用说明',style='Ghost.TButton',command=p.show_help).pack(side='right')
    ttk.Button(title,text='调试选图',style='Ghost.TButton',command=lambda:p.show_details(True)).pack(side='right',padx=(0,d(6)))
    ttk.Button(title,text='查看明细',style='Ghost.TButton',command=p.show_details).pack(side='right',padx=(0,d(6)))
    from .ui_text import Paragraph,responsive_label
    Paragraph(header,SUBTITLES[p.task],background=BG).pack(fill='x',pady=(d(7),0))
    names={'photo':'Qwen3-VL-2B（拍摄呈现 / 后期用途 / 局部遮挡复核）','people':'SigLIP 2 + CLIP → Qwen3-VL-2B；RT-DETR v2 R18（按需）','creative':'Qwen3-VL-2B（媒介/类别）+ CLIP ViT-B/32（反应图佐证）','meme':'CLIP ViT-B/32 + Qwen3-VL-2B（双证据表情包判断）'}
    if p.task in names:Paragraph(header,'模型：'+names[p.task],background=BG).pack(fill='x',pady=(d(4),0))
    p.rule=tk.StringVar(app.root,value=SUBTITLES[p.task])
    p.body.configure(padding=(d(24),0,d(24),d(14)))
    p.canvas.configure(background=BG)
    p.cards=[]
    def card(title,detail=None):
        border=tk.Frame(p.body,background=LINE,bd=0);border.pack(fill='x',pady=(0,d(14)))
        frame=ttk.Frame(border,style='Card.TFrame',padding=d(16));frame.pack(fill='both',expand=True,padx=1,pady=1)
        head=ttk.Frame(frame,style='Card.TFrame');head.pack(fill='x',pady=(0,d(10)))
        ttk.Label(head,text=title,style='CardTitle.TLabel').pack(side='left')
        if detail:ttk.Label(head,text=detail,style='CardMuted.TLabel').pack(side='right')
        p.cards.append(border)
        return frame,head
    source,head=card('输入目录','递归扫描 · 支持多个目录')
    rows=ttk.Frame(source,style='Card.TFrame');rows.pack(fill='x')
    p.input_list=tk.Listbox(rows,height=3,selectmode='extended',font='BodyFont',bg='#f8f9fc',fg=INK,
                           selectbackground='#e0daf5',selectforeground=INK,bd=0,highlightthickness=1,highlightbackground=LINE,
                           activestyle='none',exportselection=False)
    p.input_list.grid(row=0,column=0,sticky='nsew');rows.columnconfigure(0,weight=1)
    from .ui_scroll import AutoScrollbar
    scroll=AutoScrollbar(rows,command=p.input_list.yview);scroll.grid(row=0,column=1,sticky='ns')
    horizontal=AutoScrollbar(rows,orient='horizontal',command=p.input_list.xview);horizontal.grid(row=1,column=0,sticky='ew')
    p.input_list.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
    for path in settings.get('inputs',[]):p.input_list.insert('end',path)
    bar=ttk.Frame(source,style='Card.TFrame');bar.pack(fill='x',pady=(d(10),0))
    for label,command in [('添加目录',p._add),('移除',p._remove),('复制',p.copy_inputs),('粘贴追加',p.paste_inputs)]:
        button=ttk.Button(bar,text=label,style='Ghost.TButton',command=command);button.pack(side='left',padx=(0,d(6)))
        if label!='复制':p.fields.append(button)
    p.fields.append(p.input_list)
    p.input_list.bind('<Control-c>',lambda _:p.copy_inputs());p.input_list.bind('<Control-v>',lambda _:p.paste_inputs())
    if p.task!='group':
        p.output_area=ttk.Frame(source,style='Card.TFrame');p.output_area.pack(fill='x')
        ttk.Separator(p.output_area).pack(fill='x',pady=d(12))
        ttk.Label(p.output_area,text='输出目录（归集模式使用，可与输入重合）' if p.task=='hash' else '输出目录（仅预览时可留空，执行前填写）',style='CardMuted.TLabel').pack(anchor='w',pady=(0,d(6)))
        output=ttk.Frame(p.output_area,style='Card.TFrame');output.pack(fill='x')
        entry=ttk.Entry(output,textvariable=p.output);entry.pack(side='left',fill='x',expand=True)
        from .directory_lists import path_entry
        path_entry(entry,p.output)
        browse=ttk.Button(output,text='选择',command=p._browse);browse.pack(side='right',padx=(d(8),0));p.fields.extend([entry,browse])
    else:ttk.Label(source,text='分组位置：图像所在目录的来源子文件夹',style='CardMuted.TLabel').pack(anchor='w',pady=(d(10),0))
    defaults=asdict(Config()) if p.photo_task else asdict(TaskConfig(task=p.task))
    from .defaults import processing_defaults,models_cached
    try:defaults.update(processing_defaults(p.task))
    except ValueError as exc:p.status.set(str(exc))
    previous=dict(settings.get('config',{}))
    if p.task=='meme' and 'min_side' not in previous:defaults['min_side']=0
    if p.task=='people' and previous.get('people_policy')!=Config().people_policy:
        if 'people_policy' not in previous:previous.pop('people_threshold',None)
        if previous:p.status.set('人物策略已升级 · 保留常规门槛，增加独立类型/人体定位补判')
    if p.photo_task:previous['people_policy']=Config().people_policy
    if p.task=='photo' and previous.get('photo_policy')!='purpose-v7-pbdb':
        for key in ('sharpness_threshold','sharpness_mid_threshold','reblur_threshold','detail_threshold','quality_mode','focus_reblur_threshold','pbdb_threshold','focus_sharpness_threshold'):previous.pop(key,None)
        p.status.set('实拍用途与细节尺度已升级；首次使用新条件，原记录保留。')
    defaults.update(previous)
    if p.photo_task and models_cached(p.task,defaults['model_dir']):defaults['offline']=True
    if p.task=='creative':defaults['include_software']=True
    if p.photo_task:defaults['scheme']=p.task
    p.defaults=defaults;p.casts={};p.condition_labels={}
    if p.photo_task:p.vars['scheme']=tk.StringVar(app.root,value=defaults['scheme']);p.casts['scheme']=str
    specs={
     'photo':[('photo_threshold','照片分数下限',float),('meme_threshold','后期反应/文字分上限',float),('min_side','宽和高下限（px）',int),('focus_sharpness_threshold','细节加权清晰度下限',float),('focus_reblur_threshold','细节加权再模糊上限',float)],
     'people':[('min_side','宽和高都大于（px）',int),('people_threshold','常规初判分数 ≥',float),('people_verify_threshold','常规复核分数 ≥',float)],
     'creative':[('min_side','常规尺寸分界（px）',int),('creative_threshold','创作总分 ≥',float)],
     'meme':[('min_side','宽和高都大于（px；0 不限）',int),('meme_select_threshold','表情包入选分数 ≥',float)],
     'flatten':[],
     'hash':[],
     'repair':[],
     'small':[('min_side','任意一边 ≤（px）',int)],
     'file_size':[],
     'group':[('rare_count','合入其他：数量 <',int),('rare_ratio','合入其他：比例 <（0–1）',float)],
     'compress':[('min_mb','文件体积 >（MB）',float),('min_mp','图像像素 ≥（MP）',float),('max_aspect','长短边比 ≤',str),('quality','JPG 质量',int)],
    }[p.task]
    form,head=card('处理条件','满足条件的图像将剪切' if p.task in ('photo','people','creative','meme','small','file_size') else '仅生成副本，原图保留' if p.task in ('compress','repair') else None)
    if p.task=='hash':
        p.vars['hash_mode']=tk.StringVar(app.root,value=defaults.get('hash_mode','consolidate'));p.casts['hash_mode']=str
        p.vars['std_path']=tk.StringVar(app.root,value=defaults.get('std_path',''));p.casts['std_path']=str
        for label,value in [('原位去重：保留文件维持原路径，多余副本回收','inplace'),('归集去重：输出优先保留一份，其余副本回收','consolidate'),('基准去重：只回收其他目录中的相同图像','std')]:
            b=ttk.Radiobutton(form,text=label,variable=p.vars['hash_mode'],value=value);b.pack(anchor='w',pady=d(3));p.fields.append(b)
        std_controls=ttk.Frame(source,style='Card.TFrame');p.std_controls=std_controls
        marker=ttk.Button(std_controls,text='设为基准目录',style='Ghost.TButton');marker.pack(anchor='w',pady=(d(8),d(4)));p.fields.append(marker)
        p.std_caption=tk.StringVar(app.root)
        responsive_label(ttk.Label(std_controls,textvariable=p.std_caption,style='CardMuted.TLabel')).pack(fill='x')
        def mark_std():
            selected=p.input_list.curselection()
            if len(selected)!=1:p.status.set('请先选中一个输入目录，再设为基准目录。');return
            p.vars['std_path'].set(p.input_list.get(selected[0]));p.app.save()
        marker.configure(command=mark_std);p.mark_std=mark_std
        priority_controls=ttk.Frame(source,style='Card.TFrame');p.priority_controls=priority_controls
        priority_bar=ttk.Frame(priority_controls,style='Card.TFrame');priority_bar.pack(fill='x',pady=(d(8),d(4)))
        def move_priority(direction):
            if p.running:return
            values=list(p.input_list.get(0,'end'));selected=set(p.input_list.curselection())
            for i in sorted(selected,reverse=direction>0):
                target=i+direction
                if 0<=target<len(values) and target not in selected:
                    values[i],values[target]=values[target],values[i];selected.remove(i);selected.add(target)
            p.input_list.delete(0,'end')
            for value in values:p.input_list.insert('end',value)
            for i in selected:p.input_list.selection_set(i)
            if selected:p.input_list.see(min(selected))
            sync_hash_mode();p.app.save()
        p.move_priority=move_priority
        for label,direction in [('提高优先级',-1),('降低优先级',1)]:
            b=ttk.Button(priority_bar,text=label,style='Ghost.TButton',command=lambda step=direction:move_priority(step));b.pack(side='left',padx=(0,d(8)));p.fields.append(b)
        Paragraph(priority_controls,'上方目录优先保留，子目录继承优先级。同一目录内优先保留路径较短的文件，再按文件名选择。').pack(fill='x')
        def sync_hash_mode(*_):
            mode=p.vars['hash_mode'].get();std_mode=mode=='std'
            p.output_area.pack_forget();std_controls.pack_forget();priority_controls.pack_forget()
            if std_mode:std_controls.pack(fill='x')
            elif mode=='inplace':priority_controls.pack(fill='x')
            else:p.output_area.pack(fill='x')
            std=p.vars['std_path'].get()
            p.std_caption.set('基准目录：'+std if std else '尚未设置基准目录；此目录及其子目录只读。')
            for i,path in enumerate(p.input_list.get(0,'end')):p.input_list.itemconfigure(i,background='#eee9fc' if std_mode and path==std else '#f8f9fc',foreground=ACCENT if std_mode and path==std else INK)
            if hasattr(p,'execute_btn'):p.execute_btn.configure(text='回收重复副本' if mode=='inplace' else '回收匹配副本' if std_mode else '归集并去重')
            if hasattr(p,'stat_labels'):
                signature=(mode,std,tuple(p.input_list.get(0,'end')))
                if getattr(p,'_hash_display_signature',signature)!=signature:
                    for var in p.stat_values:var.set('—')
                p._hash_display_signature=signature
                labels=('已发现','原位保留','待回收副本','已回收副本') if mode=='inplace' else ('基准图像','其他图像','待回收副本','已回收副本') if std_mode else ('已发现','待处理','代表已移动','副本已回收')
                for var,label in zip(p.stat_labels,labels):var.set(label)
            p.canvas.after_idle(lambda:getattr(p.canvas,'request_fit',lambda:None)())
        p.vars['hash_mode'].trace_add('write',sync_hash_mode);p.vars['std_path'].trace_add('write',sync_hash_mode);p.sync_hash_mode=sync_hash_mode
    if p.task=='people':
        presets=ttk.Frame(form,style='Card.TFrame');presets.pack(fill='x',pady=(0,d(10)))
        for label,values in [('优先少误收',(.97,.95)),('均衡',(.85,.80))]:
            button=ttk.Button(presets,text=label,style='Ghost.TButton',command=lambda v=values:p._preset(v))
            button.pack(side='left',padx=(0,d(6)));p.preset_buttons.append(button);p.fields.append(button)
        ttk.Label(presets,text='严格门槛减少误收，也会增加漏选',style='CardMuted.TLabel').pack(side='left')
    def reload_defaults():
        try:
            from .defaults_undo import load
            load(p,processing_defaults(p.task));p.status.set('已载入默认条件：'+str(__import__('photocleaner.defaults',fromlist=['DIRECTORY']).DIRECTORY/(p.task+'.json')))
        except ValueError as exc:p.status.set(str(exc))
    p.reload_defaults=reload_defaults
    reload_button=ttk.Button(head,text='载入默认条件',style='Ghost.TButton',command=reload_defaults);reload_button.pack(side='right');p.fields.append(reload_button)
    grid=ttk.Frame(form,style='Card.TFrame');grid.pack(fill='x')
    def field(parent,n,key,label,cast):
        row,col=divmod(n,2);col*=2
        title=ttk.Label(parent,text=label,style='Card.TLabel');title.grid(row=row,column=col,sticky='w',padx=(0,d(10)),pady=d(6))
        p.condition_labels[key]=(title,label)
        from .numeric_ui import NumericVar,bind_numeric
        var=NumericVar(app.root,defaults[key],integer=cast is int) if cast in (float,int) else tk.StringVar(app.root,value=str(defaults[key]));p.vars[key]=var;p.casts[key]=cast
        if key=='max_aspect':
            from .desktop import aspect_value
            p.casts[key]=aspect_value
            if defaults[key]==16/9:var.set('16:9')
        if key=='device':widget=ttk.Combobox(parent,textvariable=var,values=['auto','cuda','cpu'],state='readonly',width=12)
        elif key=='batch_size':widget=ttk.Combobox(parent,textvariable=var,values=[4,8,16,32,64,128,256,512,1024,2048,4096],width=12)
        else:widget=ttk.Entry(parent,textvariable=var,width=12)
        if isinstance(var,NumericVar):bind_numeric(widget,var)
        widget.grid(row=row,column=col+1,sticky='ew',padx=(0,d(18) if col==0 else 0),pady=d(6));p.fields.append(widget);p.config_widgets[key]=widget
        parent.columnconfigure(1,weight=1);parent.columnconfigure(3,weight=1)
    for n,(key,label,cast) in enumerate(specs):field(grid,n,key,label,cast)
    if p.task=='file_size':
        ttk.Label(form,text='组合条件',style='Card.TLabel').pack(anchor='w')
        p.vars['file_size_rules']=tk.StringVar(app.root,value=defaults['file_size_rules']);p.casts['file_size_rules']=str
        rules=ttk.Entry(form,textvariable=p.vars['file_size_rules']);rules.pack(fill='x',pady=(d(5),d(6)));p.fields.append(rules);p.config_widgets['file_size_rules']=rules
        Paragraph(form,'例如 <400 KB OR >4 MB；区间可写 >1 MB AND <4 MB。KB / MB 为十进制，等号统一由左栏开关控制。').pack(fill='x')
        p.size_rule_feedback=tk.StringVar(app.root)
        def explain_size_rules(*args):
            try:
                from .size_rules import parse_size_rules
                p.size_rule_feedback.set('当前条件：'+parse_size_rules(p.vars['file_size_rules'].get(),app.inclusive.get()).description)
            except ValueError as exc:p.size_rule_feedback.set('请调整条件：'+str(exc))
        responsive_label(ttk.Label(form,textvariable=p.size_rule_feedback,style='CardMuted.TLabel')).pack(fill='x',pady=(d(5),0))
        p.vars['file_size_rules'].trace_add('write',explain_size_rules);p.refresh_size_rules=explain_size_rules;explain_size_rules()
    if p.task=='photo':
        optional=ttk.Frame(form,style='Card.TFrame');optional.pack(fill='x',pady=(d(6),0))
        extra_quality=ttk.Frame(form,style='Card.TFrame');p.quality_options=extra_quality
        button=ttk.Button(optional,text='更多画质条件',style='Ghost.TButton');button.pack(side='left')
        ttk.Label(optional,text='按需限制；下限 0、再模糊上限 1 表示不限',style='CardMuted.TLabel').pack(side='left',padx=d(8))
        def toggle_quality():
            if extra_quality.winfo_manager():extra_quality.pack_forget();button.configure(text='更多画质条件')
            else:extra_quality.pack(fill='x',after=optional);button.configure(text='收起画质条件')
            p.canvas.after_idle(lambda:getattr(p.canvas,'request_fit',lambda:None)())
        button.configure(command=toggle_quality);p.toggle_quality=toggle_quality
        for n,(key,label) in enumerate([('sharpness_threshold','局部清晰度下限'),('pbdb_threshold','分块锐度 PBDB 下限'),('reblur_threshold','全图再模糊上限'),('sharpness_mid_threshold','中位细节下限'),('blockiness_threshold','8px 网格指标上限')]):field(extra_quality,n,key,label,float)
    if p.task=='people':Paragraph(form,'常规筛选需同时达到两道分数门槛；开启补判时，漏选候选可凭独立类型/人体定位证据入选。可在高级设置关闭补判。').pack(fill='x',pady=(d(6),0))
    def flag(parent,key,label):
        var=tk.BooleanVar(app.root,value=defaults[key]);p.vars[key]=var
        widget=ttk.Checkbutton(parent,text=label,variable=var);widget.pack(anchor='w');p.fields.append(widget);p.config_widgets[key]=widget
        if key=='auto_batch':var.trace_add('write',lambda *_:p._sync_batch());p._sync_batch()
    if p.task=='flatten':
        for key in ('dedup','backup_duplicates'):
            p.vars[key]=tk.BooleanVar(app.root,value=defaults[key]);p.casts[key]=bool
        policies={'all':('全部文件移到输出根目录',False,False),
            'unique':('每种内容移一份，重复副本留在原处',True,False),
            'separate':('每种内容移一份，重复副本移入“重复副本备份”子目录',True,True)}
        p.flat_policy=tk.StringVar(app.root);p.flat_policy_buttons={}
        def choose_policy():
            _,dedup,backup=policies[p.flat_policy.get()]
            p.vars['dedup'].set(dedup);p.vars['backup_duplicates'].set(backup)
        for key,(label,_,_) in policies.items():
            b=ttk.Radiobutton(form,text=label,variable=p.flat_policy,value=key,command=choose_policy);b.pack(anchor='w',pady=d(3));p.fields.append(b);p.flat_policy_buttons[key]=b
        def sync_duplicates(*args):
            p.flat_policy.set('all' if not p.vars['dedup'].get() else 'separate' if p.vars['backup_duplicates'].get() else 'unique')
        for key in ('dedup','backup_duplicates'):p.vars[key].trace_add('write',sync_duplicates)
        sync_duplicates();p.sync_duplicates=sync_duplicates
        Paragraph(form,'已有输出中的同内容文件优先作为主文件。同名而内容不同的文件会自动编号，分别保留。').pack(fill='x',pady=(d(6),0))
    if p.task in ('photo','people','meme','small','file_size','compress','repair'):flag(form,'retain_tree','输出保留来源层级（不勾选则扁平输出）')
    if p.photo_task or p.task in ('small','file_size'):
        flag(form,'backup_duplicates','符合本页筛选条件的重复副本，也剪切到输出的“重复副本备份”')
    if p.task=='repair':
        flag(form,'repair_all','为全部输入图像生成 PNG 副本（包含子目录，原件保留）')
        Paragraph(form,'勾选后，输入列表中所有目录及子目录内的可解码静态图像均纳入生成，仍遵守像素上限。明细中的调试勾选只用于复制测试样本。').pack(fill='x',pady=(d(4),d(4)))
        Paragraph(form,'先检查 Windows 系统实际解码结果。系统可读且后缀可用的图像保留，即使后缀与编码不同；缺少可用后缀时补全，系统解码失败时重新编码。原件保留。').pack(fill='x',pady=(d(6),0))
    if p.task=='creative':
        from .ui_text import responsive_label
        Paragraph(form,'包括软件截图、小尺寸创作图和 GIF；按主题输出时各有对应分组。').pack(fill='x',pady=(0,d(8)))
        ttk.Separator(form).pack(fill='x',pady=d(10))
        ttk.Label(form,text='输出方式（两项互斥；均不勾选则扁平输出）',style='CardMuted.TLabel').pack(anchor='w')
        p.vars['creative_layout']=tk.StringVar(app.root,value=defaults['creative_layout'])
        layouts=ttk.Frame(form,style='Card.TFrame');layouts.pack(fill='x',pady=(d(6),0))
        p.layout_checks={};p.layout_buttons={}
        def sync_layout(*args):
            for key,var in p.layout_checks.items():var.set(p.vars['creative_layout'].get()==key)
        def choose_layout(key):p.vars['creative_layout'].set(key if p.layout_checks[key].get() else 'flat')
        for label,value in [('输出保留来源层级','tree'),('按内容主题分组','theme')]:
            var=tk.BooleanVar(app.root,value=defaults['creative_layout']==value);p.layout_checks[value]=var
            button=ttk.Checkbutton(layouts,text=label,variable=var,command=lambda key=value:choose_layout(key));button.pack(side='left',padx=(0,d(14)));p.fields.append(button);p.layout_buttons[value]=button
        p.vars['creative_layout'].trace_add('write',sync_layout)
    advanced,head=card('高级设置')
    p.advanced_body=ttk.Frame(advanced,style='Card.TFrame')
    toggle=ttk.Button(head,text='展开',style='Ghost.TButton');toggle.pack(side='right')
    def expand():
        if p.advanced_body.winfo_manager():p.advanced_body.pack_forget();toggle.configure(text='展开')
        else:p.advanced_body.pack(fill='x');toggle.configure(text='收起')
        p.canvas.after_idle(lambda:getattr(p.canvas,'request_fit',lambda:None)())
    toggle.configure(command=expand);p.toggle_advanced=expand
    extra=[]
    if p.photo_task:extra=[('device','推理设备',str),('batch_size','手动推理批量上限',int),('workers','CPU 解码线程',int),('max_pixels','完整解码像素上限',int)]
    elif p.task in ('compress','repair'):extra=[('max_pixels','单图像素上限',int)]
    extra_grid=ttk.Frame(p.advanced_body,style='Card.TFrame');extra_grid.pack(fill='x')
    for n,(key,label,cast) in enumerate(extra):field(extra_grid,n,key,label,cast)
    if p.photo_task:
        flag(p.advanced_body,'auto_batch','自动实测并记住本设备的推理批量（推荐）')
        detail='首次使用按设备和功能短测，之后复用。关闭自动模式可设手动上限；显存不足会减半重试。CPU 线程用于读取和解码。'
        Paragraph(p.advanced_body,detail).pack(fill='x',pady=(d(4),d(6)))
        flag(p.advanced_body,'offline','离线使用已缓存模型')
    if p.task=='people':
        flag(p.advanced_body,'people_rescue','启用类型与人体定位补判，减少人像漏选')
        flag(p.advanced_body,'nearby','启用时间邻域弱辅助')
    if p.task not in ('flatten','hash','repair'):flag(p.advanced_body,'sniff_all','尝试读取非图像后缀的文件')
    if p.task=='hash':p.cards[-1].pack_forget()
    if not extra and p.task=='flatten':ttk.Label(p.advanced_body,text='编号重复副本清理可在“更多操作”中打开。',style='CardMuted.TLabel').pack(anchor='w')
    # Pinned footer: progress, compact statistics and two primary actions.
    footer=ttk.Frame(p.frame,padding=(d(24),d(12),d(24),d(16)));footer.pack(fill='x')
    p.progress=ttk.Progressbar(footer);p.progress.pack(fill='x',pady=(0,d(10)))
    stats=ttk.Frame(footer);stats.pack(fill='x',pady=(0,d(10)))
    p.stat_values=[];p.stat_labels=[]
    initial=('目标分组','待归位','已归位','已在正确组') if p.task=='group' else ('已发现','待修复','兼容副本','无需修复/跳过') if p.task=='repair' else ('已发现','待压缩','JPG 副本','跳过') if p.task=='compress' else ('已发现','待处理','代表已移动','副本已回收') if p.task=='hash' else ('已发现','待剪切','已剪切','原地保留')
    for label in initial:
        cell=ttk.Frame(stats,style='Card.TFrame',padding=(d(12),d(6)));cell.pack(side='left',fill='x',expand=True,padx=(0,d(6)))
        value=tk.StringVar(app.root,value='—');name=tk.StringVar(app.root,value=label);p.stat_values.append(value);p.stat_labels.append(name)
        ttk.Label(cell,textvariable=value,style='Number.TLabel').pack(anchor='w')
        ttk.Label(cell,textvariable=name,style='CardMuted.TLabel').pack(anchor='w')
    p.device_text=tk.StringVar(app.root,value='设备：开始筛选后自动检测' if p.photo_task else '')
    p.eta_text=tk.StringVar(app.root,value='预计耗时：开始后显示')
    if p.photo_task:responsive_label(ttk.Label(footer,textvariable=p.device_text,style='Muted.TLabel')).pack(fill='x')
    responsive_label(ttk.Label(footer,textvariable=p.eta_text,style='Muted.TLabel')).pack(fill='x',pady=(0,d(5)))
    responsive_label(ttk.Label(footer,textvariable=p.status,style='Muted.TLabel')).pack(fill='x',pady=(0,d(10)))
    actions=ttk.Frame(footer);actions.pack(fill='x')
    p.preview_btn=ttk.Button(actions,text='预览',style='Primary.TButton',command=lambda:p.start('preview'));p.preview_btn.pack(side='left',padx=(0,d(8)))
    verb='生成兼容副本' if p.task=='repair' else '生成 JPG 副本' if p.task=='compress' else '执行分组' if p.task=='group' else '执行剪切'
    p.execute_btn=ttk.Button(actions,text=verb,command=lambda:p.start('move'));p.execute_btn.pack(side='left',padx=(0,d(8)))
    ttk.Button(actions,text='文件去向',command=p.show_plan).pack(side='left',padx=(0,d(8)))
    p.pause_btn=ttk.Button(actions,text='暂停',command=p.pause,state='disabled');p.pause_btn.pack(side='left',padx=(0,d(8)))
    p.resume_btn=ttk.Button(actions,text='继续执行',command=lambda:p.start(p.pending_mode))
    # Preview itself resumes saved progress. Only paused execution needs a
    # separate action, because preview and execution have different effects.
    if p.last_result and p.last_result.get('stopped') and p.pending_mode!='preview':p.resume_btn.pack(side='left')
    more=ttk.Button(actions,text='更多操作',style='Ghost.TButton');more.pack(side='right')
    p.more_frame=ttk.Frame(footer);p.more_frame.pack_forget()
    def more_ops():
        if p.more_frame.winfo_manager():p.more_frame.pack_forget();more.configure(text='更多操作')
        else:p.more_frame.pack(fill='x',pady=(d(8),0));more.configure(text='收起操作')
    more.configure(command=more_ops);p.toggle_more=more_ops
    p.scan_btn=ttk.Button(p.more_frame,text='重新扫描预览',command=lambda:p.start('preview',False));p.scan_btn.pack(side='left',padx=(0,d(6)))
    p.report_btn=ttk.Button(p.more_frame,text='打开报告',command=p.report,state='normal' if p.last_report else 'disabled');p.report_btn.pack(side='left',padx=(0,d(6)))
    p.restore_btn=ttk.Button(p.more_frame,text='恢复上次分组' if p.task=='group' else '恢复上次剪切',command=p.restore,state='normal' if p.last_run and p.task!='compress' else 'disabled')
    if p.task not in ('compress','hash','repair'):p.restore_btn.pack(side='left',padx=(0,d(6)))
    if p.task in ('compress','flatten'):
        review=ttk.Button(p.more_frame,text='压缩对比与回写' if p.task=='compress' else '清理编号副本',command=p.open_review)
        review.pack(side='left');p.fields.append(review)
    p.more_frame.bind('<Configure>',lambda _:p.canvas.configure(scrollregion=p.canvas.bbox('all')))
    if hasattr(p,'sync_hash_mode'):p.sync_hash_mode()
    from .defaults_undo import install
    install(p)

def update_stats(p,event,exact=False):
    c=event['counts'];task=p.task
    found=event.get('scanned',p.found_count);selected=c.get('selected',0);done=c.get('moved',0);retained=c.get('rejected',0)
    labels=['已发现','待剪切','已剪切','原地保留'];values=[found,selected,done,retained]
    detail=''
    if task=='compress':labels=['已发现','待压缩','JPG 副本','跳过'];values=[found,selected,c.get('copied',0),retained];detail='原图保留'
    elif task=='repair':labels=['已发现','待修复','兼容副本','无需修复/跳过'];values=[found,selected,c.get('copied',0),retained];detail='原文件保留，仅在输出生成兼容副本'
    elif task=='hash':
        if event.get('hash_mode')=='inplace':labels=['已发现','原位保留','待回收副本','已回收副本'];values=[found,retained,selected,c.get('recycled',0)];detail=f"{number(event.get('duplicate_groups',0))} 组重复内容 · 保留文件和目录结构维持原位"
        elif event.get('hash_mode')=='std':labels=['基准图像','其他图像','待回收副本','已回收副本'];values=[event.get('std_count',0),event.get('comparison_count',0),selected,c.get('recycled',0)];detail='基准目录只读，其他未匹配图像留在原处'
        else:labels=['已发现','待处理','代表已移动','副本已回收'];values=[found,selected,done,c.get('recycled',0)];detail='重复副本只有在输出代表文件校验成功后才回收'
        breakdown=event.get('retained_counts')
        if breakdown and (event.get('hash_mode')=='std' or not event.get('output_is_input')):detail+=f" · 基准/输出已有 {breakdown.get('existing',0):,} · 其他输入保留 {breakdown.get('rejected',0):,}"
    elif task=='group':
        g=event.get('grouping',{});labels=['目标分组','待归位','已归位','已在正确组'];values=[g.get('groups',0),selected,done,c.get('grouped',0)]
        detail=f"{number(g.get('methods',0))} 种命名规律 · {number(g.get('other_images',0))} 张合入其他" if event.get('group_plan_complete') is not False else '分类统计尚未完成'
    for v,n,value,label in zip(p.stat_values,p.stat_labels,values,labels):v.set(format(int(value),',') if exact else number(value,group=True));n.set(label)
    return detail

def result_summary(p,event):
    c=event['counts']
    detail=update_stats(p,event,exact=True)
    state='已暂停，可继续任务' if event.get('stopped') else '未完成，请查看运行记录' if event.get('fatal_error') else '预览完成' if event['mode']=='preview' else '执行完成'
    faults=c.get('error',0)+c.get('review',0)
    if faults:detail+=( ' · ' if detail else '')+f'{number(faults)} 个文件需核对，现有副本保留'
    if c.get('queued',0):detail+=( ' · ' if detail else '')+f"{number(c['queued'],group=True)} 个尚未判定"
    return state+(' · '+detail if detail else '')

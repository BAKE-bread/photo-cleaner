"""Tk task controllers: capture settings on the UI thread, run journals on workers.

Execution is preceded by a reviewable plan. UI views and gate comparisons are
read-only consumers; file changes remain in the existing task runners.
"""
from dataclasses import asdict
from contextlib import ExitStack
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog,messagebox,ttk,font

from .config import Config,PROJECT
from .dpi import enable_dpi,window_dpi
from .engine import Engine,normalize_paths
from .moving import restore_moves
from .storage import Store,output_lock
from .tasks import TaskConfig,TaskRunner,TASK_NAMES
from .actions import SafeActions
from .messages import completion_text
from .directory_lists import pasted_directories,clean_path
from .numbers import number,input_float,input_int

RULES={
 'hash':'递归按 SHA256 文件字节去重。优先保留输出已有的一份；没有则移动一份到输出。同哈希多余副本经核验进入回收站。输出可为一个输入目录；不判断图像内容。',
 'photo':'Qwen3-VL 分开核对拍摄呈现和后期反应用途，局部隐私遮挡/边框及实体纸张按需复核；内容通过后再检查独立画质条件。PBDB 侧重局部强细节，旧拟合综合分默认停用。高分不能覆盖类别排除。',
 'flatten':'把各层级的所有普通文件剪切到输出目录根层。去重开启时，完全相同副本默认留原处；勾选备份选项可把多余副本也移到输出的“重复副本备份”目录，便于清空源目录中的文件。同名不同内容自动编号，绝不覆盖。此功能不检查照片、清晰度或尺寸。',
 'small':'宽不大于尺寸阈值，或高不大于尺寸阈值，即剪切。只看图像尺寸，不调用视觉模型，也不检查清晰度；包括满足尺寸条件的动图。其他文件留在原处。',
 'group':'按文件名形状在各原目录分组。再次运行会合并已有程序分组与新增图像重新计数，包括从“其他”升为独立组；正确归位的图像不移动。普通子目录保留。每个原目录独立统计，少于数量或比例阈值的类别合入“其他”。',
 'compress':'文件体积大于阈值且像素数至少达到阈值，才生成 JPG 副本。长短边比超过上限时跳过。不缩放；EXIF 转正，透明区域填白，动图跳过；MPO/JPEG 取主图。生成阶段保留原图，单输入目录可再打开对比列表选择回写。',
}
RULES['people']='人物活动 / Cosplay / Live：按人物内容分数筛选，尺寸和安全 I/O 沿用照片筛选；允许人物截图和印刷照片。'
RULES['creative']='选出漫画、动画帧、绘画草图、平面设计、导视地图和三维渲染。排除实拍照片及照片拼图、低清表情包和混合表情包。全屏软件/网页/游戏界面截图默认纳入。小尺寸达标分流 07，可读 GIF 分流 08，高创作证据且非实拍的剩余候选分流 09；不套用实拍画质门槛。'
RULES['meme']='按反应表达、梗图改图、段子或贴纸用途筛选表情包；清晰、大尺寸或包浆图都可入选，不凭尺寸或清晰度认定。默认不限制最短边，动图跳过。普通插画、漫画、照片、界面和文字帖子不会只因可爱、配字或模糊而入选。'
RULES['file_size']='按文件占用的字节数筛选图像，组合条件支持 AND（且）、OR（或）和括号。KB / MB 使用十进制；是否包含等号由左栏统一开关决定。包括动图，不调用模型。'

def aspect_value(value):
    if ':' in value:
        width,height=map(input_float,value.split(':'))
        if height<=0:raise ValueError('比例分母必须为正数。')
        return input_float(width/height)
    return input_float(value)


class TaskPane:
    def __init__(self,app,task,settings):
        self.app=app;self.task=task;self.running=False;self.last_run=settings.get('last_run');self.last_report=settings.get('last_report')
        self.photo_task=task in ('photo','people','creative','meme');self.preset_buttons=[]
        self.stop=threading.Event();self.worker=None;self.review_window=None;self.pending_mode=settings.get('pending_mode','preview');self.active_roots=[];self.active_output=None
        self.preview_database=settings.get('preview_database')
        self.last_result=settings.get('last_result');self.debug_output=settings.get('debug_output','')
        self.result_columns=settings.get('result_columns',[])
        self.frame=ttk.Frame(app.notebook);app.notebook.add(self.frame,text=TASK_NAMES[task])
        canvas_frame=ttk.Frame(self.frame);canvas_frame.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(canvas_frame,highlightthickness=0)
        from .ui_scroll import AutoScrollbar,fit_canvas
        scroll=AutoScrollbar(canvas_frame,command=self.canvas.yview);self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.canvas.pack(side='left',fill='both',expand=True)
        self.body=ttk.Frame(self.canvas,padding=12)
        self.window=self.canvas.create_window((0,0),window=self.body,anchor='nw')
        self.vars={};self.fields=[];self.config_widgets={};self.output=tk.StringVar(app.root,value=settings.get('output',''))
        self.found_count=0
        self.status=tk.StringVar(app.root,value='就绪 · 先预览，再决定执行');self._build(settings)
        fit_canvas(self.canvas,self.body,self.window,self.input_list,app.dpi/96)
        if not self.last_result and self.last_report:
            try:
                import sqlite3
                dbpath=Path(self.last_report).parent/'state.sqlite3'
                with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True) as db:
                    row=db.execute('SELECT id,mode,summary,config FROM runs WHERE id=?',(Path(self.last_report).stem.removeprefix('report_'),)).fetchone()
                    if row:self.last_result={**json.loads(row[2] or '{}'),'run_id':row[0],'mode':row[1],'config':json.loads(row[3]),'report':self.last_report}
            except (OSError,ValueError,sqlite3.Error):pass

    def _build(self,settings):
        from .ui_layout import build_task
        build_task(self,settings)

    def show_help(self):
        from .help_ui import show_help
        return show_help(self,TASK_NAMES.get(self.task,'本功能')+'：添加输入目录，先预览明细，再选择处理方式执行。')

    def _preset(self,values):
        keys=('people_threshold','people_verify_threshold') if self.task=='people' else ('photo_threshold','sharpness_threshold','blockiness_threshold','meme_threshold')
        for key,value in zip(keys,values):self.vars[key].set(number(input_float(value)))
        if self.task=='photo':
            for key,value in [('sharpness_mid_threshold',0),('reblur_threshold',1),('detail_threshold',0),('focus_reblur_threshold',.50),('focus_sharpness_threshold',25),('pbdb_threshold',0)]:
                if key in self.vars:self.vars[key].set(value)
    def _scheme_changed(self):
        self._sync_batch()
        people=self.task=='people'
        if self.task in ('creative','meme'):self.rule.set(RULES[self.task]);return
        self.rule.set('人物方案：筛选以人物及其活动为焦点的肖像、Cosplay、Kigurumi、舞台表演、粉丝活动、小范围庆祝，以及人物截图/印刷照片。排除更像会场/街道环境、人群、玩偶立牌或手持物品的内容。共用尺寸、动图和解码上限；不套用标准照片、表情包或画质阈值。' if people else RULES['photo'])
        for key in ('photo_threshold','sharpness_threshold','blockiness_threshold','meme_threshold'):
            if key in self.config_widgets:self.config_widgets[key].configure(state='disabled' if people or self.running else 'normal')
        for key in ('people_threshold','people_verify_threshold','nearby'):
            if key in self.config_widgets:self.config_widgets[key].configure(state='normal' if people and not self.running else 'disabled')
        for button in self.preset_buttons:button.configure(state='disabled' if self.running else 'normal')
    def _sync_batch(self):
        widget=self.config_widgets.get('batch_size')
        auto=self.vars.get('auto_batch')
        if widget:widget.configure(state='disabled' if self.running or (auto is not None and auto.get()) else 'normal')
    def copy_inputs(self):
        selected=self.input_list.curselection();values=[self.input_list.get(i) for i in selected] if selected else list(self.input_list.get(0,'end'))
        if values:self.app.root.clipboard_clear();self.app.root.clipboard_append('\n'.join(values));self.status.set(f'已复制 {number(len(values))} 个输入目录；切换功能后可粘贴追加。')
        return 'break'
    def paste_inputs(self):
        if self.running:return 'break'
        try:
            added,invalid=pasted_directories(self.app.root.clipboard_get(),self.input_list.get(0,'end'))
            for path in added:self.input_list.insert('end',path)
            self.status.set(f'已追加 {number(len(added))} 个目录，重复项已跳过；无效路径 {invalid} 个。');self.app.save()
        except tk.TclError:self.status.set('剪贴板中没有可读取的目录文字。')
        return 'break'
    def _add(self):
        path=filedialog.askdirectory(title='添加输入目录')
        if path and path not in self.input_list.get(0,'end'):self.input_list.insert('end',path)
    def _remove(self):
        for n in reversed(self.input_list.curselection()):self.input_list.delete(n)
        if self.task=='hash':
            if self.vars['std_path'].get() not in self.input_list.get(0,'end'):self.vars['std_path'].set('')
            self.sync_hash_mode()
    def _browse(self):
        path=filedialog.askdirectory(title='选择输出目录',mustexist=False)
        if path:self.output.set(path)
    def config(self):
        data=dict(self.defaults);data['inclusive']=self.app.inclusive.get()
        for key,var in self.vars.items():
            cast=self.casts.get(key)
            value=var.input_value() if hasattr(var,'input_value') else var.get()
            data[key]=(input_float if cast is float else input_int if cast is int else cast)(value) if cast else value
        if self.task=='photo':data['quality_mode']='normal'
        if self.task=='creative':data['include_software']=True
        cls=Config if self.photo_task else TaskConfig
        config=cls(**{key:value for key,value in data.items() if key in cls.__dataclass_fields__});config.validate();return config
    def settings(self):
        return {'inputs':self.input_list.get(0,'end'),'output':self.output.get(),'config':{**asdict(self.config()),**({'photo_policy':'purpose-v7-pbdb'} if self.task=='photo' else {})},'last_run':self.last_run,'last_report':self.last_report,'pending_mode':self.pending_mode,'preview_database':self.preview_database,'result_columns':getattr(self,'result_columns',[]),'last_result':self.last_result,'debug_output':self.debug_output}
    def set_running(self,active):
        self.running=active
        plan=getattr(self,'plan_window',None)
        if active and plan and plan.window.winfo_exists():plan.window.destroy()
        self.app.activity_changed()
        for widget in self.fields+[self.preview_btn,self.execute_btn,self.resume_btn,self.scan_btn]:widget.configure(state='disabled' if active else 'readonly' if widget is self.config_widgets.get('device') else 'normal')
        self.pause_btn.configure(state='normal' if active else 'disabled')
        self.restore_btn.configure(state='normal' if not active and self.last_run and self.task not in ('compress','repair') else 'disabled')
        self.report_btn.configure(state='normal' if not active and self.last_report else 'disabled')
        if active:self.progress.configure(mode='indeterminate');self.progress.start(15)
        else:self.progress.stop();self.progress.configure(mode='determinate')
        if self.photo_task:self._scheme_changed()
        if hasattr(self,'sync_duplicates'):self.sync_duplicates()
        self.resume_btn.configure(text='继续生成副本' if self.task in ('compress','repair') else '继续分组' if self.task=='group' else '继续执行')
        paused=not active and self.pending_mode!='preview' and self.last_result and self.last_result.get('stopped')
        if paused:self.resume_btn.pack(side='left')
        else:self.resume_btn.pack_forget()
        self.preview_btn.configure(text='继续预览' if not active and self.pending_mode=='preview' and self.last_result and self.last_result.get('stopped') else '预览')
    def show_plan(self,on_confirm=None):
        if not self.last_result:
            self.status.set('先预览，即可查看文件去向。');return
        existing=getattr(self,'plan_window',None)
        if existing and existing.window.winfo_exists():
            if on_confirm and existing.on_confirm is None:existing.window.destroy()
            else:existing.window.lift();return
        from .plan_ui import PlanWindow
        from .config import Config
        cls=Config if self.photo_task else TaskConfig
        raw=self.last_result.get('config',{})
        config=cls(**{k:v for k,v in raw.items() if k in cls.__dataclass_fields__})
        self.plan_window=PlanWindow(self,self.last_result,config,on_confirm)

    def start(self,mode,resume=True,approved=None,for_execution=False):
        if self.running:return
        if mode=='preview':self.execution_after_preview=for_execution
        if getattr(getattr(self,'details_window',None),'export_pending',False):
            messagebox.showinfo('正在复制测试图像','请先等待测试复制完成或暂停复制。');return
        try:
            config=self.config();inputs=self.input_list.get(0,'end');output=clean_path(self.output.get());self.output.set(output)
            if not inputs:raise ValueError('请添加输入目录。')
            self.output_unspecified=False
            if self.task not in ('group','hash') and not output and mode=='preview':
                from .preview_state import preview_output
                from .engine import normalize_inputs
                output=str(preview_output(self.task,normalize_inputs(inputs)));self.output_unspecified=True
            if self.task!='group' and not output and not (self.task=='hash' and config.hash_mode in ('std','inplace')):raise ValueError('执行前请选择输出目录。哈希归集预览也需输出，以确定保留位置。')
            if self.photo_task:roots,out=normalize_paths(inputs,output)
            elif self.task=='hash':
                from .hash_task import HashRunner
                roots,out,_=HashRunner(config).locations(inputs,output)
            else:roots,out,_=TaskRunner(config).locations(inputs,output)
            self.app.check_conflicts(self,roots,out,mode)
        except (ValueError,OSError) as exc:messagebox.showerror('无法开始',str(exc));return
        if mode=='move':
            from .planning import matching_preview,preview_signature
            signature=preview_signature(self.task,config,roots,out)
            if approved!=signature:
                if matching_preview(self.last_result,self.task,config,roots,out):
                    # A plan is bound to the captured conditions. If paths or
                    # thresholds change while it is open, start() reviews again.
                    self.show_plan(lambda:self.start('move',resume,approved=signature))
                else:
                    self.start('preview',resume,for_execution=True)
                return
        self.defaults_undo=None
        self.active_roots=roots;self.active_output=out;self.pending_mode=mode;self.stop.clear();self.set_running(True);self.app.save()
        for value in self.stat_values:value.set('—')
        self.eta_text.set('预计耗时：扫描及模型加载完成后估算');self.device_text.set('设备：待检测')
        self.status.set('准备继续未完成任务。' if resume else '准备重新扫描目录。')
        self.app.log(self.task,'任务开始 | '+('预览' if mode=='preview' else '生成兼容副本' if self.task=='repair' else '回收重复副本' if self.task=='hash' and config.hash_mode=='inplace' else '回收匹配副本' if self.task=='hash' and config.hash_mode=='std' else '生成 JPG 副本' if self.task=='compress' else '分组归位' if self.task=='group' else '剪切'))
        def work():
            try:
                if self.preview_database and not self.output_unspecified and self.task not in ('group','hash'):
                    from .preview_state import import_decisions
                    from .moving import ensure_parents
                    metadata=out/'.photocleaner'/self.task
                    out.mkdir(parents=True,exist_ok=True)
                    ensure_parents(metadata/'state.sqlite3',out)
                    # Use the same output lock as execution while importing
                    # read-only preview decisions; never import move journals.
                    with output_lock(out/'.photocleaner'):import_decisions(self.preview_database,metadata,evidence_key=config.evidence_key() if self.photo_task else None)
                cls=Engine if self.photo_task else TaskRunner
                if self.task=='hash':
                    from .hash_task import HashRunner
                    cls=HashRunner
                runner=cls(config,lambda e:self.app.post(self.task,e),self.stop)
                runner.run(inputs,output,mode,resume=resume)
            except Exception as exc:self.app.post(self.task,{'kind':'failure','message':str(exc)})
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()
    def pause(self):
        self.stop.set();self.pause_btn.configure(state='disabled');self.status.set('请求暂停：等待当前解码、模型加载、推理或单文件迁移结束。')
    def handle(self,event):
        kind=event['kind']
        if kind=='log':self.app.log(self.task,event['message'])
        elif kind=='snapshot':
            self.last_result=dict(event)
            if getattr(self,'output_unspecified',False):
                self.preview_database=str(Path(event['report']).parent/'state.sqlite3');self.last_result['output_unspecified']=True
            self.app.save()
        elif kind=='debug_copy_done':
            self.pending_mode,self.active_output=self.debug_previous
            self.set_running(False)
            self.eta_text.set('测试复制已暂停，重试时复用已有副本' if event.get('stopped') else '测试复制已结束')
            text=event.get('error') or f"测试复制{'已暂停' if event.get('stopped') else '完成'}：新增 {number(event.get('copied',0))}，已有相同副本 {number(event.get('skipped',0))}，失败 {number(event.get('errors',0))}。指标：{event.get('manifest','')}"
            self.status.set(text);self.app.log(self.task,text)
            view=getattr(self,'details_window',None)
            if view:view.export_finished(event)
            self.app.save()
        elif kind=='device':self.device_text.set(f"{event['description']} · 推理批量 {number(event['batch'])} 张")
        elif kind=='estimate':
            from .runtime import duration
            unit='个文件/秒' if self.task=='flatten' else '张/秒'
            self.eta_text.set(f"{event.get('phase','处理')}预计剩余 {duration(event['remaining_seconds'])} · 本阶段约 {duration(event['total_seconds'])} · {number(event['rate'])} {unit}")
            if event.get('device_description'):self.device_text.set(event['device_description']+(f" · 推理批量 {number(event['batch'])} 张" if event.get('batch') else ''))
        elif kind=='scan':
            self.found_count=event['scanned']
            self.status.set(f"{'扫描完成，准备处理' if event.get('complete') else '扫描中'} · 已发现 {number(event['scanned'],group=True)} 个文件")
            if self.task!='group' and not (self.task=='hash' and self.vars['hash_mode'].get()=='std'):self.stat_values[0].set(f"{number(event['scanned'],group=True)}")
        elif kind=='progress':
            self.progress.stop();self.progress.configure(mode='determinate',maximum=max(1,event['total']),value=event['processed']);self.status.set(f"{event.get('phase','筛选')}阶段：本阶段处理 {number(event['processed'],group=True)} / {number(event['total'],group=True)}。")
            if event.get('counts'):
                from .ui_layout import update_stats
                update_stats(self,event)
        elif kind in ('done','failure','restored'):
            if kind=='done':
                self.last_report=event['report'];counts=event['counts']
                self.last_result=dict(event)
                if getattr(self,'output_unspecified',False):
                    self.preview_database=str(Path(event['report']).parent/'state.sqlite3');self.last_result['output_unspecified']=True
                self.found_count=event['scanned']
                if event['mode']=='move' and self.task!='compress' and counts.get('moved',0):self.last_run={'metadata':event.get('metadata',str(Path(self.output.get()).resolve()/'.photocleaner')),'output':str(self.active_output) if self.active_output else None,'run_id':event['run_id']}
                state='已暂停' if event['stopped'] else '未完成' if event.get('fatal_error') else '已完成'
                from .ui_layout import result_summary
                self.status.set(result_summary(self,event)+(' · 尚未指定输出，仅预览' if getattr(self,'output_unspecified',False) else '')+(' · 点“继续预览”续跑' if event.get('stopped') and event.get('mode')=='preview' else ''));self.app.log(self.task,completion_text(self.task,event)+f" | 报告：{event['report']}")
                if event.get('fatal_error'):self.app.log(self.task,'错误：'+event['fatal_error'])
                if event.get('scan_issues'):self.app.log(self.task,f"扫描提示：{event['scan_issues']} 条，请查看日志。")
                if event.get('snapshot_reused'):self.status.set(self.status.get()+' · 复用目录快照')
                if event.get('reused_scores'):self.status.set(self.status.get()+f" · {number(event['reused_scores'],group=True)} 张复用指标重筛")
                if self.photo_task and self.device_text.get()=='设备：待检测':self.device_text.set('本次无需模型推理')
            elif kind=='restored':
                self.status.set(f"恢复完成 | 已恢复 {event['restored']} | 跳过或错误 {number(event['errors'])}")
                if not event['errors'] and not event['stopped']:self.last_run=None
            else:self.status.set('未完成：'+event['message']);self.app.log(self.task,event['message'])
            self.set_running(False);self.app.save()
            self.eta_text.set('已暂停；继续后重新估算' if event.get('stopped') else '本次处理已结束')
            if getattr(self,'execution_after_preview',False):
                self.execution_after_preview=False
                if kind=='done' and event.get('mode')=='preview' and not event.get('stopped') and not event.get('fatal_error') and not event.get('scan_issues') and not event.get('counts',{}).get('queued'):
                    self.start('move')
                    return
            existing=getattr(self,'details_window',None)
            if kind=='done' and ((existing and existing.window.winfo_exists()) or (not event.get('stopped') and not event.get('fatal_error'))):self.show_details()
    def show_details(self,debug=False):
        if not getattr(self,'last_result',None):
            self.status.set('尚无预览记录；开始预览后即可查看已完成部分。');return
        from .result_ui import ResultWindow
        existing=getattr(self,'details_window',None)
        if existing and existing.window.winfo_exists():
            if existing.result.get('run_id')==self.last_result.get('run_id') and existing.database==Path(self.last_result['report']).parent/'state.sqlite3':
                existing.result=dict(self.last_result);existing.update_config();existing.refresh();existing.window.lift()
                if debug:existing.debug_mode.set(True);existing.toggle_debug()
                return
            existing.window.destroy()
        self.details_window=ResultWindow(self,self.last_result)
        if debug:self.details_window.debug_mode.set(True);self.details_window.toggle_debug()
    def restore(self):
        if not self.last_run:return
        record=dict(self.last_run)
        try:
            store=Store(Path(record['metadata'])/'state.sqlite3')
            try:roots=[Path(r[0]).resolve() for r in store.db.execute('SELECT DISTINCT f.root FROM files f JOIN moves m ON f.source=m.source WHERE m.run_id=?',(record['run_id'],))]
            finally:store.close()
            out=Path(record['output']).resolve() if record['output'] else None
            self.app.check_conflicts(self,roots,out,'move')
        except (OSError,ValueError) as exc:messagebox.showerror('无法恢复',str(exc));return
        self.active_roots=roots;self.active_output=out;self.pending_mode='move'
        self.stop.clear();self.set_running(True)
        def work():
            try:
                metadata=Path(record['metadata'])
                with ExitStack() as locks:
                    output=Path(record['output']).resolve() if record['output'] else None
                    if output and metadata.resolve()!=output/'.photocleaner':locks.enter_context(output_lock(output/'.photocleaner'))
                    locks.enter_context(output_lock(metadata))
                    store=Store(metadata/'state.sqlite3')
                    try:
                        result=restore_moves(store,Path(record['output']) if record['output'] else None,lambda s:self.app.post(self.task,{'kind':'log','message':s}),self.stop,record['run_id']);store.export('restore',metadata)
                    finally:store.close()
                self.app.post(self.task,{'kind':'restored',**result})
            except Exception as exc:self.app.post(self.task,{'kind':'failure','message':str(exc)})
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()
    def report(self):
        if self.last_report:
            try:os.startfile(self.last_report)
            except OSError as exc:messagebox.showerror('无法打开报告',str(exc))
    def open_review(self):
        if self.running:return
        if self.review_window and self.review_window.window.winfo_exists():self.review_window.window.lift();return
        try:
            from .review_ui import ReviewWindow
            self.review_window=ReviewWindow(self,'compression' if self.task=='compress' else 'numbered')
        except (OSError,ValueError,RuntimeError) as exc:messagebox.showerror('无法打开列表',str(exc))


class App:
    def __init__(self,root,show_welcome=True):
        from .ui_theme import apply_theme,BG,MUTED,LINE
        self.root=root;root.title('PhotoCleaner');self.events=queue.Queue(maxsize=2000);self.settings_path=PROJECT/'settings.json'
        from .ui_scroll import cancel_callbacks_on_destroy
        cancel_callbacks_on_destroy(root)
        self.safe_actions=SafeActions();self.closing=False;self.inclusive=tk.BooleanVar(root,value=True);self.search_index_dir=tk.StringVar(root,value=str(PROJECT/'.jobs'/'search'));self.last_used=None
        self.dpi=window_dpi(root);root.tk.call('tk','scaling',self.dpi/72)
        self.body_font=font.Font(root,name='BodyFont',family='Microsoft YaHei UI',size=10)
        self.title_font=font.Font(root,name='TitleFont',family='Microsoft YaHei UI',size=21,weight='bold')
        self.small_font=font.Font(root,name='SmallFont',family='Microsoft YaHei UI',size=9)
        self.subtitle_font=font.Font(root,name='SubtitleFont',family='Microsoft YaHei UI',size=11,weight='bold')
        self.number_font=font.Font(root,name='NumberFont',family='Segoe UI',size=18,weight='bold')
        self.style=apply_theme(root,self.dpi);root.configure(background=BG)
        d=lambda v:round(v*self.dpi/96)
        from .dpi import initial_geometry
        root.geometry(initial_geometry(root,self.dpi))
        root.minsize(d(920),min(d(710),round(root.winfo_screenheight()*.85)))
        outer=ttk.Frame(root);outer.pack(fill='both',expand=True)
        side=ttk.Frame(outer,style='Side.TFrame',padding=(d(14),d(24)));side.pack(side='left',fill='y')
        brand=tk.Frame(side,bg='#edf0f6');brand.pack(fill='x',pady=(0,d(25)))
        mark=tk.Canvas(brand,width=d(32),height=d(32),bg='#edf0f6',highlightthickness=0);mark.pack(side='left',padx=(0,d(8)))
        for x,y,c in [(2,2,'#6554c0'),(17,2,'#aca0dc'),(2,17,'#aca0dc'),(17,17,'#6554c0')]:mark.create_rectangle(d(x),d(y),d(x+12),d(y+12),fill=c,outline='')
        tk.Label(brand,text='PhotoCleaner',font='SubtitleFont',bg='#edf0f6',fg='#334155').pack(side='left')
        self.nav_buttons={}
        nav_area=ttk.Frame(side,style='Side.TFrame');nav_area.pack(fill='both',expand=True)
        self.nav_canvas=tk.Canvas(nav_area,width=d(155),background='#edf0f6',highlightthickness=0)
        from .ui_scroll import AutoScrollbar,fit_canvas
        nav_scroll=AutoScrollbar(nav_area,command=self.nav_canvas.yview);nav_scroll.pack(side='right',fill='y')
        self.nav_canvas.pack(side='left',fill='both',expand=True);self.nav_canvas.configure(yscrollcommand=nav_scroll.set)
        self.nav_body=ttk.Frame(self.nav_canvas,style='Side.TFrame');nav_item=self.nav_canvas.create_window(0,0,window=self.nav_body,anchor='nw')
        for task in TASK_NAMES:
            if task in ('meme','text_search','small','flatten','compress'):
                section={'meme':'按内容筛图','text_search':'查找图像','small':'按像素与体积','flatten':'文件整理','compress':'图像压缩'}[task]
                tk.Label(self.nav_body,text=section,font='SmallFont',bg='#edf0f6',fg=MUTED).pack(anchor='w',padx=d(16),pady=(d(20),d(9)))
            button=ttk.Button(self.nav_body,text=TASK_NAMES[task],style='Nav.TButton',command=lambda key=task:self.select_task(key));button.pack(fill='x',pady=0);self.nav_buttons[task]=button
        fit_canvas(self.nav_canvas,self.nav_body,nav_item)
        self.threshold_switch=ttk.Checkbutton(side,text='阈值包含等号（≥ / ≤）',variable=self.inclusive,command=self.threshold_changed);self.threshold_switch.pack(fill='x',pady=(d(28),d(10)))
        ttk.Button(side,text='缓存与数据库',style='Nav.TButton',command=lambda:__import__('photocleaner.storage_ui',fromlist=['show_storage']).show_storage(self)).pack(fill='x')
        ttk.Button(side,text='运行记录',style='Nav.TButton',command=self.toggle_logs).pack(fill='x')
        ttk.Button(side,text='快速上手',style='Nav.TButton',command=lambda:__import__('photocleaner.welcome_ui',fromlist=['show_introduction']).show_introduction(self)).pack(fill='x')
        tk.Label(side,text='本地处理 · 安全转移',font='SmallFont',bg='#edf0f6',fg=MUTED).pack(anchor='w',padx=d(16),pady=(d(14),0))
        ttk.Separator(outer,orient='vertical').pack(side='left',fill='y')
        workspace=ttk.Frame(outer);workspace.pack(side='left',fill='both',expand=True)
        self.notebook=ttk.Notebook(workspace);self.notebook.pack(fill='both',expand=True)
        from .search_ui import SearchPane
        settings=self._load();self.panes={task:(SearchPane if task in ('text_search','image_search') else TaskPane)(self,task,settings.get(task,{})) for task in TASK_NAMES}
        self.logs_frame=ttk.Frame(workspace,padding=(d(24),d(6),d(24),d(12)))
        title=ttk.Frame(self.logs_frame);title.pack(fill='x')
        ttk.Label(title,text='运行记录',font='SubtitleFont').pack(side='left')
        ttk.Button(title,text='收起',style='Ghost.TButton',command=self.toggle_logs).pack(side='right')
        self.log_view=tk.Text(self.logs_frame,height=5,font='SmallFont',wrap='word',state='disabled',background='#ffffff',foreground='#475569',relief='flat',bd=0,padx=d(10),pady=d(8))
        scroll=ttk.Scrollbar(self.logs_frame,command=self.log_view.yview);scroll.pack(side='right',fill='y');self.log_view.configure(yscrollcommand=scroll.set);self.log_view.pack(fill='x',expand=True)
        self.safety_text=tk.StringVar(root,value='就绪 · 各功能独立保存目录和进度')
        self.safety_label=tk.Label(workspace,textvariable=self.safety_text,font='SmallFont',background='#eaeef5',foreground='#64748b',anchor='w',padx=d(24),pady=d(9));self.safety_label.pack(fill='x')
        self.threshold_changed(save=False)
        self.select_task('meme')
        root.protocol('WM_DELETE_WINDOW',self.close);root.bind('<Configure>',self._dpi_changed,add='+');root.bind_all('<MouseWheel>',self._wheel,add='+');root.after(100,self.poll)
        for n,task in enumerate(TASK_NAMES,1):
            if n<=9:root.bind(f'<Control-Key-{number(n)}>',lambda _,key=task:self.select_task(key))
        if show_welcome:
            from .welcome_ui import introduction_due,show_introduction
            if introduction_due(self.last_used):root.after(300,lambda:show_introduction(self) if not self.closing else None)

    def threshold_changed(self,save=True):
        low,high=('≥','≤') if self.inclusive.get() else ('>','<')
        upper_keys={'blockiness_threshold','meme_threshold','reblur_threshold','focus_reblur_threshold','max_aspect','rare_count','rare_ratio'}
        for pane in getattr(self,'panes',{}).values():
            if hasattr(pane,'refresh_size_rules'):pane.refresh_size_rules()
            for key,(widget,title) in getattr(pane,'condition_labels',{}).items():
                if key in ('device','workers','batch_size','max_pixels','quality'):continue
                import re
                title=re.sub(r'[≥≤<>]','',title).replace('宽和高都大于','宽和高下限').replace('任意一边','任意一边上限')
                widget.configure(text=title+' '+(high if key in upper_keys or pane.task=='small' else low))
        if save:self.save()

    def select_task(self,task):
        self.notebook.select(self.panes[task].frame)
        for key,button in self.nav_buttons.items():button.configure(style='Active.Nav.TButton' if key==task else 'Nav.TButton')

    def toggle_logs(self):
        if self.logs_frame.winfo_manager():self.logs_frame.pack_forget()
        else:self.logs_frame.pack(fill='x',before=self.safety_label)

    def activity_changed(self):
        if not hasattr(self,'safety_text'):return
        active=[TASK_NAMES[p.task] for p in self.panes.values() if p.running]
        for task,button in self.nav_buttons.items():button.configure(text=TASK_NAMES[task]+(' · 运行中' if self.panes[task].running else ''))
        self.threshold_switch.configure(state='disabled' if active else 'normal')
        if active:
            self.safety_text.set('请勿在程序运行时对相关文件夹自行操作。  正在运行：'+'、'.join(active))
            self.safety_label.configure(background='#fff1d8',foreground='#805415')
        else:
            self.safety_text.set('就绪 · 目录有新增或修改时请重新扫描');self.safety_label.configure(background='#eaeef5',foreground='#64748b')

    def _dpi_changed(self,event):
        if event.widget!=self.root:return
        dpi=window_dpi(self.root)
        if dpi!=self.dpi:
            self.dpi=dpi;self.root.tk.call('tk','scaling',dpi/72)
            for f in (self.body_font,self.title_font,self.small_font,self.subtitle_font,self.number_font):f.configure(size=f.cget('size'))
    def _wheel(self,event):
        from .ui_scroll import can_scroll
        if event.widget is self.nav_canvas or str(event.widget).startswith(str(self.nav_body)):
            if can_scroll(self.nav_canvas):self.nav_canvas.yview_scroll(-int(event.delta/120),'units')
            return 'break'
        pane=list(self.panes.values())[self.notebook.index('current')]
        if str(event.widget).startswith(str(pane.body)) and not isinstance(event.widget,(tk.Listbox,tk.Text,ttk.Combobox)) and can_scroll(pane.canvas):pane.canvas.yview_scroll(-int(event.delta/120),'units')
    def check_conflicts(self,pane,roots,out,mode):
        if getattr(self,'storage_busy',False):raise ValueError('正在复制搜图索引，请完成后再开始任务。')
        def overlap(a,b):return a==b or a in b.parents or b in a.parents
        for other in self.panes.values():
            if other is pane or not other.running:continue
            if out and other.active_output and overlap(out,other.active_output):raise ValueError('与另一个活动任务的输出目录重叠，请先暂停相关任务。')
            if (out and any(overlap(out,p) for p in other.active_roots)) or (other.active_output and any(overlap(other.active_output,p) for p in roots)):raise ValueError('一个任务的输出位于另一个活动任务的输入内，请先暂停相关任务。')
            if mode=='preview' and other.pending_mode=='preview':continue
            if any(overlap(a,b) for a in roots for b in other.active_roots):raise ValueError(f"与正在运行的“{TASK_NAMES[other.task]}”使用重叠输入。请先暂停它；不同输入的任务可同时运行。")
    def post(self,task,event):
        if event['kind'] in ('done','failure','restored','close_finished','search_done','copy_done','index_merged','snapshot','debug_copy_done'):
            self.events.put((task,event))
            return
        try:self.events.put_nowait((task,event))
        except queue.Full:
            pass  # Drop an intermediate update, never a completion event.
    def log(self,task,text):
        stamp=datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S')
        self.log_view.configure(state='normal');self.log_view.insert('end',f'{stamp} | {TASK_NAMES[task]} | {text}\n')
        if int(self.log_view.index('end-1c').split('.')[0])>1000:self.log_view.delete('1.0','101.0')
        self.log_view.see('end');self.log_view.configure(state='disabled')
    def poll(self):
        try:
            for _ in range(300):
                task,event=self.events.get_nowait()
                if event['kind']=='close_finished':
                    if event.get('errors'):
                        self.closing=False
                        for pane in self.panes.values():pane.set_running(False)
                        leave=messagebox.askyesno('回收未完成','部分备份未进入回收站，仍保留在输入目录的 .photocleaner/safe-actions/ 中。\n是否关闭并保留这些备份？\n选择“否”返回程序，可先撤回操作或解决回收站问题，再正常关闭。')
                        if leave:self.safe_actions.release();self.root.destroy();return
                    else:self.safe_actions.release();self.root.destroy();return
                else:self.panes[task].handle(event)
        except queue.Empty:pass
        self.root.after(100,self.poll)
    def _load(self):
        try:
            data=json.loads(self.settings_path.read_text(encoding='utf-8'))
            self.last_used=data.get('last_used')
            if 'tasks' in data:
                self.inclusive.set(data.get('inclusive',True));self.search_index_dir.set(data.get('search_index_dir',str(PROJECT/'.jobs'/'search')));return data['tasks']
            legacy=data.get('last_run')
            if legacy and isinstance(legacy,list):data['last_run']={'output':legacy[0],'run_id':legacy[1],'metadata':str(Path(legacy[0]).resolve()/'.photocleaner')}
            return {'photo':data}
        except (OSError,ValueError,TypeError):return {}
    def save(self):
        try:
            try:previous=json.loads(self.settings_path.read_text(encoding='utf8')).get('tasks',{})
            except (OSError,ValueError):previous={}
            tasks={}
            for task,pane in self.panes.items():
                try:tasks[task]=pane.settings()
                except (ValueError,TypeError):
                    tasks[task]=dict(previous.get(task,{}))
                    # An unfinished numeric edit on another page must not
                    # discard a completed preview or its resume pointers.
                    for key in ('last_result','last_run','last_report','preview_database','pending_mode','result_columns','debug_output'):
                        if hasattr(pane,key):tasks[task][key]=getattr(pane,key)
            data={'version':3,'last_used':datetime.now(timezone.utc).isoformat(),'inclusive':self.inclusive.get(),'search_index_dir':self.search_index_dir.get(),'tasks':tasks}
            temporary=self.settings_path.with_suffix('.json.tmp');temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,self.settings_path)
        except (OSError,ValueError):pass
    def close(self):
        if self.closing:return
        if getattr(self,'storage_busy',False):self.log('photo','请等待索引复制和验证完成，再关闭程序。');return
        running=[pane for pane in self.panes.values() if pane.running]
        if running:
            for pane in running:pane.pause()
            self.log('photo','关闭请求：等待所有活动任务安全暂停后，可再次关闭窗口。');return
        self.save()
        if not self.safe_actions.roots:self.safe_actions.release();self.root.destroy();return
        self.closing=True
        for pane in self.panes.values():pane.set_running(True)
        self.log('compress','关闭阶段：把待关闭的撤回备份移入回收站。请等待。')
        def finish():
            try:result=self.safe_actions.finalize(lambda s:self.post('compress',{'kind':'log','message':s}))
            except Exception as exc:result={'errors':1};self.post('compress',{'kind':'log','message':str(exc)})
            self.post('compress',{'kind':'close_finished',**result})
        threading.Thread(target=finish,daemon=False).start()

def main():
    enable_dpi();root=tk.Tk();App(root);root.mainloop()

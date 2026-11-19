"""Shared execution preview; all database work stays outside Tk callbacks."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .numbers import number
from .planning import read_plan
from .tasks import TASK_NAMES
from .ui_text import responsive_label


class PlanWindow:
    def __init__(self, pane, result, config, on_confirm=None):
        self.pane = pane
        self.result = dict(result)
        self.on_confirm = on_confirm
        self.plan = None
        self.events = queue.Queue()
        self.window = win = tk.Toplevel(pane.app.root)
        win.title(TASK_NAMES[pane.task] + ' · 文件去向')
        win.transient(pane.app.root)
        from .ui_scroll import cancel_callbacks_on_destroy
        cancel_callbacks_on_destroy(win)
        scale = pane.app.dpi / 96
        win.geometry(f'{min(round(880*scale),round(win.winfo_screenwidth()*.9))}x{min(round(610*scale),round(win.winfo_screenheight()*.85))}')
        body = ttk.Frame(win, padding=22)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='执行前，确认文件将去哪里', font='SubtitleFont').pack(anchor='w')
        self.message = tk.StringVar(win, value='正在汇总已保存的处理计划…')
        responsive_label(ttk.Label(body, textvariable=self.message, style='Muted.TLabel')).pack(fill='x', pady=(10, 14))
        roots=result.get('priority_roots') or result.get('roots',[])
        if pane.task=='hash' and getattr(config,'hash_mode','')=='inplace':
            scope='保留目录优先级：'+' → '.join(str(root) for root in roots[:4])+(f'（共 {len(roots)} 个目录，完整顺序见输入列表）' if len(roots)>4 else '')
        elif pane.task=='hash' and getattr(config,'hash_mode','')=='std':
            scope='基准目录：'+config.std_path
        else:
            scope=f'输入目录 {len(roots)} 个，包含子目录。'
        responsive_label(ttk.Label(body,text=scope,style='Muted.TLabel')).pack(fill='x',pady=(0,10))
        self.tree = ttk.Treeview(body, columns=('action', 'count', 'size', 'destination'), show='headings', height=4)
        for key, title, width in [('action', '操作', 160), ('count', '文件数', 100), ('size', '原文件合计 MB', 150), ('destination', '去向', 380)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=round(width*scale), minwidth=round(90*scale), stretch=key == 'destination')
        self.tree.pack(fill='both', expand=True)
        self.retained = tk.StringVar(win)
        responsive_label(ttk.Label(body, textvariable=self.retained)).pack(fill='x', pady=12)
        self.recovery = tk.StringVar(win)
        responsive_label(ttk.Label(body, textvariable=self.recovery)).pack(fill='x', pady=(0, 10))
        self.space = tk.StringVar(win)
        responsive_label(ttk.Label(body, textvariable=self.space, style='Muted.TLabel')).pack(fill='x')
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(16, 0))
        ttk.Button(actions, text='返回', command=win.destroy).pack(side='right')
        self.confirm = None
        if on_confirm:
            self.confirm = ttk.Button(actions, text='按此计划执行', state='disabled', style='Primary.TButton', command=self.accept)
            self.confirm.pack(side='right', padx=8)
        def work():
            try:
                database = Path(result['report']).parent / 'state.sqlite3'
                self.events.put((read_plan(database, result['run_id'], pane.task, config, result), None))
            except Exception as exc:
                self.events.put((None, str(exc)))
        threading.Thread(target=work, daemon=True, name='file-plan').start()
        win.after(50, self.poll)

    def poll(self):
        if not self.window.winfo_exists():
            return
        try:
            plan, error = self.events.get_nowait()
        except queue.Empty:
            self.window.after(50, self.poll)
            return
        if error:
            self.message.set('处理计划暂时无法读取：' + error)
            return
        self.plan = plan
        for item in plan.items:
            self.tree.insert('', 'end', values=(item.action, format(item.count, ','), number(item.bytes / 1_000_000), item.destination))
        self.message.set(f'本次待执行 {plan.pending:,} 个文件。' + ('当前为部分结果；完成预览后可执行。' if plan.partial else '以下统计来自本次预览，执行时会再次核验源文件和同名冲突。'))
        self.retained.set(f'输入中保留 {plan.kept:,} · 基准/输出已有 {plan.existing:,} · 已完成 {plan.completed:,} · 待判定 {plan.queued:,} · 待复查/失败 {plan.unresolved:,}')
        self.recovery.set('恢复方式：' + plan.recovery)
        self.space.set(plan.space_note)
        if self.confirm:
            self.confirm.configure(state='normal' if plan.pending and not plan.partial else 'disabled')

    def accept(self):
        if self.on_confirm and self.plan and self.plan.pending and not self.plan.partial:
            callback = self.on_confirm
            self.on_confirm = None  # A double click must not enqueue a second run.
            self.window.destroy()
            if callback:
                callback()

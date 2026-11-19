"""An explicit draft → impact snapshot → apply workflow for content gates."""
from dataclasses import replace
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .config import PROJECT
from .numbers import number
from .threshold_preview import THRESHOLD_FIELDS, compare_thresholds, threshold_config
from .ui_text import responsive_label


class ThresholdWindow:
    def __init__(self, viewer):
        self.viewer = viewer
        self.pane = viewer.pane
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.revision = 0
        self.snapshot = None
        self.proposed = None
        self.window = win = tk.Toplevel(viewer.window)
        win.title('门槛影响 · 先比较，再应用')
        win.transient(viewer.window)
        from .ui_scroll import cancel_callbacks_on_destroy
        cancel_callbacks_on_destroy(win)
        win.bind('<Destroy>', lambda event: self.stop.set() if event.widget is win else None, add='+')
        scale = self.pane.app.dpi / 96
        win.geometry(f'{min(round(740*scale),round(win.winfo_screenwidth()*.9))}x{min(round(760*scale),round(win.winfo_screenheight()*.88))}')
        outer = ttk.Frame(win, padding=20)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='调整门槛后，会多选或少选哪些图？', font='SubtitleFont').pack(anchor='w')
        responsive_label(ttk.Label(outer, text='比较只读取已保存的分数。原明细保持原条件；点击“计算影响”生成独立快照。', style='Muted.TLabel')).pack(fill='x', pady=(10, 12))
        area = ttk.Frame(outer)
        area.pack(fill='both', expand=True)
        canvas = tk.Canvas(area, highlightthickness=0)
        from .ui_scroll import AutoScrollbar, fit_canvas
        scroll = AutoScrollbar(area, command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        fields = ttk.Frame(canvas)
        item = canvas.create_window(0, 0, window=fields, anchor='nw')
        fit_canvas(canvas, fields, item)
        fields.columnconfigure(1, weight=1)
        self.values = {}
        for n, (key, title) in enumerate(THRESHOLD_FIELDS[self.pane.task]):
            ttk.Label(fields, text=title).grid(row=n, column=0, sticky='w', pady=5, padx=(0, 12))
            variable = tk.StringVar(win, value=str(getattr(viewer.config, key)))
            self.values[key] = variable
            ttk.Entry(fields, textvariable=variable, width=16).grid(row=n, column=1, sticky='ew', pady=5)
            ttk.Label(fields, text='原值 '+number(getattr(viewer.config, key)), style='Muted.TLabel').grid(row=n, column=2, sticky='w', padx=(12, 0))
            variable.trace_add('write', self.edited)
        self.inclusive = tk.BooleanVar(win, value=viewer.config.inclusive)
        ttk.Checkbutton(outer, text='门槛包含等号（≥ / ≤；应用时同步左栏总开关）', variable=self.inclusive, command=self.edited).pack(anchor='w', pady=(8, 4))
        self.status = tk.StringVar(win, value='先计算影响，再打开新增或移出的缩略图复核。')
        responsive_label(ttk.Label(outer, textvariable=self.status)).pack(fill='x', pady=8)
        self.summary = tk.StringVar(win)
        responsive_label(ttk.Label(outer, textvariable=self.summary, style='Muted.TLabel')).pack(fill='x', pady=(0, 8))
        review = ttk.Frame(outer)
        review.pack(fill='x')
        self.review_buttons = []
        for text, change in [('查看新增符合', '新增符合'), ('查看不再符合', '不再符合'), ('全部对比明细', '全部变化')]:
            button = ttk.Button(review, text=text, state='disabled', command=lambda value=change: self.open_comparison(value))
            button.pack(side='left', padx=(0, 6))
            self.review_buttons.append(button)
        responsive_label(ttk.Label(outer, text='比较范围为本次全部记录，显示筛选资格的变化。去重、文件变化和缺少的证据在完整预览时复核；类别排除沿用当前策略。', style='Muted.TLabel')).pack(fill='x', pady=12)
        actions = ttk.Frame(outer)
        actions.pack(fill='x')
        self.calculate_button = ttk.Button(actions, text='计算影响', style='Primary.TButton', command=self.calculate)
        self.calculate_button.pack(side='left')
        ttk.Button(actions, text='恢复原条件', command=self.reset).pack(side='left', padx=6)
        self.apply_button = ttk.Button(actions, text='应用条件并更新预览', command=self.apply, state='disabled')
        self.apply_button.pack(side='right')
        win.after(60, self.poll)

    def edited(self, *_):
        self.revision += 1
        if hasattr(self, 'apply_button'):
            self.apply_button.configure(state='disabled')
            self.status.set('条件已修改；点击“计算影响”更新对比。已生成的对比快照仍可查看。')

    def reset(self):
        for key, variable in self.values.items():
            variable.set(str(getattr(self.viewer.config, key)))
        self.inclusive.set(self.viewer.config.inclusive)
        self.edited()

    def calculate(self):
        if self.busy:
            return
        try:
            config = threshold_config(self.viewer.config, {key: var.get() for key, var in self.values.items()})
            config = replace(config, inclusive=self.inclusive.get())
            config.validate()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.busy = True
        self.calculate_button.configure(state='disabled')
        self.apply_button.configure(state='disabled')
        self.status.set('正在对比已有证据；本次不会读取原图或运行模型。')
        revision = self.revision
        def work():
            try:
                result = compare_thresholds(self.viewer.database, self.viewer.result['run_id'], self.viewer.config, config, PROJECT/'.jobs'/'threshold_views', self.stop)
                result.update({key:self.viewer.result[key] for key in ('roots', 'output', 'output_unspecified') if key in self.viewer.result})
                self.events.put((revision, result, config, None))
            except Exception as exc:
                self.events.put((revision, None, config, str(exc)))
        threading.Thread(target=work, daemon=True, name='threshold-impact').start()

    def poll(self):
        if not self.window.winfo_exists():
            return
        try:
            while True:
                revision, result, config, error = self.events.get_nowait()
                self.busy = False
                self.calculate_button.configure(state='normal')
                if error:
                    self.status.set(error)
                    continue
                self.snapshot, self.proposed = result, config
                c = result['comparison_counts']
                self.summary.set(f"新增符合 {c['新增符合']:,} · 不再符合 {c['不再符合']:,} · 继续符合 {c['继续符合']:,} · 继续保留 {c['继续保留']:,}\n待补算 {c['待补算']:,} · 已执行 {c['已执行']:,}")
                self.status.set('对比已生成。可用缩略图复核新增及移出项。' if revision==self.revision else '已生成计算时的条件快照；草稿后来有修改，再点“计算影响”可更新。')
                self.apply_button.configure(state='normal' if revision==self.revision else 'disabled')
                for button in self.review_buttons:
                    button.configure(state='normal')
        except queue.Empty:
            pass
        self.window.after(60, self.poll)

    def open_comparison(self, change):
        if not self.snapshot:
            return
        from .result_ui import ResultWindow
        viewer = ResultWindow(self.pane, dict(self.snapshot))
        viewer.change.set(change)
        viewer.display_mode.set('gallery')
        viewer.switch_display()

    def apply(self):
        if not self.proposed or str(self.apply_button.cget('state'))=='disabled':
            return
        if self.pane.running:
            self.status.set('本功能正在处理文件；安全暂停后可应用条件。')
            return
        if self.pane.app.inclusive.get()!=self.proposed.inclusive and any(p.running for p in self.pane.app.panes.values()):
            self.status.set('等号规则由所有功能共用；其他任务暂停或完成后可同步此选项。')
            return
        for key in self.values:
            self.pane.vars[key].set(getattr(self.proposed, key))
        self.pane.app.inclusive.set(self.proposed.inclusive)
        self.window.destroy()
        self.pane.start('preview')

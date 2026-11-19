"""A short introduction, shown on first use or after a long absence."""
from datetime import datetime,timezone,timedelta
import tkinter as tk
from tkinter import ttk

def introduction_due(last_used,now=None):
    now=now or datetime.now(timezone.utc)
    try:
        previous=datetime.fromisoformat(last_used)
        if previous.tzinfo is None:previous=previous.replace(tzinfo=timezone.utc)
        return now-previous>=timedelta(days=90)
    except (TypeError,ValueError):return True

def show_introduction(app):
    existing=getattr(app,'welcome_window',None)
    if existing is not None and existing.winfo_exists():existing.lift();return
    from .ui_text import Paragraph
    from .ui_theme import BG
    win=tk.Toplevel(app.root);app.welcome_window=win;win.title('欢迎使用 PhotoCleaner');win.transient(app.root)
    s=app.dpi/96;width=round(520*s);height=round(370*s)
    x=max(0,min(app.root.winfo_rootx()+(app.root.winfo_width()-width)//2,win.winfo_screenwidth()-width))
    y=max(0,min(app.root.winfo_rooty()+(app.root.winfo_height()-height)//2,win.winfo_screenheight()-height))
    win.geometry(f'{width}x{height}+{x}+{y}');win.resizable(False,False)
    frame=ttk.Frame(win,padding=round(26*s));frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='把图像整理得更轻松',font='TitleFont').pack(anchor='w',pady=(0,round(14*s)))
    for title,body in [('选择一个入口','像素与体积筛选快；按内容筛图更细；查找图像帮你找到想要的画面。'),('先预览，再执行','添加目录后先看明细，可调整条件继续筛选。搜图结果由你勾选后复制。'),('安心处理','处理记录和进度自动保存。运行时请让相关文件夹保持原样。')]:
        ttk.Label(frame,text=title,font='SubtitleFont').pack(anchor='w',pady=(round(10*s),round(3*s)))
        Paragraph(frame,body,background=BG).pack(fill='x')
    ttk.Button(frame,text='开始使用',style='Primary.TButton',command=win.destroy).pack(anchor='e',pady=(round(18*s),0))
    win.protocol('WM_DELETE_WINDOW',win.destroy)

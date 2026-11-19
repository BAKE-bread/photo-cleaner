"""One native text help viewer; editable UTF-8 sources are read on each open."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from .config import PROJECT
from .tasks import TASK_NAMES

HELP_DIR=PROJECT/'help'

def help_source(task):
    path=HELP_DIR/(task+'.md')
    if path.is_file():return path
    from .paths import resource_file
    return resource_file('help',task+'.md')

def show_help(pane,fallback):
    from .ui_theme import BG,INK
    path=help_source(pane.task)
    try:text=path.read_text(encoding='utf-8-sig')
    except (OSError,UnicodeError):text=fallback
    lines=text.splitlines()
    if lines and lines[0].startswith('# '):text='\n'.join(lines[1:]).lstrip('\n')
    existing=getattr(pane,'help_window',None)
    if existing is not None and existing.winfo_exists():
        box=existing.text_widget;box.configure(state='normal');box.delete('1.0','end');box.insert('1.0',text);box.configure(state='disabled');existing.deiconify();existing.lift();return existing
    win=tk.Toplevel(pane.app.root);win.title(TASK_NAMES[pane.task]+' · 使用说明');win.configure(background=BG)
    scale=pane.app.dpi/96
    width=min(round(800*scale),round(win.winfo_screenwidth()*.92));height=min(round(680*scale),round(win.winfo_screenheight()*.86))
    x=max(0,min(pane.app.root.winfo_rootx()+(pane.app.root.winfo_width()-width)//2,win.winfo_screenwidth()-width))
    y=max(0,min(pane.app.root.winfo_rooty()+(pane.app.root.winfo_height()-height)//2,win.winfo_screenheight()-height))
    win.geometry(f'{width}x{height}+{x}+{y}')
    win.minsize(min(width,round(440*scale)),min(height,round(340*scale)));win.transient(pane.app.root)
    body=ttk.Frame(win,padding=round(20*scale));body.pack(fill='both',expand=True)
    body.columnconfigure(0,weight=1);body.rowconfigure(1,weight=1)
    ttk.Label(body,text=TASK_NAMES[pane.task]+' · 使用说明',font='SubtitleFont').grid(row=0,column=0,sticky='w',pady=(0,12))
    area=ttk.Frame(body);area.grid(row=1,column=0,sticky='nsew')
    box=tk.Text(area,wrap='char',width=1,height=1,font='BodyFont',background='#ffffff',foreground=INK,relief='flat',
                padx=round(14*scale),pady=round(12*scale),spacing1=3,spacing2=3,spacing3=7,undo=False)
    from .ui_scroll import AutoScrollbar
    scroll=AutoScrollbar(area,command=box.yview);scroll.pack(side='right',fill='y');box.pack(fill='both',expand=True)
    box.configure(yscrollcommand=scroll.set);box.insert('1.0',text);box.configure(state='disabled')
    def wheel(event):box.yview_scroll(-int(event.delta/120),'units');return 'break'
    box.bind('<MouseWheel>',wheel)
    footer=ttk.Frame(body);footer.grid(row=2,column=0,sticky='ew',pady=(12,0))
    ttk.Label(footer,text='文字来源：'+str(path),style='Muted.TLabel',wraplength=round(550*scale)).pack(side='left',fill='x',expand=True)
    ttk.Button(footer,text='知道了',command=win.destroy,style='Primary.TButton').pack(side='right',padx=(10,0))
    win.text_widget=box;win.help_path=path
    pane.help_window=win
    return win

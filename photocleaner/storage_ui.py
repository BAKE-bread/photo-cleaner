"""Inspect storage and relocate the search index by a verified SQLite copy."""
from .numbers import number
from pathlib import Path
import sqlite3,threading,queue
from contextlib import closing
import tkinter as tk
from tkinter import ttk,filedialog
from .config import PROJECT

def copy_index(source,target):
    source=Path(source);target=Path(target)
    target.mkdir(parents=True,exist_ok=True)
    if target.resolve()!=target.absolute():raise ValueError('索引目录不能包含链接或联接。')
    dst=target/'index.sqlite3'
    if dst.exists():
        with closing(sqlite3.connect(dst.as_uri()+'?mode=ro',uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='images'").fetchone():raise ValueError('目标不是有效搜索索引；未改动任何数据库。')
        return '使用目标已有索引；可在搜图页合并其他索引。'
    src=source/'index.sqlite3'
    if not src.exists():return '已设置新位置；首次搜索会建立索引。'
    with dst.open('xb'):pass
    a=sqlite3.connect(src.resolve().as_uri()+'?mode=ro',uri=True);b=sqlite3.connect(dst)
    try:
        a.backup(b)
        if b.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise OSError('索引副本验证未通过；原库保留。')
        return '索引已复制并验证；原位置的数据库完整保留。'
    finally:a.close();b.close()

def show_storage(app):
    existing=getattr(app,'storage_window',None)
    if existing is not None and existing.winfo_exists():existing.lift();return existing
    win=tk.Toplevel(app.root);win.title('缓存与数据库位置');frame=ttk.Frame(win,padding=20);frame.pack(fill='both',expand=True)
    app.storage_window=win
    from .ui_scroll import cancel_callbacks_on_destroy
    cancel_callbacks_on_destroy(win)
    s=app.dpi/96;win.geometry(f'{min(round(800*s),round(win.winfo_screenwidth()*.92))}x{min(round(740*s),round(win.winfo_screenheight()*.88))}')
    text=tk.StringVar(win)
    storage_events=queue.Queue();refresh_generation=[0]
    def refresh():
        refresh_generation[0]+=1;generation=refresh_generation[0]
        paths=[('模型权重',PROJECT/'models'),('搜图共用索引',Path(app.search_index_dir.get())),('媒介证据缓存',PROJECT/'.jobs'/'media.sqlite3'),('无输出预览 / 分组 / 调试快照',PROJECT/'.jobs')]
        text.set('\n\n'.join(f'{title}\n{path}' for title,path in paths)+'\n\n正在统计数据库占用…')
        def read_sizes():
            lines=[]
            try:
                for title,path in paths:
                    files=[path] if path.is_file() else list(path.rglob('*.sqlite3')) if path.is_dir() and title!='模型权重' else []
                    sizes=[]
                    for item in files:
                        for file in (item,Path(str(item)+'-wal'),Path(str(item)+'-shm')):
                            try:sizes.append(file.stat().st_size)
                            except OSError:pass
                    lines.append(f'{title}\n{path}'+(f'\n数据库占用约 {number(sum(sizes)/1e9)} GB' if files else ''))
                lines.append('已指定输出的筛选与恢复记录位于输出内的 .photocleaner 子目录。\n文字、相似图与风格图共用一个搜图索引；可复制到空间充足的位置，原库保留。')
                storage_events.put((generation,'\n\n'.join(lines)))
            except OSError as exc:storage_events.put((generation,'读取占用信息失败：'+str(exc)))
        threading.Thread(target=read_sizes,daemon=True).start()
    def poll_sizes():
        if not win.winfo_exists():return
        try:
            while True:
                generation,value=storage_events.get_nowait()
                if generation==refresh_generation[0]:text.set(value)
        except queue.Empty:pass
        win.after(100,poll_sizes)
    from .ui_text import responsive_label
    responsive_label(ttk.Label(frame,textvariable=text,wraplength=650)).pack(fill='x');refresh();poll_sizes()
    note=tk.StringVar(win,value='全部任务暂停后可更换搜图索引位置。');ttk.Label(frame,textvariable=note,wraplength=650).pack(fill='x',pady=12)
    events=queue.Queue()
    def choose():
        if any(p.running for p in app.panes.values()):note.set('请先暂停任务，再更换索引位置。');return
        target=filedialog.askdirectory(parent=win,title='选择搜图索引目录；原索引将复制过去')
        if not target:return
        source=Path(app.search_index_dir.get());app.storage_busy=True;button.configure(state='disabled');close.configure(state='disabled');note.set('正在复制和验证数据库；原库保留。')
        def work():
            try:events.put((target,copy_index(source,Path(target).absolute()),None))
            except Exception as exc:events.put((None,None,str(exc)))
        threading.Thread(target=work,daemon=False).start()
        def poll():
            try:
                target,message,error=events.get_nowait();app.storage_busy=False;button.configure(state='normal');close.configure(state='normal')
                if error:note.set(error)
                else:app.search_index_dir.set(target);app.save();refresh();note.set(message)
            except queue.Empty:win.after(150,poll)
        poll()
    button=ttk.Button(frame,text='更换搜图索引位置…',command=choose);button.pack(side='left')
    close=ttk.Button(frame,text='关闭',command=win.destroy);close.pack(side='right')
    win.protocol('WM_DELETE_WINDOW',lambda:note.set('请等待索引复制完成。') if getattr(app,'storage_busy',False) else win.destroy())
    return win

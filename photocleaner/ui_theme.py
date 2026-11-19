"""Small native ttk theme; no extra UI runtime or downloaded assets."""
from tkinter import ttk

BG='#f5f6fa'
SURFACE='#ffffff'
INK='#1e293b'
MUTED='#64748b'
ACCENT='#6554c0'
LINE='#e4e7ef'

def apply_theme(root,dpi):
    style=ttk.Style(root);style.theme_use('clam')
    s=dpi/96
    style.configure('.',font='BodyFont',background=BG,foreground=INK)
    style.configure('TFrame',background=BG)
    style.configure('Card.TFrame',background=SURFACE)
    style.configure('Side.TFrame',background='#edf0f6')
    style.configure('TLabel',background=BG,foreground=INK)
    for name,color in [('Card',INK),('Muted',MUTED),('CardMuted',MUTED),('Accent',ACCENT)]:
        style.configure(name+'.TLabel',foreground=color,background=SURFACE if name.startswith('Card') else BG)
    style.configure('CardTitle.TLabel',font='SubtitleFont',background=SURFACE)
    style.configure('Number.TLabel',font='NumberFont',foreground=INK,background=SURFACE)
    style.configure('TButton',padding=(round(12*s),round(7*s)),borderwidth=0,relief='flat',background='#e9ecf3',foreground=INK)
    style.map('TButton',background=[('pressed','#d8dce8'),('active','#dfe3ee'),('disabled','#eef0f5')],foreground=[('disabled','#a2acba')])
    style.configure('Primary.TButton',background=ACCENT,foreground='white')
    style.map('Primary.TButton',background=[('disabled','#d7d1ec'),('pressed','#4e419a'),('active','#7563d0')],foreground=[('disabled','#f8f7fc')])
    style.configure('Ghost.TButton',background=SURFACE,foreground=MUTED)
    style.map('Ghost.TButton',background=[('active','#f0eef9'),('pressed','#e5e1f5')],foreground=[('active',ACCENT),('disabled','#b3bbca')])
    style.configure('Nav.TButton',anchor='w',background='#edf0f6',padding=(round(17*s),round(8*s)),foreground='#475569')
    style.map('Nav.TButton',background=[('active','#e4e7f0')])
    style.configure('Active.Nav.TButton',background='#e0daf5',foreground='#53439c')
    style.map('Active.Nav.TButton',background=[('active','#d8d0f0')])
    style.configure('TEntry',fieldbackground=SURFACE,bordercolor=LINE,lightcolor=LINE,darkcolor=LINE,padding=round(6*s))
    style.map('TEntry',bordercolor=[('focus',ACCENT)],fieldbackground=[('disabled','#f2f4f8')])
    style.configure('TCombobox',fieldbackground=SURFACE,background='#eef0f6',bordercolor=LINE,padding=round(6*s),arrowsize=round(13*s))
    style.map('TCombobox',fieldbackground=[('readonly',SURFACE),('disabled','#f2f4f8')],foreground=[('readonly',INK)])
    style.configure('TCheckbutton',background=SURFACE,padding=(0,round(4*s)))
    style.map('TCheckbutton',background=[('active',SURFACE)])
    style.configure('TProgressbar',background=ACCENT,troughcolor='#e7e7f0',borderwidth=0,thickness=round(5*s))
    style.configure('Vertical.TScrollbar',background='#d4d8e4',troughcolor=BG,borderwidth=0,arrowsize=round(11*s))
    style.configure('TNotebook',background=BG,borderwidth=0)
    style.layout('TNotebook.Tab',[])
    style.configure('Treeview',background=SURFACE,fieldbackground=SURFACE,borderwidth=0,font='BodyFont',rowheight=round(32*s))
    style.configure('Treeview.Heading',background='#eef0f6',font='BodyFont',padding=(8,8),borderwidth=0)
    style.map('Treeview',background=[('selected','#d9d2f1')],foreground=[('selected',INK)])
    return style

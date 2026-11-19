"""Width-aware paragraphs with CJK justification and intact English words."""
import re
import tkinter as tk
from tkinter import font

def notify_layout(widget):
    parent=widget.master
    while parent is not None:
        callback=getattr(parent,'request_fit',None)
        if callback is not None:callback();return
        parent=parent.master

def wrapped_lines(text,measure,width):
    result=[]
    for paragraph in text.split('\n'):
        tokens=re.findall(r'[A-Za-z0-9][A-Za-z0-9_./:%–—-]*|\s+|.',paragraph)
        line=[];used=0
        for token in tokens:
            chunks=[token] if measure(token)<=width else list(token)
            for chunk in chunks:
                size=measure(chunk)
                if line and used+size>width:
                    while line and line[-1].isspace():used-=measure(line.pop())
                    carry=[]
                    if line and (chunk in '，。、；：！？）》】”’%…' or line[-1] in '（《【“‘('):carry=[line.pop()]
                    result.append((line,True));line=carry;used=sum(measure(t) for t in line)
                if not line and chunk.isspace():continue
                line.append(chunk);used+=size
        result.append((line,False))
    return result

class Paragraph(tk.Canvas):
    def __init__(self,parent,text,background='#ffffff',foreground='#64748b',font_name='BodyFont',**kwargs):
        super().__init__(parent,background=background,highlightthickness=0,height=24,**kwargs)
        self.text=text;self.ink=foreground;self.font_name=font_name
        self._pending=None;self._layout_key=None;self._measurements={}
        self.bind('<Configure>',self.schedule)
        self.bind('<Destroy>',self._cancel)
    def _cancel(self,event):
        if event.widget is self and self._pending is not None:self.after_cancel(self._pending);self._pending=None
    def schedule(self,event=None):
        if self._pending is None:self._pending=self.after_idle(self.redraw)
    def redraw(self,event=None):
        self._pending=None
        if not self.winfo_exists():return
        width=max(20,self.winfo_width()-2)
        f=font.nametofont(self.font_name);height=round(f.metrics('linespace')*1.65)
        signature=tuple(sorted(f.actual().items()));key=(width,self.text,signature)
        if key==self._layout_key:return
        self._layout_key=key;self.delete('all')
        if getattr(self,'_font_signature',None)!=signature:self._measurements.clear();self._font_signature=signature
        def measure(token):
            if token not in self._measurements:self._measurements[token]=f.measure(token)
            return self._measurements[token]
        lines=wrapped_lines(self.text,measure,width)
        y=0
        for tokens,justify in lines:
            if not justify:
                self.create_text(0,y,text=''.join(tokens),fill=self.ink,font=self.font_name,anchor='nw');y+=height;continue
            used=sum(measure(t) for t in tokens)
            gap=max(0,(width-used)/(len(tokens)-1)) if justify and len(tokens)>1 else 0
            x=0
            for token in tokens:
                self.create_text(x,y,text=token,fill=self.ink,font=self.font_name,anchor='nw');x+=measure(token)+gap
            y+=height
        if int(self.cget('height'))!=max(height,y):
            self.configure(height=max(height,y));notify_layout(self)

def responsive_label(widget):
    def resize(event):
        width=max(50,event.width-4)
        if getattr(widget,'_responsive_width',None)!=width:
            widget._responsive_width=width;widget.configure(wraplength=width)
            notify_layout(widget)
    widget.bind('<Configure>',resize,add='+')
    return widget

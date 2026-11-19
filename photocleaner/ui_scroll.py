"""Scroll only overflowing content; let the viewport expand on maximize."""
from tkinter import ttk

class AutoScrollbar(ttk.Scrollbar):
    def set(self,first,last):
        if float(first)<=0 and float(last)>=1:
            manager=self.winfo_manager()
            if manager=='pack':self._pack=self.pack_info();self.pack_forget()
            elif manager=='grid':self.grid_remove()
        elif not self.winfo_manager():
            if hasattr(self,'_pack'):self.pack(**self._pack)
            else:self.grid()
        super().set(first,last)

def fit_canvas(canvas,body,item,input_list=None,scale=1):
    cancel_callbacks_on_destroy(canvas)
    pending=[None]
    def fit():
        pending[0]=None
        if not canvas.winfo_exists():return
        if input_list is not None:
            rows=max(3,min(8,3+int(max(0,canvas.winfo_height()/scale-410)/65)))
            if int(input_list.cget('height'))!=rows:input_list.configure(height=rows)
        width=canvas.winfo_width();height=max(canvas.winfo_height(),body.winfo_reqheight())
        if float(canvas.itemcget(item,'width'))!=width:canvas.itemconfigure(item,width=width)
        if float(canvas.itemcget(item,'height'))!=height:canvas.itemconfigure(item,height=height)
        region=(0,0,width,height)
        if tuple(map(float,canvas.cget('scrollregion').split()))!=region:canvas.configure(scrollregion=region)
        if height<=canvas.winfo_height():canvas.yview_moveto(0)
    def schedule(event=None):
        if pending[0] is None:pending[0]=canvas.after_idle(fit)
    canvas.bind('<Configure>',schedule,add='+');body.bind('<Configure>',schedule,add='+')
    tag='PhotoCleanerFit_'+str(canvas)
    for event in ('<Configure>','<Map>','<Unmap>'):canvas.bind_class(tag,event,schedule)
    def observe(widget):
        widget.bindtags((*widget.bindtags(),tag))
        for child in widget.winfo_children():observe(child)
    observe(body)
    # The body is at least as tall as the viewport. Expanding children can
    # change requested height without changing its allocated size, so no body
    # Configure event is guaranteed. Explicit requests cover collapsible cards.
    followup=[None]
    def invalidate():
        schedule()
        if followup[0] is not None:canvas.after_cancel(followup[0])
        def settled():
            followup[0]=None;schedule()
        # pack/unpack propagates requested sizes through nested frames over
        # several idle geometry passes, even when allocation stays fixed.
        followup[0]=canvas.after(25,settled)
    canvas.request_fit=invalidate
    schedule()

def can_scroll(canvas):
    box=canvas.bbox('all')
    return box is not None and box[3]-box[1]>canvas.winfo_height()+1

def cancel_callbacks_on_destroy(widget):
    """Cancel this widget's timers before Tk removes their Tcl commands."""
    def cleanup(event):
        if event.widget is not widget:return
        commands=set(getattr(widget,'_tclCommands',None) or [])
        for token in widget.tk.call('after','info'):
            script=widget.tk.call('after','info',token)[0]
            if str(script).split(' ',1)[0] in commands:widget.after_cancel(token)
    widget.bind('<Destroy>',cleanup,add='+')

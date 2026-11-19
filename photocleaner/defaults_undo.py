"""One task-local undo for loading defaults; no calculation or history stack."""
from copy import deepcopy

def snapshot(pane):
    values={key:(var.get(),getattr(var,'_actual',None)) for key,var in pane.vars.items()}
    return values,deepcopy(pane.defaults),pane.status.get()

def restore(pane,state,keys=None):
    values,defaults,status=state
    if keys is None:pane.defaults=deepcopy(defaults)
    else:
        for key in keys:
            if key in defaults:pane.defaults[key]=deepcopy(defaults[key])
            else:pane.defaults.pop(key,None)
    for key,(text,actual) in values.items():
        if keys is not None and key not in keys:continue
        var=pane.vars.get(key)
        if var is None:continue
        var.set(text)
        if hasattr(var,'_actual'):var._actual=actual
    pane.status.set(status)

def load(pane,values):
    before=snapshot(pane)
    try:
        for key,value in values.items():
            if key in pane.vars:
                var=pane.vars[key]
                if hasattr(var,'present'):var.present(value)
                else:var.set(value)
            else:pane.defaults[key]=value
        pane.config()
    except Exception:
        restore(pane,before);raise
    after=snapshot(pane)
    if before[:2]!=after[:2]:pane.defaults_undo=(before,set(values))

def undo(pane):
    state=getattr(pane,'defaults_undo',None)
    if pane.running or state is None:return False
    pane.defaults_undo=None;before,keys=state;restore(pane,before,keys);return True

def install(pane):
    tag='PhotoCleanerDefaultsUndo'
    def handler(event):
        undo(pane)
        return 'break'
    # A pane-specific tag precedes Entry's native key bindings, so one press
    # cannot also change an input field after restoring the saved conditions.
    tag+='_'+str(pane.frame)
    pane.frame.bind_class(tag,'<Control-z>',handler)
    pane.frame.bind_class(tag,'<Control-Z>',handler)
    def visit(widget):
        widget.bindtags((tag,*widget.bindtags()))
        for child in widget.winfo_children():visit(child)
    visit(pane.frame)

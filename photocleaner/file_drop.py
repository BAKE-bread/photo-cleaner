"""Explorer drops with pointer-sized Win32 subclassing and deferred Tk delivery."""
import os

def bind_file_drop(widget,callback):
    if os.name!='nt':return False
    if getattr(widget,'_drop_binding',None):return True
    try:
        import ctypes
        from ctypes import wintypes
        from collections import deque
        common=ctypes.WinDLL('comctl32',use_last_error=True);shell=ctypes.WinDLL('shell32',use_last_error=True)
        proc_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t,ctypes.c_size_t,ctypes.c_size_t)
        common.SetWindowSubclass.argtypes=[wintypes.HWND,proc_type,ctypes.c_size_t,ctypes.c_size_t];common.SetWindowSubclass.restype=wintypes.BOOL
        common.RemoveWindowSubclass.argtypes=[wintypes.HWND,proc_type,ctypes.c_size_t];common.RemoveWindowSubclass.restype=wintypes.BOOL
        common.DefSubclassProc.argtypes=[wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t];common.DefSubclassProc.restype=ctypes.c_ssize_t
        shell.DragAcceptFiles.argtypes=[wintypes.HWND,wintypes.BOOL];shell.DragAcceptFiles.restype=None
        shell.DragQueryFileW.argtypes=[wintypes.HANDLE,wintypes.UINT,wintypes.LPWSTR,wintypes.UINT];shell.DragQueryFileW.restype=wintypes.UINT
        shell.DragFinish.argtypes=[wintypes.HANDLE];shell.DragFinish.restype=None
        hwnd=widget.winfo_id();identity=id(widget);pending=deque(maxlen=8);closed=[False];timer=[None]
        @proc_type
        def procedure(handle,message,wparam,lparam,subclass_id,reference):
            if message==0x0233:  # WM_DROPFILES. Never enter Tcl from this hook.
                try:
                    paths=[]
                    for i in range(min(shell.DragQueryFileW(wparam,0xffffffff,None,0),256)):
                        size=shell.DragQueryFileW(wparam,i,None,0);buffer=ctypes.create_unicode_buffer(size+1)
                        shell.DragQueryFileW(wparam,i,buffer,size+1);paths.append(buffer.value)
                    pending.append(paths)
                finally:shell.DragFinish(wparam)
                return 0
            if message==0x0082:  # WM_NCDESTROY
                closed[0]=True;common.RemoveWindowSubclass(handle,procedure,identity)
            return common.DefSubclassProc(handle,message,wparam,lparam)
        if not common.SetWindowSubclass(hwnd,procedure,identity,0):return False
        shell.DragAcceptFiles(hwnd,True)
        def drain():
            timer[0]=None
            if closed[0] or not widget.winfo_exists():return
            try:
                while pending:callback(pending.popleft())
            finally:
                if not closed[0]:timer[0]=widget.after(50,drain)
        def cleanup(event):
            if event.widget is not widget:return
            closed[0]=True;pending.clear()
            if timer[0] is not None:widget.after_cancel(timer[0]);timer[0]=None
            shell.DragAcceptFiles(hwnd,False);common.RemoveWindowSubclass(hwnd,procedure,identity)
            widget._drop_binding=None
        widget.bind('<Destroy>',cleanup,add='+')
        # Retain the native callback until the HWND is detached/destroyed.
        widget._drop_binding=(procedure,common,shell,pending,cleanup)
        timer[0]=widget.after(50,drain)
        return True
    except (ImportError,OSError,AttributeError):return False

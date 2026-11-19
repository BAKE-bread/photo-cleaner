"""Opt in before creating Tk: Windows must render text at monitor DPI."""
import os

def enable_dpi():
    if os.name!='nt':return
    import ctypes
    try:
        fn=ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes=[ctypes.c_void_p]
        if fn(ctypes.c_void_p(-4)):return
    except (AttributeError,OSError):pass
    try:ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError,OSError):
        try:ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError,OSError):pass

def window_dpi(root):
    if os.name=='nt':
        import ctypes
        try:
            value=ctypes.windll.user32.GetDpiForWindow(root.winfo_id())
            if value:return value
        except (AttributeError,OSError):pass
    return max(96,round(root.winfo_fpixels('1i')))

def initial_geometry(root,dpi,details=False):
    """Requested physical size at 150%, scaled for other monitors and clamped."""
    scale=dpi/144
    width=min(round(1920*scale),round(root.winfo_screenwidth()*.94))
    height=min(round((1280 if details else 1600)*scale),round(root.winfo_screenheight()*.90))
    return f'{width}x{height}'

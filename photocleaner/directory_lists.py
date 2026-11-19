from pathlib import Path
import os

def clean_path(value):
    value=str(value).strip()
    while len(value)>=2 and (value[0],value[-1]) in [('"','"'),("'","'"),('“','”')]:value=value[1:-1].strip()
    return value

def path_entry(widget,variable):
    def clean(event=None):variable.set(clean_path(variable.get()))
    widget.bind('<FocusOut>',clean,add='+')
    widget.bind('<<Paste>>',lambda _:widget.after_idle(clean),add='+')

def pasted_directories(text,existing):
    seen={os.path.normcase(str(Path(p).expanduser().resolve())) for p in existing};added=[];invalid=0
    for line in text.splitlines():
        value=clean_path(line)
        if not value:continue
        try:
            path=Path(value).expanduser().resolve(strict=True)
            if not path.is_dir():raise OSError('not directory')
            key=os.path.normcase(str(path))
            if key not in seen:added.append(str(path));seen.add(key)
        except (OSError,ValueError):invalid+=1
    return added,invalid

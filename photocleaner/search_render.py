"""Pillow-only candidate decoding; these workers never hold Tk objects."""
import io
from PIL import Image

def decode_thumbnail(data,size):
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.thumbnail(size,Image.Resampling.LANCZOS)
            return source.convert('RGB'),None
    except (OSError,ValueError,TypeError,Image.DecompressionBombError) as exc:return None,str(exc)

def decode_preview(path,expected):
    try:
        from pathlib import Path
        from .moving import fingerprint
        from .search_model import load_search_image
        if fingerprint(Path(path))!=expected:raise OSError('文件已变化，请更新索引。')
        im,_,_=load_search_image(Path(path))
        if fingerprint(Path(path))!=expected:im.close();raise OSError('读取期间文件已变化，请更新索引。')
        return im,None
    except (OSError,ValueError,Image.DecompressionBombError) as exc:return None,str(exc)

"""Read-only Windows WIC decode probe; no app launching or source modification."""
import os
from pathlib import Path

PROBE_VERSION='wic-full-frame-extensions-v2'
# A recognized photo filename can contain a different supported encoding:
# Photos, like WIC, can sniff JPEG data in a .png/.bmp and vice versa.
PHOTO_SUFFIXES={'.jpg','.jpeg','.jpe','.jfif','.png','.bmp','.dib','.gif','.tif','.tiff',
                '.webp','.heic','.heif','.avif','.jxr','.wdp','.hdp'}

def probe_image(path,max_pixels=80_000_000):
    if os.name!='nt':return {'status':'unavailable','detail':'当前系统不是 Windows，未进行系统解码检查'}
    import ctypes as C
    from ctypes import wintypes as W
    import uuid
    class GUID(C.Structure):
        _fields_=[('data1',C.c_uint32),('data2',C.c_uint16),('data3',C.c_uint16),('data4',C.c_ubyte*8)]
    class Rect(C.Structure):_fields_=[('x',C.c_int),('y',C.c_int),('width',C.c_int),('height',C.c_int)]
    def guid(value):return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
    HRESULT=C.c_long;P=C.c_void_p;UINT=C.c_uint
    def method(obj,index,restype,*args):
        address=C.cast(obj,C.POINTER(C.POINTER(P))).contents[index]
        return C.WINFUNCTYPE(restype,P,*args)(address)
    def check(code):
        if code<0:raise OSError(f'Windows 解码返回 0x{code & 0xffffffff:08X}')
    ole=C.WinDLL('ole32',use_last_error=True)
    ole.CoInitializeEx.argtypes=[P,W.DWORD];ole.CoInitializeEx.restype=HRESULT
    ole.CoUninitialize.argtypes=[];ole.CoUninitialize.restype=None
    ole.CoCreateInstance.argtypes=[C.POINTER(GUID),P,W.DWORD,C.POINTER(GUID),C.POINTER(P)];ole.CoCreateInstance.restype=HRESULT
    initialized=ole.CoInitializeEx(None,0);uninitialize=initialized>=0
    if initialized<0 and (initialized & 0xffffffff)!=0x80010106:
        return {'status':'unavailable','detail':f'Windows 解码检查无法初始化：0x{initialized & 0xffffffff:08X}'}
    objects=[];stage='factory'
    try:
        clsid=guid('cacaf262-9370-4615-a13b-9f5539da4c0a');iid=guid('ec5ec8a9-c395-4314-9c77-54d7a935ff70');factory=P()
        check(ole.CoCreateInstance(C.byref(clsid),None,1,C.byref(iid),C.byref(factory)));objects.append(factory)
        decoder=P();stage='decode'
        check(method(factory,3,HRESULT,W.LPCWSTR,P,W.DWORD,UINT,C.POINTER(P))(factory,str(Path(path).absolute()),None,0x80000000,0,C.byref(decoder)));objects.append(decoder)
        extensions=[]
        # The active installed codec can support additional formats such as
        # camera RAW. Do not treat every extension outside a fixed list as bad.
        info=P()
        if method(decoder,6,HRESULT,C.POINTER(P))(decoder,C.byref(info))>=0 and info.value:
            objects.append(info);count=UINT()
            method(info,17,HRESULT,UINT,W.LPWSTR,C.POINTER(UINT))(info,0,None,C.byref(count))
            if 0<count.value<=65536:
                text=C.create_unicode_buffer(count.value)
                if method(info,17,HRESULT,UINT,W.LPWSTR,C.POINTER(UINT))(info,count.value,text,C.byref(count))>=0:
                    extensions=[part.strip().lower() for part in text.value.replace(';',',').split(',') if part.strip().startswith('.')]
        frame=P();check(method(decoder,13,HRESULT,UINT,C.POINTER(P))(decoder,0,C.byref(frame)));objects.append(frame)
        width=UINT();height=UINT();check(method(frame,3,HRESULT,C.POINTER(UINT),C.POINTER(UINT))(frame,C.byref(width),C.byref(height)))
        if not width.value or not height.value or width.value*height.value>max_pixels:
            return {'status':'unavailable','detail':'系统解码像素数超过本页上限'}
        converter=P();check(method(factory,10,HRESULT,C.POINTER(P))(factory,C.byref(converter)));objects.append(converter)
        bgra=guid('6fddc324-4e03-4bfe-b185-3d77768dc90f')
        check(method(converter,8,HRESULT,P,C.POINTER(GUID),C.c_int,P,C.c_double,C.c_int)(converter,frame,C.byref(bgra),0,None,0.0,0))
        tile_width=min(width.value,2048);tile_height=min(height.value,128);stride=tile_width*4
        buffer=C.create_string_buffer(stride*tile_height)
        copy=method(converter,7,HRESULT,C.POINTER(Rect),UINT,UINT,P)
        for y in range(0,height.value,tile_height):
            for x in range(0,width.value,tile_width):
                rect=Rect(x,y,min(tile_width,width.value-x),min(tile_height,height.value-y))
                check(copy(converter,C.byref(rect),stride,len(buffer),buffer))
        return {'status':'passed','detail':'Windows 系统已完整解码首帧','width':width.value,'height':height.value,'extensions':extensions}
    except (OSError,ValueError,AttributeError) as exc:
        return {'status':'unavailable' if stage=='factory' else 'failed','detail':str(exc)}
    finally:
        for obj in reversed(objects):method(obj,2,C.c_ulong)(obj)
        if uninitialize:ole.CoUninitialize()

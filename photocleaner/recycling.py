"""Windows recycle operation with a veto against permanent deletion."""
import os
from pathlib import Path


def recycle_file(path):
    path=Path(path).absolute()
    if os.name!='nt':raise OSError('本功能需要 Windows 回收站。文件保留在撤回备份中。')
    try:
        import pythoncom
        import pywintypes
        from win32com.shell import shell,shellcon
        from win32com.server.policy import DesignatedWrapPolicy
        from win32com.server.exception import COMException
    except ImportError as exc:
        raise OSError('缺少 pywin32；请运行 setup.bat。备份保留，不永久删除。') from exc

    class Sink(DesignatedWrapPolicy):
        _com_interfaces_=[shell.IID_IFileOperationProgressSink]
        _public_methods_=['StartOperations','FinishOperations','PreRenameItem','PostRenameItem',
            'PreMoveItem','PostMoveItem','PreCopyItem','PostCopyItem','PreDeleteItem','PostDeleteItem',
            'PreNewItem','PostNewItem','UpdateProgress','ResetTimer','PauseTimer','ResumeTimer']
        def __init__(self):
            self._wrap_(self);self.recycled=False
        def noop(self,*args):return 0
        StartOperations=FinishOperations=PreRenameItem=PostRenameItem=PreMoveItem=PostMoveItem=noop
        PreCopyItem=PostCopyItem=PreNewItem=PostNewItem=UpdateProgress=ResetTimer=PauseTimer=ResumeTimer=noop
        def PreDeleteItem(self,flags,item):
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                # Raising, rather than merely returning an HRESULT, makes the
                # Python COM wrapper actually cancel the native operation.
                raise COMException(desc='目标不支持回收，拒绝永久删除',scode=-2147467259)
            return 0
        def PostDeleteItem(self,flags,item,result,new_item):
            # COPYENGINE_S_RECYCLE is a positive success HRESULT, not S_OK.
            self.recycled=not (result & 0x80000000) and new_item is not None
            return 0

    pythoncom.CoInitialize()
    try:
        operation=pythoncom.CoCreateInstance(shell.CLSID_FileOperation,None,pythoncom.CLSCTX_ALL,shell.IID_IFileOperation)
        operation.SetOperationFlags(shellcon.FOF_SILENT|shellcon.FOF_NOERRORUI|shellcon.FOF_NOCONFIRMATION|
                                    shellcon.FOFX_EARLYFAILURE|0x00080000|0x20000000)
        sink=Sink();wrapped=pythoncom.WrapObject(sink,shell.IID_IFileOperationProgressSink)
        operation.DeleteItem(shell.SHCreateItemFromParsingName(str(path),None,shell.IID_IShellItem),wrapped)
        result=operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted() or not sink.recycled or path.exists():
            raise OSError('未确认成功进入回收站；撤回备份保留，请检查磁盘或回收站设置。')
    except pywintypes.com_error as exc:
        raise OSError(f'回收失败；备份保留：{exc.strerror}') from exc
    finally:
        pythoncom.CoUninitialize()

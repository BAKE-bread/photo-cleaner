"""Task-specific conclusions, with units and actions that have a meaning."""
from .numbers import number
def completion_text(task,event):
    c=event['counts'];r=event.get('reasons',{})
    state='已暂停' if event.get('stopped') else '未完成' if event.get('fatal_error') else '预览完成' if event['mode']=='preview' else '执行完成'
    common=f"{state} | 发现 {number(event['scanned'],group=True)} 个文件"
    faults=c.get('error',0)+c.get('review',0);queued=c.get('queued',0)
    if task=='hash':
        if event.get('hash_mode')=='inplace':text=state+f" | 文件 {number(event['scanned'])} 个 | 重复内容 {number(event.get('duplicate_groups',0))} 组 | 原位保留 {number(c.get('rejected',0))} 个 | 待回收 {number(c.get('selected',0))} 个 | 已回收 {number(c.get('recycled',0))} 个"
        elif event.get('hash_mode')=='std':text=state+f" | 基准图像 {number(event.get('std_count',0))} 张 | 其他图像 {number(event.get('comparison_count',0))} 张 | 待回收 {number(c.get('selected',0))} 张 | 已回收 {number(c.get('recycled',0))} 张；基准目录保持原样"
        else:text=common+f" | 待处理 {number(c.get('selected',0))} 个 | 代表文件已移动 {number(c.get('moved',0))} 个 | 重复副本已回收 {number(c.get('recycled',0))} 个 | 保留 {number(c.get('rejected',0))} 个"
    elif task=='group':
        g=event.get('grouping',{})
        text=common+(' | 分组统计尚未完成，请继续任务' if event.get('group_plan_complete') is False else f" | {number(g.get('directories',0))} 个原目录 / {number(g.get('methods',0))} 种命名规律 / {number(g.get('groups',0))} 个目标分组目录（含其他）")
        text+=f" | 待归位 {number(c.get('selected',0))} 张 | 已归位 {number(c.get('moved',0))} 张 | 已在正确分组 {c.get('grouped',0)} 张"
        if event.get('group_plan_complete') is not False:text+=f" | 其他目标组 {number(g.get('other_groups',0))} 个 / {number(g.get('other_images',0))} 张"
        text+=f" | 非图像等跳过 {number(r.get('non_image_extension',0))} 个"
    elif task=='compress':
        text=common+f" | 待压缩 {number(c.get('selected',0))} 张 | 已有或新生成 JPG {number(c.get('copied',0))} 张 | 不符合压缩条件/非图像 {number(c.get('rejected',0))} 个；原图保留"
    elif task=='repair':
        text=common+f" | 待修复 {number(c.get('selected',0))} 张 | 兼容副本 {number(c.get('copied',0))} 张 | 无需修复/不支持 {number(c.get('rejected',0))} 个；原文件保留"
    elif task=='flatten':
        text=common+f" | 待扁平剪切 {number(c.get('selected',0))} 个 | 已剪切 {number(c.get('moved',0))} 个 | 完全重复原地保留 {number(r.get('exact_duplicate',0))} 个"
        if r.get('backup_duplicate'):text+=f" | 重复副本备份 {number(r['backup_duplicate'])} 个（包含在待剪切/已剪切中）"
    elif task=='small':
        text=common+f" | 待剪切低像素图像 {number(c.get('selected',0))} 张 | 已剪切 {number(c.get('moved',0))} 张 | 像素条件不符/非图像/重复 {number(c.get('rejected',0))} 个"
    elif task=='file_size':
        text=common+f" | 符合体积条件待剪切 {number(c.get('selected',0))} 张 | 已剪切 {number(c.get('moved',0))} 张 | 体积条件不符/非图像/重复 {number(c.get('rejected',0))} 个"
    else:
        label='人物活动图像' if event.get('scheme')=='people' else '表情包' if event.get('scheme')=='meme' else '绘画与设计图像' if event.get('scheme')=='creative' else '实拍照片'
        text=common+f" | 待剪切{label} {number(c.get('selected',0))} 张 | 已剪切 {number(c.get('moved',0))} 张 | 未入选（含尺寸/格式/重复） {number(c.get('rejected',0))} 个"
    if task=='hash' and event.get('retained_counts') and (event.get('hash_mode')=='std' or not event.get('output_is_input')):
        kept=event['retained_counts'];text+=f" | 基准/输出已有（无需操作）{number(kept.get('existing',0))} 个 | 其他输入保留 {number(kept.get('rejected',0))} 个"
    if r.get('backup_duplicate') and task!='flatten':text+=f" | 入选重复副本 {number(r['backup_duplicate'])} 张，使用重复副本备份目录"
    if queued:text+=f' | 尚未判定 {number(queued)} 个'
    if task=='group' and c.get('rejected',0)>r.get('non_image_extension',0):text+=f" | 不可读取等跳过 {number(c['rejected']-r.get('non_image_extension',0))} 个"
    if faults:text+=f' | 失败或中断待人工核对 {number(faults)} 个（现有文件保留）'
    return text

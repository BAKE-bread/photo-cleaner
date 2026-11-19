from .numbers import number
from .numbers import input_float,input_int
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import sys
import threading
import sqlite3

from .config import Config
from .engine import Engine
from .moving import restore_moves
from .storage import Store, output_lock


def content_restore_plan(output,task,run_id=None):
    """Select only this content tool's runs, with explicit legacy fallback."""
    journal=Path(output)/'.photocleaner';current=journal/task
    def matching(directory):
        database=directory/'state.sqlite3'
        if not database.is_file():return []
        if database.resolve()!=database.absolute():raise ValueError('恢复记录路径不能包含链接。')
        result=[]
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
            for identity,config in db.execute('SELECT id,config FROM runs ORDER BY started DESC,id DESC'):
                try:scheme=json.loads(config).get('scheme')
                except (TypeError,ValueError,AttributeError):continue
                if scheme==task:result.append(identity)
        return result
    current_exists=(current/'state.sqlite3').is_file()
    if run_id:
        for directory in (current,journal):
            if run_id in matching(directory):return directory,[run_id]
        raise ValueError('未找到属于所选功能的该运行记录；请核对 --task 和 --run-id。')
    if current_exists:return current,matching(current)
    if (journal/'state.sqlite3').is_file():return journal,matching(journal)
    raise ValueError('未找到该输出目录的剪切记录。')


def main(argv=None):
    parser = argparse.ArgumentParser(description="PhotoCleaner：本地照片筛选与安全剪切")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gui", help="打开中文图形界面")
    search=sub.add_parser('search',help='文字/参考图搜索，只列出候选；确认复制请使用 GUI')
    search.add_argument('--input',nargs='+',required=True)
    search.add_argument('--output',help='从搜索范围排除该输出目录；此命令不复制图像')
    query=search.add_mutually_exclusive_group(required=True)
    query.add_argument('--text',nargs='+',help='多条中文或英文描述')
    query.add_argument('--reference',help='参考图像路径')
    search.add_argument('--style',action='store_true',help='参考图使用相近风格模式')
    search.add_argument('--combine',choices=['any','all'],default='any')
    search.add_argument('--limit',type=input_int,default=200)
    search.add_argument('--refresh',action='store_true',help='重新扫描，增量更新索引')
    search.add_argument('--refine',action='store_true',help='文字搜索精细核对前 60 项')
    search.add_argument('--device',choices=['auto','cuda','cpu'],default='auto')
    search.add_argument('--offline',action='store_true')
    search.add_argument('--index-dir',help='指定索引目录，默认项目 .jobs/search')
    download = sub.add_parser("download-model", help="预下载模型，便于离线运行")
    download.add_argument("--model-dir", default=Config().model_dir)
    download.add_argument('--scheme',choices=['photo','people','creative','meme'],default='photo',help='预下载所选内容筛选使用的模型组')
    run = sub.add_parser("run", help="扫描、筛选并输出报告；显式加 --move 才剪切")
    run.add_argument("--input", nargs="+", required=True, help="一个或多个输入目录")
    run.add_argument("--output", required=True)
    run.add_argument('--scheme',choices=['photo','people','creative','meme'],default='photo',help='photo 实拍照片；people 人物/Cosplay/Live；meme 表情包；creative 绘画与设计')
    run.add_argument('--meme-select-threshold',type=input_float,default=.80)
    run.add_argument('--reblur',type=input_float,default=1,help='实拍再模糊指标上限，1 不据此排除')
    run.add_argument('--focus-reblur',type=input_float,default=.50,help='细节加权再模糊上限，1 不限制')
    run.add_argument('--focus-sharpness',type=input_float,default=25,help='细节加权清晰度下限，0 不限制')
    run.add_argument('--pbdb',type=input_float,default=0,help='分块锐度原始值下限，0 不限制')
    run.add_argument('--sharpness-mid',type=input_float,default=0,help='中位细节下限，0 不单独限制')
    run.add_argument('--quality-mode',choices=['normal','strict'],default='normal',help=argparse.SUPPRESS)
    run.add_argument('--creative-threshold',type=input_float,default=.85)
    run.add_argument('--creative-layout',choices=['flat','tree','theme'],default='flat')
    run.add_argument('--include-software',action='store_true',default=True,help=argparse.SUPPRESS)
    run.add_argument('--strict-boundaries',action='store_true',help='处理门槛采用严格 > / <；默认 ≥ / ≤')
    run.add_argument('--detail-threshold',type=input_float,default=0,help='旧拟合细节分下限（仅兼容旧设置，默认不用）')
    run.add_argument('--flat',action='store_true',help='实拍/人物筛选扁平输出并按内容去重，默认保留层级')
    run.add_argument('--manual-batch',action='store_true',help='关闭自动批量，使用 --batch-size 上限；默认手动 8')
    run.add_argument('--people-threshold',type=input_float,default=.97,help='人物语义初判下限，默认 .97（优先少误收）；非校准准确率')
    run.add_argument('--people-verify-threshold',type=input_float,default=.95,help='常规主体复核下限，默认 .95；另默认开启类型/人体补判，可独立入选')
    run.add_argument('--nearby',action='store_true',help='人物方案可选时间邻域弱辅助，默认关闭')
    run.add_argument('--no-people-rescue',action='store_true',help='关闭人物类型/人体定位补判，仅使用常规两道门槛')
    run.add_argument("--move", action="store_true", help="剪切入选文件；默认仅预览")
    run.add_argument("--min-side", type=input_int, default=None, help="宽和高均须达到此值；默认 400，表情包默认 0 不限")
    run.add_argument("--photo-threshold", type=input_float, default=.55)
    run.add_argument("--meme-threshold", type=input_float, default=.50, help="实拍用途核对的后期反应/文字分上限")
    run.add_argument("--rescan", action="store_true", help="重新扫描目录；默认中继未完成任务")
    run.add_argument("--sharpness", type=input_float, default=0)
    run.add_argument("--blockiness", type=input_float, default=1.7)
    run.add_argument("--batch-size", type=input_int, default=None,help='填写即使用手动批量上限 1–4096；语义模型单次最多 32；不填则按显存自动设置')
    run.add_argument("--workers", type=input_int, default=2,help='CPU 解码线程 1–16（CUDA 也由 CPU 解码），默认 2；不控制 GPU 线程')
    run.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    run.add_argument("--offline", action="store_true")
    run.add_argument("--sniff-all", action="store_true", help="尝试按内容识别任意扩展名的图像（较慢）")
    run.add_argument('--backup-duplicates',action='store_true',help='符合筛选条件的重复副本也移至输出的重复副本备份目录')
    run.add_argument("--model-dir", default=Config().model_dir)
    run.add_argument("--max-pixels", type=input_int, default=80_000_000)
    restore = sub.add_parser("restore", help="按移动记录恢复原路径，已有文件绝不覆盖")
    restore.add_argument("--output")
    restore.add_argument("--run-id", help="只恢复指定运行；省略则恢复所有尚未恢复的剪切")
    restore.add_argument('--task',choices=['photo','people','creative','meme','flatten','small','file_size','group'],default='photo')
    restore.add_argument('--input',nargs='+',help='恢复命名分组时提供原输入目录')
    restore.set_defaults(output=None)
    # Other tools have independent defaults and state databases.
    for name in ('small','file_size','flatten','group','compress','repair'):
        tool=sub.add_parser(name,help={'flatten':'所有文件扁平整理','small':'按像素尺寸筛选图像','file_size':'按磁盘文件体积筛选图像','group':'按命名方法就地分组','compress':'大图压缩副本，保留原图','repair':'生成 Windows 照片兼容副本，保留原件'}[name])
        tool.add_argument('--input',nargs='+',required=True)
        if name!='group':tool.add_argument('--output',required=name not in ('small','file_size'),help='兼容副本输出目录' if name=='repair' else '剪切的目标目录；像素/体积筛选仅预览时可省略')
        tool.add_argument('--move',action='store_true',help='生成兼容副本，原件保留' if name=='repair' else '执行操作；压缩功能生成副本，原图保留')
        tool.add_argument('--rescan',action='store_true')
        if name in ('small','file_size','compress','repair'):tool.add_argument('--flat',action='store_true',help='输出不保留层级')
        if name not in ('flatten','repair'):tool.add_argument('--strict-boundaries',action='store_true',help='处理门槛采用严格 > / <；默认 ≥ / ≤')
        if name=='flatten':tool.add_argument('--no-dedup',action='store_true',help='不做内容去重')
        if name in ('flatten','small','file_size'):tool.add_argument('--backup-duplicates',action='store_true',help='符合本页条件的重复副本剪切入输出的重复副本备份目录')
        if name=='small':tool.add_argument('--min-side',type=input_int,default=400)
        if name=='file_size':
            from .size_rules import DEFAULT_RULES
            tool.add_argument('--rules',dest='file_size_rules',default=DEFAULT_RULES,help='例如 "<400 KB OR >4 MB" 或 ">1 MB AND <4 MB"；KB=1000 B，MB=1000000 B')
        if name=='repair':
            tool.add_argument('--repair-all',action='store_true',help='强制生成所选范围静态图像的 PNG 显示副本')
            tool.add_argument('--max-pixels',type=input_int,default=80_000_000)
        if name=='group':
            tool.add_argument('--rare-count',type=input_int,default=5)
            tool.add_argument('--rare-ratio',type=input_float,default=.02)
        if name=='compress':
            tool.add_argument('--min-mb',type=input_float,default=12)
            tool.add_argument('--min-mp',type=input_float,default=10)
            tool.add_argument('--max-aspect',type=input_float,default=16/9)
            tool.add_argument('--quality',type=input_int,default=95)
            tool.add_argument('--max-pixels',type=input_int,default=80_000_000)
        if name not in ('flatten','repair'):tool.add_argument('--sniff-all',action='store_true')
    hashing=sub.add_parser('hash',help='SHA256 去重：输出优先保留一份，重复副本进入回收站')
    hashing.add_argument('--input',nargs='+',required=True);hashing.add_argument('--output')
    methods=hashing.add_mutually_exclusive_group()
    methods.add_argument('--reference-dir',dest='std_path',default='',help='将输入中的此目录作为只读基准；只回收其他输入的同哈希图像，无需输出目录')
    methods.add_argument('--in-place',action='store_true',help='各文件保留原位，回收重复副本；--input 的目录从左到右优先保留，无需输出目录')
    hashing.add_argument('--move',action='store_true');hashing.add_argument('--rescan',action='store_true')
    args = parser.parse_args(argv)
    if args.command == "gui":
        from .gui import main as gui_main
        gui_main()
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    def emit(event):
        if event["kind"] == "log":
            print(event["message"], flush=True)
        elif event["kind"] == "scan":
            print(f"已扫描 {event['scanned']:,} 个文件", flush=True)
        elif event["kind"] == "progress":
            print(f"已处理 {number(event['processed'])}/{number(event['total'])}；{event['counts']}", flush=True)
        elif event['kind']=='estimate':
            from .runtime import duration
            print(f"{event.get('phase','处理')}预计剩余 {duration(event['remaining_seconds'])}，本阶段约 {duration(event['total_seconds'])}。",flush=True)
    try:
        if args.command=='search':
            from .search_model import SearchConfig
            from .search import SearchEngine
            if args.style and not args.reference:raise ValueError('--style 需与 --reference 一起使用。')
            cfg=SearchConfig(mode='text' if args.text else 'style' if args.style else 'similar',prompts=tuple(args.text or ()),
                reference=args.reference or '',combine=args.combine,limit=args.limit,refine=args.refine,device=args.device,offline=args.offline)
            summary=SearchEngine(cfg,emit,stop,index_dir=args.index_dir).run(args.input,args.output,args.refresh)
            summary['results']=[{k:v for k,v in r.items() if k!='thumb'} for r in summary['results']]
        elif args.command == "download-model":
            from .vision import get_search_encoder,get_semantic_core,get_clip_core,release_models
            cfg=Config(scheme=args.scheme,model_dir=args.model_dir)
            core=get_semantic_core(cfg,print)
            if args.scheme!='photo':get_clip_core(cfg,print)
            if args.scheme=='people':
                get_search_encoder(cfg,print)
                from .person_detector import PersonDetector
                core.detector=PersonDetector(cfg,core.device,print)
            release_models()
            print("模型已缓存；之后可以使用 --offline。")
            return 0
        elif args.command == "restore":
            from .tasks import TaskRunner,TaskConfig
            restore_runs=None
            if args.task=='group':
                if not args.input:raise ValueError('命名分组恢复需要 --input。')
                roots,_,metadata=TaskRunner(TaskConfig(task='group')).locations(args.input,None)
                output=None
            else:
                if not args.output:raise ValueError('恢复需要 --output。')
                output = Path(args.output).expanduser().resolve(strict=True)
                if args.task in ('photo','people','creative','meme'):
                    metadata,restore_runs=content_restore_plan(output,args.task,args.run_id)
                else:metadata=output/'.photocleaner'/args.task
            if metadata.is_symlink() or not (metadata / "state.sqlite3").is_file():
                raise ValueError("未找到该输出目录的剪切记录。")
            with ExitStack() as locks:
                if output and metadata!=output/'.photocleaner':locks.enter_context(output_lock(output/'.photocleaner'))
                locks.enter_context(output_lock(metadata))
                store = Store(metadata / "state.sqlite3")
                try:
                    if restore_runs is None:summary=restore_moves(store,output,print,stop,args.run_id)
                    else:
                        print(f'恢复范围：{args.task}，{len(restore_runs)} 次运行；记录：{metadata}')
                        summary={'restored':0,'errors':0,'stopped':False}
                        for identity in restore_runs:
                            if stop.is_set():break
                            part=restore_moves(store,output,print,stop,identity)
                            for key in ('restored','errors'):summary[key]+=part[key]
                        summary['stopped']=stop.is_set()
                    store.export("restore", metadata)
                finally:
                    store.close()
        elif args.command!='run':
            from .tasks import TaskConfig,TaskRunner
            values={key:getattr(args,key) for key in TaskConfig.__dataclass_fields__ if hasattr(args,key)}
            values.update(task=args.command,retain_tree=not getattr(args,'flat',False),dedup=not getattr(args,'no_dedup',False),inclusive=not getattr(args,'strict_boundaries',False))
            if args.command=='hash':values['hash_mode']='inplace' if args.in_place else 'std' if args.std_path else 'consolidate'
            config=TaskConfig(**values)
            runner=TaskRunner
            if args.command=='hash':
                from .hash_task import HashRunner
                runner=HashRunner
            output=getattr(args,'output',None)
            if args.command in ('small','file_size') and not output:
                if args.move:raise ValueError('执行剪切前请选择 --output 输出目录。')
                from .engine import normalize_inputs
                from .preview_state import preview_output
                output=str(preview_output(args.command,normalize_inputs(args.input)))
            summary=runner(config,emit,stop).run(args.input,output,'move' if args.move else 'preview',resume=not args.rescan)
        else:
            config = Config(scheme=args.scheme,retain_tree=not args.flat,people_threshold=args.people_threshold,people_verify_threshold=args.people_verify_threshold,people_rescue=not args.no_people_rescue,nearby=args.nearby,min_side=args.min_side if args.min_side is not None else 0 if args.scheme=='meme' else 400, photo_threshold=args.photo_threshold, meme_threshold=args.meme_threshold,meme_select_threshold=args.meme_select_threshold,reblur_threshold=args.reblur,
                            sharpness_threshold=args.sharpness,sharpness_mid_threshold=args.sharpness_mid,quality_mode='normal',focus_reblur_threshold=args.focus_reblur,focus_sharpness_threshold=args.focus_sharpness,pbdb_threshold=args.pbdb,inclusive=not args.strict_boundaries,detail_threshold=args.detail_threshold, blockiness_threshold=args.blockiness,
                            batch_size=args.batch_size if args.batch_size is not None else 8,auto_batch=not(args.manual_batch or args.batch_size is not None),creative_threshold=args.creative_threshold,creative_layout=args.creative_layout,include_software=args.include_software, workers=args.workers, device=args.device,
                            offline=args.offline, sniff_all=args.sniff_all, model_dir=args.model_dir,
                            max_pixels=args.max_pixels,backup_duplicates=args.backup_duplicates)
            summary = Engine(config, emit, stop).run(args.input, args.output, "move" if args.move else "preview",resume=not args.rescan)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if 'counts' in summary:
            from .messages import completion_text
            print(completion_text('photo' if args.command=='run' else args.command,summary))
        if summary.get("stopped"):
            return 130
        if summary.get("fatal_error") or summary.get("errors") or summary.get("scan_issues") or any(summary.get("counts", {}).get(k, 0) for k in ("error", "review")):
            return 2
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

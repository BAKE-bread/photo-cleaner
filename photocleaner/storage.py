"""Durable task journals, evidence and interrupted-operation recovery records.

The runner owns a writable Store under the task/output lock. Result windows
use separate read-only SQLite connections and never share this connection.
"""
from contextlib import contextmanager
from pathlib import Path
import csv
import json
import os
import sqlite3
import time

REASONS = {
    'inplace_keeper':'按目录优先级保留，文件维持原路径',
    'inplace_duplicate':'SHA256 相同，多余副本进入回收站，保留文件维持原路径',
    'repair_unneeded':'Windows 系统解码通过且后缀可用，无需自动修复',
    'repair_unverified':'Windows 系统检查未完成，未自动认定需要修复',
    'empty_file':'空文件，没有可恢复的图像数据',
    'repair_multiframe':'特殊多帧格式或强制重编码多帧图像，保留原件',
    'repaired':'兼容副本已生成，原件保留',
    'std_reference':'基准图像，保留原处',
    'std_duplicate':'与基准图像的 SHA256 相同，副本进入回收站',
    'std_unmatched':'基准目录中没有相同 SHA256，保留原处',
    'not_file_size':'文件体积不符合筛选条件，留在原处',
    'low_detail_score':'综合细节分未达到下限，留在原处',
    'low_mid_detail':'中位局部细节未达到下限，留在原处',
    'hash_kept':'输出优先保留的代表文件 / 非本次输入文件',
    'hash_duplicate':'SHA256 相同，多余副本将进入回收站',
    'not_creative':'创作类别或入选条件未通过，留在原处',
    'not_meme':'表情包语义或入选分数未通过，留原处',
    'soft_detail':'全图再模糊条件未通过，留原处',
    'soft_focus_detail':'细节加权再模糊条件未通过，留原处',
    'low_pbdb':'分块锐度低于下限，留原处',
    'low_focus_sharpness':'细节加权清晰度低于下限，留原处',
    "too_small": "像素尺寸未满足入选条件", "animated_or_multiframe": "动图或多帧图像",
    "too_many_pixels": "超过单图解码像素上限", "blurred": "清晰度低",
    "blocky": "块效应明显", "not_photo": "主要内容判断为非照片",
    "duplicate_variant": "内容相同的图像副本，留在原处",
    'backup_duplicate':'符合筛选条件且内容相同，移至输出的重复副本备份目录',
    "non_image_extension": "其他文件类型（未尝试图像解码）",
    "unreadable_or_unsupported": "无法解码、损坏或不支持的格式",
    "changed": "扫描后源文件发生变化", "error": "处理失败，源文件保留",
    "review": "中断后需要复查，保留现有文件", "selected": "符合筛选条件",
    "exact_duplicate": "内容完全相同，副本留在原处", "not_small": "像素尺寸不在本轮移出范围，留在原处",
    "not_large": "文件体积或像素数不满足压缩条件", "wide_aspect": "长短边比例超过上限，原图保留",
    "compressed": "已生成 JPG，原图保留", "meme": "表情包、字幕截图或反应图，留在原处",
    'not_people':'常规人物筛选及已启用的补判均未通过，或属于明确排除场景，留在原处',
    'people_aspect':'长宽比超过语义模型支持范围（200:1），留在原处',
    'already_grouped':'已在本轮应属分组，无需移动',
}


@contextmanager
def output_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "process.lock").open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("这个任务目录正在使用；请等待相关任务完成或暂停后再继续。") from exc
        yield
    finally:
        handle.close()  # Closing releases the lock. The lock file is retained.


class Store:
    def __init__(self, path):
        path=Path(path).absolute()
        if path.resolve()!=path:
            raise OSError('数据库路径不能包含符号链接或目录联接。')
        self.db = sqlite3.connect(path,timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, started REAL, config TEXT, mode TEXT,
            finished REAL, summary TEXT);
        CREATE TABLE IF NOT EXISTS files (
            source TEXT PRIMARY KEY, root TEXT, relative TEXT, group_key TEXT,
            size INTEGER, mtime INTEGER, state TEXT, reason TEXT,
            metrics TEXT DEFAULT '{}', config_key TEXT, seen_run TEXT,
            thumb BLOB, pixel_digest TEXT, destination TEXT);
        CREATE INDEX IF NOT EXISTS files_group ON files(group_key, state);
        CREATE INDEX IF NOT EXISTS files_queue ON files(seen_run, state, source);
        CREATE INDEX IF NOT EXISTS files_root_state ON files(root,state,source);
        CREATE TABLE IF NOT EXISTS moves (
            id INTEGER PRIMARY KEY, run_id TEXT, source TEXT, destination TEXT,
            digest TEXT, size INTEGER, mtime INTEGER, phase TEXT,
            staging TEXT, error TEXT DEFAULT '', restored INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions (
            key TEXT PRIMARY KEY, run_id TEXT, scan_complete INTEGER DEFAULT 0,
            scanned INTEGER DEFAULT 0, scan_issues INTEGER DEFAULT 0, status TEXT);
        CREATE TABLE IF NOT EXISTS content_index (digest TEXT PRIMARY KEY, path TEXT);
        CREATE TABLE IF NOT EXISTS copies (
            id INTEGER PRIMARY KEY, run_id TEXT, source TEXT, destination TEXT,
            digest TEXT, phase TEXT, staging TEXT, error TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS copies_source_phase ON copies(source,phase,id);
        CREATE TABLE IF NOT EXISTS scan_dirs (
            run_id TEXT, path TEXT, root TEXT, state TEXT,
            PRIMARY KEY(run_id,path));
        CREATE INDEX IF NOT EXISTS scan_pending ON scan_dirs(run_id,root,state,path);
        CREATE TABLE IF NOT EXISTS task_flags (
            run_id TEXT, key TEXT, done INTEGER,
            PRIMARY KEY(run_id,key));
        """)
        self.db.executescript('''
        CREATE INDEX IF NOT EXISTS moves_source_phase ON moves(source,phase,id);
        CREATE INDEX IF NOT EXISTS moves_run ON moves(run_id,id);
        ''')
        if 'mtime' not in {r[1] for r in self.db.execute('PRAGMA table_info(scan_dirs)')}:self.db.execute('ALTER TABLE scan_dirs ADD COLUMN mtime INTEGER')

    def session(self, key, resume, mode=None, reuse_preview=False):
        row = self.db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
        if not resume or not row:return None
        if row['status']!='finished':return row
        previous=self.db.execute('SELECT mode FROM runs WHERE id=?',(row['run_id'],)).fetchone()
        # Execute an unchanged preview snapshot without walking the tree again.
        return row if previous and previous[0]=='preview' and (mode=='move' or reuse_preview) else None

    def session_update(self, key, run_id, count, issues, scan_complete, status):
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
                        (key,run_id,int(scan_complete),count,issues,status))
        self.db.commit()

    def close(self):
        self.db.close()

    def snapshot_unchanged(self,run_id,scanned):
        """Validate a finished read-only snapshot using metadata, never decode.

        Directory stamps catch new/renamed members; per-file stamps also catch
        in-place edits which do not change their parent's directory timestamp.
        """
        from .moving import fingerprint
        if self.db.execute('SELECT COUNT(*) FROM files WHERE seen_run=?',(run_id,)).fetchone()[0]!=scanned:return False
        directories=self.db.execute('SELECT path,mtime,state FROM scan_dirs WHERE run_id=?',(run_id,)).fetchall()
        if not directories:return False
        for row in directories:
            try:
                path=Path(row['path'])
                if row['state']!='done' or row['mtime'] is None or path.resolve()!=path or path.stat().st_mtime_ns!=row['mtime']:return False
            except OSError:return False
        for row in self.db.execute('SELECT source,size,mtime FROM files WHERE seen_run=?',(run_id,)):
            try:
                path=Path(row['source'])
                if path.resolve()!=path or fingerprint(path)!=(row['size'],row['mtime']):return False
            except OSError:return False
        return True

    def register(self, source, root, stat, run_id, config_key, supported,preserve_evidence=False):
        existing = self.db.execute("SELECT size,mtime,state,config_key,reason,root FROM files WHERE source=?", (str(source),)).fetchone()
        if existing and existing['state']=='review' and (existing['size'],existing['mtime'])==(stat.st_size,stat.st_mtime_ns):
            self.db.execute('UPDATE files SET seen_run=?,root=?,relative=? WHERE source=?',(run_id,str(root),str(source.relative_to(root)),str(source)))
            return
        if existing and existing['root']!=str(root) and preserve_evidence and existing['state'] in ('selected','rejected','queued') and (existing['size'],existing['mtime'])==(stat.st_size,stat.st_mtime_ns):
            # Adding an ancestor to the input list changes the output-relative
            # base, not image pixels. Refresh the decision/path context while
            # preserving already-computed semantic and header evidence.
            state,reason=('queued','') if supported else ('rejected','non_image_extension')
            self.db.execute('UPDATE files SET state=?,reason=?,root=?,relative=?,config_key=?,seen_run=?,destination=NULL WHERE source=?',
                            (state,reason,str(root),str(source.relative_to(root)),config_key,run_id,str(source)))
            return
        if existing and existing['root']==str(root) and (existing["size"], existing["mtime"]) == (stat.st_size, stat.st_mtime_ns):
            if existing["state"] == "review" or (existing["config_key"] == config_key and existing["state"] in ("selected", "rejected", "copied", "grouped") and existing["reason"] not in ("duplicate_variant", "exact_duplicate")):
                self.db.execute("UPDATE files SET seen_run=? WHERE source=?", (run_id, str(source)))
                return
            if preserve_evidence and existing['state'] in ('selected','rejected','queued','copied','grouped'):
                self.db.execute("UPDATE files SET state=?,reason=?,config_key=?,seen_run=? WHERE source=?",('queued' if supported else 'rejected','' if supported else 'non_image_extension',config_key,run_id,str(source)))
                return
        group_key = os.path.normcase(str(source.parent / source.stem))
        state, reason = ("queued", "") if supported else ("rejected", "non_image_extension")
        self.db.execute("""INSERT INTO files(source,root,relative,group_key,size,mtime,state,reason,config_key,seen_run)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            root=excluded.root,relative=excluded.relative,group_key=excluded.group_key,
            size=excluded.size,mtime=excluded.mtime,state=excluded.state,reason=excluded.reason,
            config_key=excluded.config_key,seen_run=excluded.seen_run,metrics='{}',thumb=NULL,pixel_digest=NULL,destination=NULL""",
            (str(source), str(root), str(source.relative_to(root)), group_key,
             stat.st_size, stat.st_mtime_ns, state, reason, config_key, run_id))

    def decision(self, source, state, reason, metrics, thumb=None, digest=None):
        self.db.execute("UPDATE files SET state=?,reason=?,metrics=?,thumb=?,pixel_digest=? WHERE source=?",
                        (state, reason, json.dumps(metrics, ensure_ascii=False), thumb, digest, str(source)))

    def export(self, run_id, directory):
        csv_path = directory / f"report_{run_id}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            record=self.db.execute('SELECT config FROM runs WHERE id=?',(run_id,)).fetchone();config=json.loads(record[0]) if record else {}
            from .result_fields import FIELDS,TASK_FIELDS
            task=config.get('scheme') or config.get('task','photo')
            category={'people':'person_category_label','creative':'creative_theme','meme':'meme_category_label','group':'category'}
            fields=[]
            for key in TASK_FIELDS.get(task,[]):
                if task=='hash' and ((config.get('hash_mode') in ('std','inplace') and key in ('in_output','requested')) or (config.get('hash_mode')!='std' and key=='is_std')):continue
                if task=='hash' and key=='priority_rank' and config.get('hash_mode')!='inplace':continue
                title,_,metric=FIELDS[key]
                if key=='category':metric=category.get(task)
                if metric:fields.append((metric,title))
            writer.writerow(["源文件", "目标文件", "状态", "原因", "宽", "高",*[name for _,name in fields],"详细数据"])
            from .config import Config
            from .decisions import explain_decision
            cfg=Config(**{k:v for k,v in config.items() if k in Config.__dataclass_fields__}) if config.get('scheme') else None
            for row in self.db.execute("SELECT * FROM files WHERE seen_run=? ORDER BY source", (run_id,)):
                metrics = json.loads(row["metrics"])
                from .result_status import display_state
                writer.writerow([excel_safe(row["source"]), excel_safe(row["destination"] or ""), display_state(row,metrics),
                                 explain_decision(row['reason'],metrics,cfg) if cfg else REASONS.get(row["reason"], row["reason"]), metrics.get("width", ""), metrics.get("height", ""),
                                 *[excel_safe('' if metrics.get(key) is None else str(metrics.get(key,''))) if not (task=='photo' and key=='meme_score' and not metrics.get('category_scores',{}).get('photo_checks')) else '' for key,_ in fields],
                                 excel_safe(row["metrics"])])
        journal = directory / "move_history.csv"
        with journal.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ID", "运行ID", "源文件", "目标文件", "SHA256", "阶段", "已恢复", "临时文件", "错误"])
            for r in self.db.execute("SELECT * FROM moves ORDER BY id"):
                writer.writerow([r["id"], r["run_id"], excel_safe(r["source"]), excel_safe(r["destination"]),
                                 r["digest"], r["phase"], r["restored"], excel_safe(r["staging"] or ""), excel_safe(r["error"])])
        return csv_path


def excel_safe(value):
    # Paths beginning with '=' must not become formulas when the CSV is opened.
    return "'" + value if value and value[0] in "=+-@\t\r" else value

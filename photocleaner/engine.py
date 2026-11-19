"""Content-task lifecycle: durable scan → cached evidence → gates → safe moves.

Evidence identity excludes user thresholds and performance settings. Changing
a gate reuses compatible scores; file changes and model-rule changes invalidate
the relevant evidence. SQLite writes and transfers stay on the runner thread.
"""
from .numbers import number
from .pipeline import DecodeWindow,close_inspection
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import os
import threading
import time
import uuid

from PIL import Image

from .config import Config,PROJECT
from .moving import ensure_parents, execute_move, file_hash, fingerprint, recover_moves
from .storage import Store, output_lock
from .vision import get_classifier, inspect_image, same_content, model_session


def normalize_inputs(inputs):
    from .directory_lists import clean_path
    if not inputs:
        raise ValueError("请至少选择一个输入目录。")
    roots = []
    for value in inputs:
        value=clean_path(value)
        if not value:raise ValueError('输入目录不能为空。')
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"输入不是目录：{root}")
        if any(root == p or p in root.parents for p in roots):
            continue
        roots = [p for p in roots if root not in p.parents]
        roots.append(root)
    return sorted(roots,key=str)


def normalize_paths(inputs, output):
    from .directory_lists import clean_path
    roots=normalize_inputs(inputs)
    output = Path(clean_path(output)).expanduser().absolute()
    if output.is_symlink() or getattr(output, "is_junction", lambda: False)():
        raise ValueError("输出目录不能是符号链接或目录联接。")
    output = output.resolve()
    for root in roots:
        if output == root or output in root.parents:
            raise ValueError("输出不能等于输入目录，也不能是输入目录的祖先。可放在输入内的子目录。")
    return sorted(roots, key=str), output


def persistent_files(root,exclusions,stop,issue,skip_grouped,store,run_id):
    """A durable directory frontier; only the unfinished directory is reopened."""
    def excluded(path):return any(path==p or p in path.parents for p in exclusions)
    store.db.execute("INSERT OR IGNORE INTO scan_dirs(run_id,path,root,state) VALUES(?,?,?,'pending')",(run_id,str(root),str(root)))
    store.db.execute("UPDATE scan_dirs SET state='pending' WHERE run_id=? AND root=? AND state='error'",(run_id,str(root)))
    store.db.commit()
    while not stop.is_set():
        row=store.db.execute("SELECT path FROM scan_dirs WHERE run_id=? AND root=? AND state='pending' ORDER BY path LIMIT 1",(run_id,str(root))).fetchone()
        if not row:break
        directory=Path(row[0]);failed=False
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if stop.is_set():break
                    try:
                        path=Path(entry.path)
                        if entry.is_symlink() or getattr(path,'is_junction',lambda:False)():
                            issue(f'跳过链接：{path}');continue
                        if entry.is_dir(follow_symlinks=False):
                            internal=entry.name=='.photocleaner' or (skip_grouped and (path/'.photocleaner-group').is_file())
                            if not internal and not excluded(path):
                                store.db.execute("INSERT OR IGNORE INTO scan_dirs(run_id,path,root,state) VALUES(?,?,?,'pending')",(run_id,str(path),str(root)))
                        elif entry.is_file(follow_symlinks=False) and entry.name!='.photocleaner-group' and not excluded(path):
                            seen=store.db.execute('SELECT 1 FROM files WHERE source=? AND seen_run=?',(str(path),run_id)).fetchone()
                            if not seen:yield path,entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        issue(f'无法读取 {entry.path}：{exc}');failed=True
        except OSError as exc:
            issue(f'目录读取中断 {directory}：{exc}');failed=True
        if not stop.is_set():
            try:stamp=directory.stat().st_mtime_ns
            except OSError:stamp=None;failed=True
            store.db.execute('UPDATE scan_dirs SET state=?,mtime=? WHERE run_id=? AND path=?',('error' if failed else 'done',stamp,run_id,str(directory)))
        store.db.commit()


def stream_files(root, exclusions, stop, issue, skip_grouped=False,store=None,run_id=None):
    # Open iterators only along current traversal depth, no giant path lists.
    def excluded(path):
        return any(path == p or p in path.parents for p in exclusions)

    if excluded(root) or (skip_grouped and (root/'.photocleaner-group').is_file()):
        return
    if store is not None:
        yield from persistent_files(root,exclusions,stop,issue,skip_grouped,store,run_id)
        return
    stack = []
    try:
        stack.append(os.scandir(root))
        while stack and not stop.is_set():
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close()
                continue
            except OSError as exc:
                issue(str(exc))
                stack.pop().close()
                continue
            try:
                path = Path(entry.path)
                if entry.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    issue(f"跳过链接：{path}")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    internal = entry.name == '.photocleaner' or (skip_grouped and (path/'.photocleaner-group').is_file())
                    if not internal and not excluded(path):
                        stack.append(os.scandir(path))
                elif entry.is_file(follow_symlinks=False) and entry.name != '.photocleaner-group' and not excluded(path):
                    yield path, entry.stat(follow_symlinks=False)
            except OSError as exc:
                issue(f"无法读取 {entry.path}：{exc}")
    finally:
        for iterator in stack:
            iterator.close()


class Engine:
    def __init__(self, config=None, emit=None, stop=None, classifier_factory=get_classifier):
        self.config = config or Config()
        self.emit = emit or (lambda event: None)
        self.stop = stop or threading.Event()
        self.classifier_factory = classifier_factory
        self.classifier = None
        self.config_key=self.config.key()
        self.rescored_count=0
        self.evidence_key=self.config.evidence_key()
        self.scan_issues = 0

    def log(self, message):
        self.emit({"kind": "log", "message": message})

    def issue(self, message):
        self.scan_issues += 1
        if self.scan_issues <= 100:
            self.log(message)

    def _device_event(self):
        if self.classifier is None:return
        import sys
        from .runtime import device_memory
        core=getattr(self.classifier,'core',None)
        torch=getattr(core,'torch',None) or sys.modules.get('torch')
        device=getattr(self.classifier,'device','cpu')
        description=getattr(self.classifier,'device_description',device)
        if torch is not None:
            try:description=device_memory(torch,device)
            except (RuntimeError,AttributeError):pass
        self.emit({'kind':'device','batch':getattr(self.classifier,'actual_batch',self.config.batch_size),'description':description})

    def run(self, inputs, output, mode="preview", resume=True):
        self.config.validate()
        self.rescored_count=0
        if mode not in ("preview", "move"):
            raise ValueError("模式应为 preview 或 move。")
        roots, output = normalize_paths(inputs, output)
        self.log('运行提示：请勿在程序运行时对相关文件夹自行操作。')
        output.mkdir(parents=True, exist_ok=True)
        journal_root=output/'.photocleaner'
        metadata=journal_root/self.config.scheme
        if metadata.resolve()!=metadata.absolute():
            raise ValueError("记录目录不能是链接。")
        self.scan_issues = 0
        with output_lock(journal_root), model_session():
            legacy=journal_root/'state.sqlite3'
            if legacy.is_file():
                from .preview_state import import_decisions
                imported=import_decisions(legacy,metadata,scheme=self.config.scheme,evidence_key=self.evidence_key)
                if imported:self.log(f'已有记录迁移：复用 {number(imported,group=True)} 条本功能证据；原数据库与操作记录保留。')
            metadata.mkdir(parents=True,exist_ok=True)
            store = Store(metadata / "state.sqlite3")
            try:
                summary=self._run(store, roots, output, metadata, mode, resume)
            finally:
                if self.classifier is not None:
                    close=getattr(self.classifier,'close',None)
                    if close:close()
                self.classifier=None
                store.close()
        self.emit({"kind":"done",**summary})
        return summary

    def _run(self, store, roots, output, metadata, mode, resume=True):
        session_key = hashlib.sha256(json.dumps([list(map(str,roots)),str(output),self.config.scheme,'file-list-v1']).encode()).hexdigest()
        session = store.session(session_key, resume, mode,reuse_preview=True)
        if not session:self._promote_legacy_evidence(store)
        if session and session['scan_complete']:
            previous=store.db.execute('SELECT mode FROM runs WHERE id=?',(session['run_id'],)).fetchone()
            if previous and previous[0]=='preview' and not self._snapshot_unchanged(store,session):session=None
        run_id = session['run_id'] if session else time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        store.db.execute("INSERT INTO runs(id,started,config,mode) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,config=excluded.config",
                         (run_id, time.time(), json.dumps(asdict(self.config)), mode))
        store.db.commit()
        recover_moves(store, self.log)
        if session:
            store.db.execute("UPDATE files SET state='queued',reason='' WHERE seen_run=? AND (config_key<>? OR COALESCE(json_extract(CASE WHEN json_valid(metrics) THEN metrics ELSE '{}' END,'$.evidence_key'),'')<>?) AND state IN ('selected','rejected') AND (reason<>'non_image_extension' OR ?)",(run_id,self.config_key,self.evidence_key,self.config.sniff_all))
            store.db.execute('UPDATE files SET config_key=? WHERE seen_run=? AND state<>?',(self.config_key,run_id,'review'))
        self.emit({'kind':'snapshot','run_id':run_id,'mode':mode,'scanned':0,'report':str(metadata/f'report_{run_id}.csv'),'metadata':str(metadata),'output':str(output),'config':asdict(self.config),'live':True})
        need_scan=not session or not session['scan_complete']
        self.log("扫描中继：跳过已完成目录，仅继续未完成目录。" if session and need_scan else "继续上次任务：复用已完成扫描与判定，仅处理剩余文件。" if session else "扫描阶段：递归读取目录，清单写入磁盘数据库。")
        supported = set(Image.registered_extensions()) | {".heic", ".heif", ".avif"}
        count = store.db.execute('SELECT COUNT(*) FROM files WHERE seen_run=?',(run_id,)).fetchone()[0] if session else 0
        self.scan_issues = session['scan_issues'] if session else 0
        exclusions = [output, Path(self.config.model_dir).resolve(),PROJECT/'.jobs']
        store.session_update(session_key,run_id,count,self.scan_issues,not need_scan,'running')
        for root in (roots if need_scan else []):
            for source, stat in stream_files(root, exclusions, self.stop, self.issue,store=store,run_id=run_id):
                eligible = self.config.sniff_all or not source.suffix or source.suffix.lower() in supported
                store.register(source, root, stat, run_id, self.config_key, eligible,preserve_evidence=True)
                count += 1
                if count % 1000 == 0:
                    store.session_update(session_key,run_id,count,self.scan_issues,False,'running')
                    self.emit({"kind": "scan", "scanned": count})
            if self.stop.is_set():
                break
        store.db.commit()
        # register() may reuse a same-config decision on a fresh scan. Prompt /
        # evidence changes have their own identity and must be checked here as
        # well as when resuming a session. Keep raw evidence for cheap re-decides.
        store.db.execute("UPDATE files SET state='queued',reason='' WHERE seen_run=? AND (config_key<>? OR COALESCE(json_extract(CASE WHEN json_valid(metrics) THEN metrics ELSE '{}' END,'$.evidence_key'),'')<>?) AND state IN ('selected','rejected') AND (reason<>'non_image_extension' OR ?)",
                         (run_id,self.config_key,self.evidence_key,self.config.sniff_all))
        store.db.execute('UPDATE files SET config_key=? WHERE seen_run=? AND state<>?',(self.config_key,run_id,'review'))
        store.db.commit()
        scan_errors=store.db.execute("SELECT COUNT(*) FROM scan_dirs WHERE run_id=? AND state='error'",(run_id,)).fetchone()[0]
        scan_complete = not self.stop.is_set() and not scan_errors
        store.session_update(session_key,run_id,count,self.scan_issues,scan_complete,'running')
        self.log(f"扫描结束：{number(count,group=True)} 个文件；开始筛选。")
        self.flat_dedup=None
        if self.config.backup_duplicates or (self.config.scheme=='creative' and self.config.creative_layout!='tree') or (self.config.scheme!='creative' and not self.config.retain_tree):
            from .flat_dedup import FlatDedup
            self.log('内容去重：符合筛选条件的重复副本'+('移至输出的重复副本备份目录。' if self.config.backup_duplicates else '留在原处。'))
            self.flat_dedup=FlatDedup(store,output,self.stop,self.issue,self.emit)
        self.emit({'kind':'scan','scanned':count,'complete':scan_complete})
        self.emit({'kind':'snapshot','run_id':run_id,'mode':mode,'scanned':count,'report':str(metadata/f'report_{run_id}.csv'),'metadata':str(metadata),'output':str(output),'config':asdict(self.config),'live':True})
        if self.rescored_count:self.log(f'复用判别证据：{number(self.rescored_count,group=True)} 张仅按新门槛重判，无需再次解码或模型推理。')
        queue_states="('queued','selected')" if mode=='move' else "('queued')"
        total = store.db.execute(f"SELECT COUNT(*) FROM files WHERE seen_run=? AND state IN {queue_states}", (run_id,)).fetchone()[0]
        live_counts = self._counts(store, run_id)
        processed = 0
        last_source = ""
        fatal_error = None
        try:
            if scan_errors:raise OSError('部分目录读取未完成；文件清单已保留，重连或解决权限问题后点击继续。')
            # One look-ahead batch overlaps decode with inference and journaling.
            # The richer people inputs use smaller CPU windows, independently
            # of the user's upper bound for GPU micro-batches.
            window_size=(64 if self.config.scheme=='photo' else 32) if self.config.auto_batch else min(self.config.batch_size,64)
            from .runtime import Estimate
            estimate=None
            timing={'decode_worker_seconds':0.0,'decode_wait_seconds':0.0,'model_seconds':0.0,'write_seconds':0.0,'batches':0}
            with DecodeWindow(self.config.workers,self.stop,inspect_image,cached=self._cached_inspection) as decoder:
                def schedule():
                    nonlocal last_source
                    batch=store.db.execute(f"SELECT * FROM files WHERE seen_run=? AND state IN {queue_states} AND source>? ORDER BY source LIMIT ?",
                                           (run_id,last_source,window_size)).fetchall()
                    if not batch:return False
                    last_source=batch[-1]['source']
                    flags=[row['state']=='queued' and store.db.execute('SELECT 1 FROM files WHERE group_key=? AND source<>? LIMIT 1',
                           (row['group_key'],row['source'])).fetchone() is not None for row in batch]
                    decoder.submit(batch,flags,self.config)
                    return True
                schedule();schedule()
                while decoder.batches and not self.stop.is_set():
                    started=time.perf_counter();rows,items,decode_seconds=decoder.take()
                    timing['decode_wait_seconds']+=time.perf_counter()-started;timing['decode_worker_seconds']+=decode_seconds;timing['batches']+=1
                    if self.stop.is_set():break
                    queued=[row for row in rows if row['state']=='queued']
                    results=[item for row,item in zip(rows,items) if row['state']=='queued']
                    for item in results:
                        if getattr(item,'cached',False):self.rescored_count+=1
                        else:
                            item.metrics['inspection_reason']=item.reason
                            item.metrics['evidence_key']=self.evidence_key
                    candidates = [(row, item) for row, item in zip(queued, results) if not item.reason and item.image is not None]
                    if candidates and not self.stop.is_set():
                        if self.classifier is None:
                            self.classifier = self.classifier_factory(self.config, self.log)
                            self._device_event()
                        if estimate is None:estimate_base=processed;estimate=Estimate(total-processed)
                        if not self.stop.is_set():
                            model_started=time.perf_counter()
                            previous_batch=getattr(self.classifier,'actual_batch',self.config.batch_size)
                            predictions = (self.classifier.classify_people if self.config.scheme=='people' else self.classifier.classify)([item.image for _, item in candidates])
                            core=getattr(self.classifier,'core',None)
                            if core is not None and hasattr(core,'micro_batch') and core.micro_batch<self.classifier.actual_batch:
                                self.classifier.actual_batch=core.micro_batch
                            if getattr(self.classifier,'actual_batch',previous_batch)!=previous_batch:self._device_event()
                            timing['model_seconds']+=time.perf_counter()-model_started
                            if len(predictions) != len(candidates):
                                raise RuntimeError("模型返回的结果数量错误。")
                            for (_, item), scores in zip(candidates, predictions):
                                from .decisions import apply_scores
                                item.reason=apply_scores(scores,self.config,item.metrics)
                    write_started=time.perf_counter()
                    for row, item in zip(queued, results):
                        # Persist a fully returned batch even when pause was
                        # requested during inference. Never select an image
                        # whose inference was skipped because of that pause.
                        if not item.reason and not item.metrics.get('category_scores') and not item.metrics.get('creative_gif'):continue
                        try:
                            if fingerprint(Path(row["source"])) != (row["size"], row["mtime"]):
                                item.reason = "changed"
                        except OSError as exc:
                            item.reason = "changed"
                            item.metrics["error"] = str(exc)
                        if not item.reason and self._duplicate(store, row, item):
                            item.reason = 'backup_duplicate' if self.config.backup_duplicates else 'duplicate_variant'
                        state='selected' if not item.reason or item.reason=='backup_duplicate' else 'rejected'
                        store.decision(row["source"], state,
                                       item.reason or "selected", item.metrics, item.thumbnail, item.pixel_digest)
                        if item.metrics.get('exif_warning'):
                            self.log(f"元数据提示：{Path(row['source']).name} 的 EXIF 不完整；图像像素仍按正常解码结果处理，详情已记入报告。")
                        live_counts["queued"] = live_counts.get("queued", 0)-1
                        new_state = state
                        live_counts[new_state] = live_counts.get(new_state, 0)+1
                    store.db.commit()
                    for item in results:close_inspection(item)
                    for row in rows:
                        if self.stop.is_set():
                            break
                        current = store.db.execute("SELECT * FROM files WHERE source=?", (row["source"],)).fetchone()
                        if current["state"] == "selected" and mode == "move" and self.config.scheme!='people':
                            self._move(store, current, output, run_id)
                            after = store.db.execute("SELECT state FROM files WHERE source=?", (row["source"],)).fetchone()[0]
                            live_counts["selected"] -= 1
                            live_counts[after] = live_counts.get(after, 0)+1
                        processed += 1
                    timing['write_seconds']+=time.perf_counter()-write_started
                    if not self.stop.is_set():schedule()
                    self.emit({"kind": "progress", "processed": processed, "total": total,
                               "counts": {k: v for k, v in live_counts.items() if v}})
                    if estimate is not None:
                        eta=estimate.update(processed-estimate_base)
                        if eta:
                            self._device_event()
                            self.emit({'kind':'estimate',**eta,'phase':'筛选'})
            if self.config.scheme=='people' and not self.stop.is_set():
                if self.config.nearby:
                    from .people import nearby_review
                    promoted=nearby_review(store,run_id,self.config.people_threshold)
                    demoted=store.db.execute('SELECT demoted FROM person_context_stats').fetchone()[0]
                    self.log(f'邻域辅助：提升 {promoted} 张临界图像；排除 {demoted} 张更符合连续环境/纪实场景的图像。')
                # A promoted borderline image still obeys the original small
                # duplicate rule, in both preview and execution.
                from .vision import Inspection
                for row in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' ORDER BY source",(run_id,)):
                    if self.stop.is_set():break
                    item=Inspection(metrics=json.loads(row['metrics']),thumbnail=row['thumb'],pixel_digest=row['pixel_digest'])
                    if row['reason']=='backup_duplicate':continue
                    if self._duplicate(store,row,item):store.decision(row['source'],'selected' if self.config.backup_duplicates else 'rejected','backup_duplicate' if self.config.backup_duplicates else 'duplicate_variant',item.metrics,row['thumb'],row['pixel_digest'])
                store.db.commit()
                if mode=='move' and not self.stop.is_set():
                    remaining=store.db.execute("SELECT COUNT(*) FROM files WHERE seen_run=? AND state='selected'",(run_id,)).fetchone()[0]
                    moved=0
                    move_estimate=Estimate(remaining)
                    for current in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' ORDER BY source",(run_id,)):
                        if self.stop.is_set():break
                        self._move(store,current,output,run_id);moved+=1
                        self.emit({'kind':'progress','phase':'人物图像剪切','processed':moved,'total':remaining,'counts':{}})
                        eta=move_estimate.update(moved)
                        if eta:self.emit({'kind':'estimate','phase':'人物图像剪切',**eta})
        except Exception as exc:
            fatal_error = RuntimeError('CUDA 显存不足；请将当前模型批量减半后继续，必要时再次减半。CPU 解码线程不直接减少推理显存。已完成判定与迁移保留。') if type(exc).__name__=='OutOfMemoryError' else exc
            self.log(f"处理已停止，已完成的记录保留，源文件不会因错误被丢弃：{fatal_error}")
        finally:
            counts = self._counts(store, run_id)
            summary = {"run_id": run_id, "mode": mode, "scanned": count, "counts": counts,
                       "scan_issues": self.scan_issues, "stopped": self.stop.is_set(),
                       "fatal_error": str(fatal_error) if fatal_error else None}
            summary['scheme']=self.config.scheme
            summary.update(metadata=str(metadata),output=str(output))
            summary['roots']=list(map(str,roots))
            summary['config']=asdict(self.config)
            summary['reused_scores']=self.rescored_count
            summary['snapshot_reused']=bool(session and not need_scan)
            if self.rescored_count:self.log(f'复用判别证据：{number(self.rescored_count,group=True)} 张直接重判，未解码或运行模型。')
            if 'timing' in locals():
                summary['timing']={k:round(v,3) if isinstance(v,float) else v for k,v in timing.items()}
                self.log(f"阶段耗时 | 等待解码 {number(timing['decode_wait_seconds'])}s | 模型/预处理 {number(timing['model_seconds'])}s | 判定写入/迁移 {number(timing['write_seconds'])}s | 解码线程累计 {number(timing['decode_worker_seconds'])}s（并行累计，不与前项相加）")
            summary['reasons']={r[0]:r[1] for r in store.db.execute('SELECT reason,COUNT(*) FROM files WHERE seen_run=? GROUP BY reason',(run_id,))}
            store.db.execute("UPDATE runs SET finished=?,summary=? WHERE id=?", (time.time(), json.dumps(summary, ensure_ascii=False), run_id))
            store.db.commit()
            store.session_update(session_key,run_id,count,self.scan_issues,scan_complete,
                                 'paused' if self.stop.is_set() else 'error' if fatal_error else 'finished')
            report = store.export(run_id, metadata)
            summary["report"] = str(report)
            summary["history"] = str(metadata / "move_history.csv")
        return summary

    def _counts(self, store, run_id):
        return {row[0]: row[1] for row in store.db.execute("SELECT state,COUNT(*) FROM files WHERE seen_run=? GROUP BY state", (run_id,))}

    def _promote_legacy_evidence(self,store):
        # Upgrade only identifiable v5 records, never guess from a high score.
        query="SELECT f.source,f.metrics,f.config_key,r.config FROM files f JOIN runs r ON r.id=f.seen_run WHERE json_type(r.config,'$.quality_mode') IS NULL AND json_extract(r.config,'$.scheme')=?"
        promoted=0
        for row in store.db.execute(query,(self.config.scheme,)):
            try:
                data=json.loads(row['config']);cfg=Config(**{k:v for k,v in data.items() if k in Config.__dataclass_fields__})
                if row['config_key']!=cfg.key(legacy_v5=True):continue
                metrics=json.loads(row['metrics']);scores=metrics.get('category_scores',{})
                if cfg.scheme in ('photo','creative'):
                    raw=scores.get('media_scores',{})
                    if 'presentation_check' not in raw or 'meme_context_score' not in raw.get('meme_evidence',{}):continue
                    if cfg.scheme=='photo' and not all(k in metrics for k in ('sharpness','reblur','sharpness_mid')):continue
                elif cfg.scheme=='meme':
                    if 'meme_context_score' not in scores:continue
                else:
                    from dataclasses import replace
                    if not scores.get('semantic_model') or metrics.get('evidence_key')!=replace(cfg,people_threshold=.97,people_verify_threshold=.95,nearby=False).key():continue
                metrics.update(evidence_key=cfg.evidence_key(),inspection_reason='')
                store.db.execute('UPDATE files SET metrics=? WHERE source=?',(json.dumps(metrics,ensure_ascii=False),row['source']));promoted+=1
            except (ValueError,TypeError,KeyError):continue
        if promoted:store.db.commit();self.log(f'旧版 v5 证据迁移：{number(promoted,group=True)} 张保留已有模型分数和画质指标，仅补齐缓存版本标记。')

    def _snapshot_unchanged(self,store,session):
        return store.snapshot_unchanged(session['run_id'],session['scanned'])

    def _cached_inspection(self,row,duplicate):
        from .vision import Inspection
        try:metrics=json.loads(row['metrics'] or '{}')
        except (ValueError,TypeError):return None
        if not isinstance(metrics,dict):return None
        suffix=Path(row['source']).suffix.lower()
        if not self.config.sniff_all and suffix and suffix not in set(Image.registered_extensions())|{'.heic','.heif','.avif'}:
            item=Inspection(reason='non_image_extension',metrics=metrics)
            item.cached=True
            return item
        if metrics.get('evidence_key')!=self.evidence_key:return None
        try:
            path=Path(row['source'])
            if path.resolve()!=path or fingerprint(path)!=(row['size'],row['mtime']):return None
        except OSError:return None
        reason=metrics.get('inspection_reason','')
        if self.config.scheme=='creative' and metrics.get('format')=='GIF':
            if not metrics.get('creative_gif'):return None
            return Inspection(metrics=metrics)
        if self.config.scheme=='creative' and reason=='too_small':return None
        if reason in ('animated_or_multiframe','people_aspect','unreadable_or_unsupported'):pass
        elif reason in ('error','changed'):return None
        elif not __import__('photocleaner.thresholds',fromlist=['lower']).lower(min(metrics.get('width',0),metrics.get('height',0)),self.config.min_side,self.config) and self.config.scheme!='creative':reason='too_small'
        else:
            pixels=metrics.get('width',0)*metrics.get('height',0)
            if pixels>self.config.max_pixels and self.config.scheme in ('people','creative','meme') and metrics.get('format')=='JPEG' and not duplicate:
                # JPEG's supported reduced decode is part of the existing
                # safety policy. Query its header dimensions without decoding
                # pixels or throwing away already-computed semantic evidence.
                try:
                    with Image.open(path) as header:
                        header.draft('RGB',(1024,1024));pixels=header.width*header.height
                except (OSError,ValueError):return None
            if pixels>self.config.max_pixels:
                item=Inspection(reason='too_many_pixels',metrics=metrics,thumbnail=row['thumb'] or b'',pixel_digest=row['pixel_digest'] or '')
                item.cached=True
                return item
            scores=metrics.get('category_scores')
            if not scores:return None
            if self.config.scheme=='photo':
                from .quality import QUALITY_VERSION
                if not all(k in metrics for k in ('sharpness','reblur','sharpness_mid','pbdb','focus_reblur','focus_sharpness')) or metrics.get('quality_version')!=QUALITY_VERSION:return None
            if self.config.scheme=='people':
                from .decisions import people_decision
                accepted,_=people_decision(scores,self.config)
                if not scores.get('semantic_model') or (accepted and not scores.get('art_check')):return None
                if not accepted and self.config.people_rescue and 'rescue_scores' not in scores:
                    from .people_rescue import candidate
                    if candidate(scores):return None
            from .decisions import apply_scores
            reason=apply_scores(scores,self.config,metrics)
            if not reason and duplicate and not row['pixel_digest']:return None
        item=Inspection(reason=reason,metrics=metrics,thumbnail=row['thumb'] or b'',pixel_digest=row['pixel_digest'] or '')
        item.cached=True
        return item

    def _duplicate(self, store, row, item):
        item.metrics.pop('duplicate_of',None)
        if getattr(self,'flat_dedup',None) is not None:
            duplicate,digest=self.flat_dedup.duplicate(Path(row['source']),(row['size'],row['mtime']))
            item.metrics['sha256']=digest
            if duplicate:
                item.metrics['duplicate_of']=duplicate
                return True
        if not item.thumbnail:
            return False
        reps = store.db.execute("""SELECT * FROM files WHERE group_key=? AND source<>? AND
            ((state='selected' AND source<?) OR state='moved')""", (row["group_key"], row["source"], row["source"]))
        for other in reps:
            reference = Path(other["destination"] if other["state"] == "moved" else other["source"])
            try:
                if other["state"] == "moved":
                    record = store.db.execute("SELECT digest FROM moves WHERE source=? AND destination=? AND phase='done' AND restored=0 ORDER BY id DESC LIMIT 1",
                                              (other["source"], str(reference))).fetchone()
                    if not record or file_hash(reference) != record["digest"]:
                        continue
                elif fingerprint(reference) != (other["size"], other["mtime"]):
                    continue
                metrics = json.loads(other["metrics"])
                thumb, digest = other["thumb"], other["pixel_digest"]
                if not thumb:
                    old = inspect_image(reference, self.config, True)
                    metrics, thumb, digest = old.metrics, old.thumbnail, old.pixel_digest
                    if old.image:
                        for view in old.image.info.get('people_views',[]):view.close()
                        old.image.close()
                    store.db.execute("UPDATE files SET thumb=?,pixel_digest=? WHERE source=?", (thumb, digest, other["source"]))
                if same_content(item, metrics, thumb, digest):
                    item.metrics["duplicate_of"] = str(reference)
                    return True
            except OSError:
                continue
        return False

    def _move(self, store, row, output, run_id):
        source = Path(row["source"])
        root = Path(row["root"])
        bucket = root.name + "_" + hashlib.sha256(str(root).encode()).hexdigest()[:8]
        target = output / bucket / row["relative"]
        if self.config.scheme!='creative' and not self.config.retain_tree:target=output/source.name
        if self.config.scheme=='creative':
            from .creative import output_path
            target=output_path(output,source,root,row['relative'],json.loads(row['metrics']),self.config)
        if row['reason']=='backup_duplicate':target=output/'重复副本备份'/source.name
        try:
            if getattr(self,'flat_dedup',None) is not None and row['reason']!='backup_duplicate':
                duplicate,digest=self.flat_dedup.duplicate(source,(row['size'],row['mtime']))
                if duplicate:
                    metrics=json.loads(row['metrics']);metrics.update(duplicate_of=duplicate,sha256=digest)
                    if self.config.backup_duplicates:
                        target=output/'重复副本备份'/source.name
                        store.decision(str(source),'selected','backup_duplicate',metrics,row['thumb'],row['pixel_digest'])
                    else:
                        store.decision(str(source),'rejected','exact_duplicate',metrics,row['thumb'],row['pixel_digest']);store.db.commit();return
            # Reject a source parent replaced by a junction after enumeration.
            if source.resolve() != source:
                raise OSError("源路径现在包含链接，未移动。")
            ensure_parents(target, output)
            target = execute_move(store, run_id, source, target, (row["size"], row["mtime"]), self.log)
            if getattr(self,'flat_dedup',None) is not None and row['reason']!='backup_duplicate' and target.parent!=output/'重复副本备份':self.flat_dedup.remember(target,digest)
            store.db.execute("UPDATE files SET state='moved',destination=? WHERE source=?", (str(target), str(source)))
            store.db.commit()
        except OSError as exc:
            pending = store.db.execute("SELECT 1 FROM moves WHERE source=? AND phase='review' LIMIT 1", (str(source),)).fetchone()
            state = "review" if pending else "error"
            metrics = json.loads(row["metrics"])
            metrics["move_error"] = str(exc)
            store.decision(str(source), state, state, metrics, row["thumb"], row["pixel_digest"])
            store.db.commit()
            self.log(f"移动未完成，现有文件保留：{source} — {exc}")

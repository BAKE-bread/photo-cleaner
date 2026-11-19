"""Never overwrite. A source is removed only as part of a verified transfer."""
from pathlib import Path
import errno
import hashlib
import os
import shutil
import stat
import uuid


def fingerprint(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or path.is_symlink():
        raise OSError("不是普通文件或是符号链接。")
    return st.st_size, st.st_mtime_ns


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_parents(path, boundary):
    boundary = boundary.resolve()
    path.absolute().relative_to(boundary)
    current = boundary
    if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
        raise OSError("输出根目录不能是符号链接或目录联接。")
    for part in path.absolute().relative_to(boundary).parts[:-1]:
        current /= part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise OSError(f"目标目录含链接，停止移动：{current}")
        current.mkdir(exist_ok=True)
        current.resolve().relative_to(boundary)


def available_destination(path):
    candidate, counter = path, 1
    while os.path.lexists(candidate):
        candidate = path.with_name(f"{path.stem}__{counter}{path.suffix}")
        counter += 1
    return candidate


def rename_no_replace(source, destination):
    if os.name == "nt":
        # Windows os.rename raises FileExistsError, unlike os.replace.
        os.rename(source, destination)
    else:
        # link creation is atomic and exclusive; never use POSIX rename, which
        # can silently overwrite a destination created by another process.
        os.link(source, destination, follow_symlinks=False)
        os.unlink(source)


def safe_move(source, destination, expected, digest, phase=lambda *_: None):
    source, destination = Path(source), Path(destination)
    if fingerprint(source) != expected:
        raise OSError("源文件发生变化，未移动。")
    phase("planned", "")  # Caller durably stores intent before touching files.
    try:
        rename_no_replace(source, destination)
        # A foreign edit can land between the preflight check and the rename.
        # Keep the destination and journal it for review; never silently certify
        # changed bytes as the scanned source or remove an additional copy.
        if fingerprint(destination)!=expected or file_hash(destination)!=digest:
            raise OSError('移动期间文件发生变化，目标现有文件保留，请核对迁移记录。')
        phase("done", "")
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise
    # Different filesystem: copy to an exclusive staging file, verify, publish,
    # then remove only the verified source entry. Failures retain source data.
    staging = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.partial")
    phase("copying", str(staging))
    with source.open("rb") as reader, staging.open("xb") as writer:
        for block in iter(lambda: reader.read(1024*1024), b""):
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    if fingerprint(source) != expected or file_hash(staging) != digest:
        raise OSError(f"复制校验失败，源文件及临时文件保留：{staging}")
    shutil.copystat(source, staging, follow_symlinks=False)
    rename_no_replace(staging, destination)
    phase("published", str(staging))
    if fingerprint(source) != expected or file_hash(source) != digest:
        raise OSError("复制期间源文件改变，源文件和目标文件均保留。")
    # This is the source-entry removal of a cut, never a rejected-file deletion.
    os.unlink(source)
    phase("done", str(staging))


def recover_moves(store, emit, run_id=None):
    """Complete bookkeeping only. Ambiguous moves never delete either copy."""
    scope=" AND (m.run_id=? OR EXISTS(SELECT 1 FROM moves original WHERE m.run_id='restore:'||original.id AND original.run_id=?))" if run_id is not None else ''
    scope_args=(run_id,run_id) if run_id is not None else ()
    # A crash may happen after the durable 'done' journal entry and before the
    # files-table update. Repair that bookkeeping, without touching file data.
    completed = store.db.execute("""SELECT m.* FROM moves m JOIN files f ON f.source=m.source
        WHERE m.phase='done' AND m.restored=0 AND m.run_id NOT LIKE 'restore:%'
        AND f.state<>'moved'"""+scope,scope_args).fetchall()
    for row in completed:
        source, dest = Path(row["source"]), Path(row["destination"])
        try:
            if not os.path.lexists(source) and dest.is_file() and file_hash(dest) == row["digest"]:
                store.db.execute("UPDATE files SET state='moved',reason='selected',destination=? WHERE source=?", (str(dest), str(source)))
        except OSError:
            pass
    store.db.commit()
    completed_restores = store.db.execute("""SELECT m.* FROM moves m
        WHERE m.phase='done' AND m.run_id LIKE 'restore:%'
        AND EXISTS(SELECT 1 FROM moves original WHERE original.id=CAST(substr(m.run_id,9) AS INTEGER) AND original.restored=0)"""+scope,scope_args).fetchall()
    for row in completed_restores:
        source, dest = Path(row["source"]), Path(row["destination"])
        try:
            if not os.path.lexists(source) and dest.is_file() and file_hash(dest) == row["digest"]:
                store.db.execute("UPDATE moves SET restored=1 WHERE id=?", (int(row["run_id"].split(":")[1]),))
                store.db.execute("UPDATE files SET state='queued',destination=NULL WHERE source=?", (str(dest),))
        except OSError:
            pass
    store.db.commit()
    for row in store.db.execute("SELECT m.* FROM moves m WHERE phase NOT IN ('done','failed','review')"+scope,scope_args).fetchall():
        source, dest = Path(row["source"]), Path(row["destination"])
        try:
            verified = dest.is_file() and not dest.is_symlink() and file_hash(dest) == row["digest"]
            if not os.path.lexists(source) and verified:
                phase, state, reason = "done", "moved", "selected"
            elif not os.path.lexists(dest) and not (row["staging"] and os.path.lexists(row["staging"])):
                phase, state, reason = "failed", "error", "error"
            else:
                phase, state, reason = "review", "review", "review"
                emit(f"上次中断的文件需要复查（所有现有副本均保留）：{source}")
        except OSError:
            phase, state, reason = "review", "review", "review"
        store.db.execute("UPDATE moves SET phase=? WHERE id=?", (phase, row["id"]))
        if row["run_id"].startswith("restore:"):
            if phase == "done":
                original_id = int(row["run_id"].split(":")[1])
                store.db.execute("UPDATE moves SET restored=1 WHERE id=?", (original_id,))
                store.db.execute("UPDATE files SET state='queued',destination=NULL WHERE source=?", (str(dest),))
        else:
            store.db.execute("UPDATE files SET state=?,reason=?,destination=? WHERE source=?",
                             (state, reason, str(dest), str(source)))
        store.db.commit()


def execute_move(store, run_id, source, target, expected, emit, allow_rename=True):
    digest = file_hash(source)
    if fingerprint(source) != expected:
        raise OSError("源文件发生变化，未移动。")
    if allow_rename:
        target = available_destination(target)
    elif os.path.lexists(target):
        raise FileExistsError("原路径已有文件，未恢复。")
    while True:
        cursor = store.db.execute("INSERT INTO moves(run_id,source,destination,digest,size,mtime,phase) VALUES(?,?,?,?,?,?,?)",
                                  (run_id, str(source), str(target), digest, *expected, "planned"))
        move_id = cursor.lastrowid
        store.db.commit()

        def phase(name, staging):
            if name=='planned' and not staging:return  # Intent already durable.
            store.db.execute("UPDATE moves SET phase=?,staging=? WHERE id=?", (name, staging, move_id))
            # The caller commits done together with its files-table update.
            # A crash first is recovered from the durable planned/published row.
            if name!='done':store.db.commit()

        try:
            safe_move(source, target, expected, digest, phase)
            return target
        except FileExistsError:
            # A concurrent foreign file won the destination. Keep it untouched.
            record = store.db.execute("SELECT staging FROM moves WHERE id=?", (move_id,)).fetchone()
            if record["staging"] and os.path.lexists(record["staging"]):
                store.db.execute("UPDATE moves SET phase='review',error=? WHERE id=?", ("目标冲突，临时文件保留", move_id))
                store.db.commit()
                raise OSError("目标文件冲突，源文件及临时文件保留。")
            store.db.execute("UPDATE moves SET phase='failed',error='destination collision' WHERE id=?", (move_id,))
            store.db.commit()
            if not allow_rename:
                raise
            target = available_destination(target)
        except Exception as exc:
            # Journal stays recoverable if an output/staging copy may exist.
            row = store.db.execute("SELECT staging FROM moves WHERE id=?", (move_id,)).fetchone()
            changed_output = os.path.lexists(target) or bool(row["staging"] and os.path.lexists(row["staging"]))
            store.db.execute("UPDATE moves SET phase=?,error=? WHERE id=?",
                             ("review" if changed_output else "failed", str(exc)[:1000], move_id))
            store.db.commit()
            raise


def restore_moves(store, output, emit, stop, run_id=None):
    recover_moves(store, emit,run_id)
    query = "SELECT * FROM moves WHERE phase='done' AND restored=0 AND run_id NOT LIKE 'restore:%'"
    args = ()
    if run_id:
        query += " AND run_id=?"
        args = (run_id,)
    count, errors = 0, 0
    # Iterate using an ID cursor, so even a huge history stays bounded in RAM.
    last_id = store.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM moves").fetchone()[0]
    while not stop.is_set():
        row = store.db.execute(query + " AND id<? ORDER BY id DESC LIMIT 1", (*args, last_id)).fetchone()
        if not row:
            break
        last_id = row["id"]
        try:
            source, original = Path(row["destination"]), Path(row["source"])
            if output is not None:
                source.resolve().relative_to(output.resolve())
            if os.path.lexists(original):
                raise OSError("原路径已有文件，保留目标副本，未覆盖。")
            if fingerprint(source)[0] != row["size"] or file_hash(source) != row["digest"]:
                raise OSError("目标文件已改变或缺失，未恢复。")
            # Reject changed original directory links before writing outside.
            for parent in original.parents:
                if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                    raise OSError("原目录现在含链接，未恢复。")
            original.parent.mkdir(parents=True, exist_ok=True)
            dest = execute_move(store, f"restore:{row['id']}", source, original, fingerprint(source), emit, allow_rename=False)
            if dest != original:
                raise OSError("恢复时原路径出现冲突，副本已安全保留在带编号的路径。")
            store.db.execute("UPDATE moves SET restored=1 WHERE id=?", (row["id"],))
            store.db.execute("UPDATE files SET state='queued',destination=NULL WHERE source=?", (str(original),))
            store.db.commit()
            count += 1
            emit(f"已恢复：{original}")
        except (OSError, ValueError) as exc:
            errors += 1
            emit(f"恢复跳过：{row['source']} — {exc}")
    return {"restored": count, "errors": errors, "stopped": stop.is_set()}

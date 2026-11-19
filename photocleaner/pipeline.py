"""One bounded look-ahead batch; SQLite and file moves stay on their owner thread."""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import time


class DecodeWindow:
    def __init__(self,workers,stop,inspect,cached=None):
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='image-decode')
        self.stop=stop;self.inspect=inspect;self.batches=deque();self.current=[]
        self.cached=cached

    def submit(self,rows,duplicate_flags,config):
        # The caller permits at most current + next batch. No unbounded queue.
        if len(self.batches)>=2:raise RuntimeError('解码预取队列已满。')
        futures=[self.pool.submit(self._decode,row,config,duplicate) for row,duplicate in zip(rows,duplicate_flags)]
        self.batches.append((rows,futures))

    def _decode(self,row,config,duplicate):
        if self.stop.is_set() or row['state']!='queued':return None,0
        start=time.perf_counter()
        try:
            from pathlib import Path
            result=self.cached(row,duplicate) if self.cached else None
            if result is None:result=self.inspect(Path(row['source']),config,duplicate)
        except Exception as exc:
            from .vision import Inspection
            result=Inspection(reason='error',metrics={'error':str(exc)[:500]})
        return result,time.perf_counter()-start

    def take(self):
        for item in self.current:close_inspection(item)
        rows,futures=self.batches.popleft();items=[];cpu_seconds=0
        for future in futures:
            item,seconds=future.result();items.append(item);cpu_seconds+=seconds
            self.current=items  # Keep partial batches releasable on failure.
        self.current=items
        return rows,items,cpu_seconds

    def close(self):
        # A cancelled/paused prefetched image has no decision persisted and no
        # move scheduled. Release its raster and leave its SQLite row queued.
        for _,futures in self.batches:
            for future in futures:future.cancel()
        self.pool.shutdown(wait=True,cancel_futures=True)
        for _,futures in self.batches:
            for future in futures:
                if future.cancelled():continue
                item,_=future.result()
                close_inspection(item)
        self.batches.clear()
        for item in self.current:close_inspection(item)
        self.current=[]

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def close_inspection(item):
    if item is None or item.image is None:return
    for view in item.image.info.get('people_views',[]):view.close()
    item.image.close()

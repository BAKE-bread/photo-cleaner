"""Paged contact sheets with bounded, Pillow-only background decoding.

Workers own Pillow images; only the Tk thread constructs PhotoImage objects.
At most two jobs are submitted at once, including after rapid view changes.
"""
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageOps, ImageTk


def decode_file(path, size, expected=None):
    """Decode a bounded preview without importing any model dependencies."""
    from .moving import fingerprint
    path = Path(path)
    if path.resolve() != path:
        raise OSError('预览路径包含链接，请核对文件位置。')
    before = fingerprint(path)
    if expected is not None and tuple(expected) != before:
        raise OSError('文件已变化；重新扫描后可核对新图像。')
    with Image.open(path) as image:
        image.seek(0)
        # Camera JPEGs can be downsampled by their decoder before allocating
        # the full raster. Non-JPEGs keep the existing decompression safeguard.
        if image.format in ('JPEG', 'MPO'):
            image.draft('RGB', size)
        if image.width * image.height > 80_000_000:
            raise OSError('图像超出预览解码上限。')
        oriented = ImageOps.exif_transpose(image)
        try:
            oriented.thumbnail(size, Image.Resampling.LANCZOS)
            rgba = oriented.convert('RGBA')
            background = Image.new('RGB', rgba.size, 'white')
            background.paste(rgba, mask=rgba.getchannel('A'))
            rgba.close()
        finally:
            oriented.close()
    if fingerprint(path) != before:
        background.close()
        raise OSError('读取期间文件发生变化。')
    return background


class ThumbnailGrid:
    PAGE = 60

    def __init__(self, parent, on_select, on_open, scale=1):
        self.frame = ttk.Frame(parent)
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.frame, background='#f1f3f8', highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        from .ui_scroll import AutoScrollbar
        scroll = AutoScrollbar(self.frame, command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = ttk.Frame(self.canvas)
        self.item = self.canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.scale = scale
        self.card_width = round(208 * scale)
        self.image_size = (round(184 * scale), round(145 * scale))
        self.on_select, self.on_open = on_select, on_open
        self.cards = {}
        self.photos = {}
        self.paths = {}
        self.cache = OrderedDict()
        self.todo = deque()
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='result-thumb')
        self.inflight = 0
        self.generation = 0
        self.closed = False
        self.lifecycle = threading.Lock()
        self.columns = 0
        self.resize_timer = None
        self.canvas.bind('<Configure>', self.resize)
        self.body.bind('<Configure>', lambda _: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.frame.bind('<Destroy>', self.destroy, add='+')
        self.timer = self.frame.after(40, self.poll)

    def wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), 'units')
        return 'break'

    def resize(self, event=None):
        if self.resize_timer:
            self.frame.after_cancel(self.resize_timer)
        self.resize_timer = self.frame.after(50, self.reflow)

    def reflow(self):
        self.resize_timer = None
        if self.closed:
            return
        width = max(1, self.canvas.winfo_width())
        self.canvas.itemconfigure(self.item, width=width)
        columns = max(1, width // self.card_width)
        if columns == self.columns:
            return
        self.columns = columns
        for n, card in enumerate(self.cards.values()):
            card['frame'].grid(row=n // columns, column=n % columns, padx=5, pady=5, sticky='nw')

    def render(self, entries, selected=()):
        self.generation += 1
        generation = self.generation
        self.todo.clear()
        for card in self.cards.values():
            card['frame'].destroy()
        self.cards.clear()
        self.paths.clear()
        self.photos.clear()
        self.columns = 0
        for entry in entries:
            key = entry['key']
            frame = tk.Frame(self.body, background='white', highlightthickness=2, highlightbackground='#dce1ec')
            frame.configure(width=self.card_width - 10, height=round(240 * self.scale))
            frame.pack_propagate(False)
            image_box = tk.Label(frame, text='正在读取…', background='#f6f7fb', foreground='#64748b')
            image_box.pack(fill='both', expand=True, padx=5, pady=5)
            for text, color in [(entry['name'], '#26344b'), (entry['evidence'], '#6554ad'), (entry['reason'], '#64748b')]:
                label = tk.Label(frame, text=text, background='white', foreground=color, font='SmallFont', anchor='w', wraplength=self.card_width - 25)
                label.pack(fill='x', padx=7, pady=(0, 3))
            self.cards[key] = {'frame': frame, 'image': image_box}
            self.paths[key] = entry['path']
            for widget in [frame, *frame.winfo_children()]:
                widget.bind('<Button-1>', lambda event, k=key: self.on_select(k, event))
                widget.bind('<Double-1>', lambda event, k=key: self.on_open(k))
                widget.bind('<MouseWheel>', self.wheel)
            cache_key = (str(entry['path']), entry.get('expected'), self.image_size)
            if cache_key in self.cache:
                self.cache.move_to_end(cache_key)
                self.show_image(key, self.cache[cache_key])
            elif entry['path']:
                self.todo.append((generation, key, cache_key, entry['path'], entry.get('expected')))
            else:
                image_box.configure(text='当前文件路径不可预览')
        self.reflow()
        self.canvas.yview_moveto(0)
        self.highlight(selected)
        self.submit()

    def submit(self):
        while not self.closed and self.inflight < 2 and self.todo:
            generation, key, cache_key, path, expected = self.todo.popleft()
            self.inflight += 1
            def work(g=generation, k=key, ck=cache_key, p=path, fp=expected):
                try:
                    image = decode_file(p, self.image_size, fp)
                    with self.lifecycle:
                        if self.closed:
                            image.close()
                        else:
                            self.events.put((g, k, ck, image, None))
                except Exception as exc:
                    with self.lifecycle:
                        if not self.closed:
                            self.events.put((g, k, ck, None, str(exc)))
            self.pool.submit(work)

    def show_image(self, key, image):
        if key not in self.cards:
            return
        photo = ImageTk.PhotoImage(image, master=self.frame)
        self.photos[key] = photo
        self.cards[key]['image'].configure(image=photo, text='')

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                generation, key, cache_key, image, error = self.events.get_nowait()
                self.inflight -= 1
                if generation != self.generation:
                    if image:
                        image.close()
                    continue
                if image:
                    previous = self.cache.pop(cache_key, None)
                    if previous:
                        previous.close()
                    self.cache[cache_key] = image
                    self.show_image(key, image)
                    while len(self.cache) > 120:
                        _, expired = self.cache.popitem(last=False)
                        expired.close()
                elif key in self.cards:
                    self.cards[key]['image'].configure(text='预览暂不可用\n' + (error or '')[:65])
        except queue.Empty:
            pass
        self.submit()
        self.timer = self.frame.after(40, self.poll)

    def highlight(self, keys):
        keys = set(keys)
        for key, card in self.cards.items():
            card['frame'].configure(highlightbackground='#7960c8' if key in keys else '#dce1ec')

    def destroy(self, event=None):
        if event is not None and event.widget is not self.frame:
            return
        if self.closed:
            return
        with self.lifecycle:
            self.closed = True
        for timer in (self.timer, self.resize_timer):
            if timer:
                self.frame.after_cancel(timer)
        self.todo.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for image in self.cache.values():
            image.close()
        self.cache.clear()
        try:
            while True:
                *_, image, error = self.events.get_nowait()
                if image:
                    image.close()
        except queue.Empty:
            pass


def open_preview(parent, path, expected=None):
    """Large previews also decode away from Tk; closing is always immediate."""
    win = tk.Toplevel(parent)
    win.title(Path(path).name if path else '图像预览')
    win.geometry(f'{min(1100,round(win.winfo_screenwidth()*.85))}x{min(850,round(win.winfo_screenheight()*.8))}')
    label = ttk.Label(win, text='正在读取图像…', anchor='center')
    label.pack(fill='both', expand=True)
    events = queue.Queue()
    lifecycle = threading.Lock()
    closed = [False]
    timer = [None]
    def destroy(event):
        if event.widget is win:
            with lifecycle:
                closed[0] = True
                try:
                    while True:
                        image,_=events.get_nowait()
                        if image:image.close()
                except queue.Empty:
                    pass
            if timer[0]:
                win.after_cancel(timer[0])
    win.bind('<Destroy>', destroy, add='+')
    def work():
        try:
            image = decode_file(path, (1100, 850), expected)
            with lifecycle:
                if closed[0]:
                    image.close()
                else:
                    events.put((image, None))
        except Exception as exc:
            with lifecycle:
                if not closed[0]:
                    events.put((None, str(exc)))
    from threading import Thread
    Thread(target=work, daemon=True, name='result-preview').start()
    def poll():
        try:
            image, error = events.get_nowait()
        except queue.Empty:
            timer[0] = win.after(50, poll)
            return
        timer[0] = None
        if image:
            photo = ImageTk.PhotoImage(image, master=win)
            image.close()
            label.configure(image=photo, text='')
            label.image = photo
        else:
            label.configure(text='当前文件无法预览：' + (error or ''))
    timer[0] = win.after(50, poll)
    return win

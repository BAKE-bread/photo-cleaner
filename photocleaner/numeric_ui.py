"""Keep editable numeric values separate from their rounded presentation."""
import tkinter as tk
import weakref
from .numbers import number,input_float,input_int

class NumericVar(tk.StringVar):
    def __init__(self,master,value,integer=False):
        super().__init__(master,value='')
        self.cast=input_int if integer else input_float
        self._actual=None;self._internal=False
        reference=weakref.ref(self)
        def changed(*args):
            variable=reference()
            if variable is not None:variable._edited(*args)
        self.trace_add('write',changed);self.present(value)
    def _edited(self,*_):
        if not self._internal:self._actual=None
    def input_value(self):
        return self._actual if self._actual is not None else self.cast(self.get())
    def _show(self,text):
        self._internal=True
        try:self.set(text)
        finally:self._internal=False
    def present(self,value=None):
        self._actual=self.cast(self.input_value() if value is None else value)
        self._show(number(self._actual))
    def edit(self):
        try:
            self._actual=self.input_value();self._show(str(self._actual))
        except ValueError:pass

def bind_numeric(widget,variable):
    widget.bind('<FocusIn>',lambda _:variable.edit(),add='+')
    def finish(_):
        try:variable.present()
        except ValueError:pass
    widget.bind('<FocusOut>',finish,add='+')

"""Safe, small expression grammar for on-disk image bytes (decimal units).

Directions follow the application's inclusive switch. AND binds before OR;
parentheses are supported. Parsing never evaluates Python or user code.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from functools import lru_cache
import re
from .numbers import number

DEFAULT_RULES = '<400 KB OR >4 MB'
UNITS = {'B': 1, 'KB': 1_000, 'MB': 1_000_000, 'GB': 1_000_000_000}
TOKEN = re.compile(r'\s*(<=|>=|[<>≤≥()]|AND\b|OR\b|(?:\d+(?:\.\d*)?|\.\d+)\s*(?:GB|MB|KB|B)\b)', re.I)
VALUE = re.compile(r'((?:\d+(?:\.\d*)?|\.\d+))\s*(GB|MB|KB|B)', re.I)


@dataclass(frozen=True)
class SizeRules:
    # Closed, disjoint intervals over nonnegative integer byte counts.
    intervals: tuple
    description: str

    def matches(self, byte_count):
        return any(byte_count >= lo and (hi is None or byte_count <= hi)
                   for lo, hi in self.intervals)


def _union(intervals):
    merged = []
    for lo, hi in sorted(intervals, key=lambda p:p[0]):
        if hi is not None and hi < lo:continue
        if merged and (merged[-1][1] is None or lo <= merged[-1][1] + 1):
            previous = merged[-1]
            merged[-1] = (previous[0], None if hi is None or previous[1] is None else max(previous[1], hi))
        else:merged.append((lo, hi))
    return tuple(merged)


def _intersection(left, right):
    result = []
    for alo, ahi in left:
        for blo, bhi in right:
            lo = max(alo, blo)
            hi = bhi if ahi is None else ahi if bhi is None else min(ahi, bhi)
            if hi is None or hi >= lo:result.append((lo, hi))
    return _union(result)


@lru_cache(maxsize=128)
def parse_size_rules(expression=DEFAULT_RULES, inclusive=True):
    """Return a validated predicate; each user number rounds to 2 decimals.

    Examples: '<400 KB OR >4 MB', '>1 MB AND <4 MB'. Semicolon, newline,
    Chinese comma/slash mean OR, Chinese '且/并且' means AND. Explicit equals
    symbols are accepted as familiar input; the global switch decides equality.
    """
    if not isinstance(expression,str) or not expression.strip():
        raise ValueError('请输入文件体积条件，例如 <400 KB OR >4 MB。')
    if len(expression)>2000:raise ValueError('文件体积条件过长，请精简到 2000 字以内。')
    text=expression.strip().upper().replace('（','(').replace('）',')')
    text=text.replace('并且',' AND ').replace('或者',' OR ').replace('且',' AND ').replace('或',' OR ')
    text=re.sub(r'[;；\n\r、，,/]+',' OR ',text)
    tokens=[];position=0
    while position<len(text):
        if not text[position:].strip():break
        match=TOKEN.match(text,position)
        if not match:
            raise ValueError('文件体积条件无法识别：'+text[position:position+24]+'。请使用 < 或 >、B/KB/MB/GB 和 AND/OR。')
        tokens.append(match.group(1));position=match.end()
    if len(tokens)>256:raise ValueError('文件体积条件过多，请减少比较条件。')
    index=0
    def term(depth):
        nonlocal index
        if depth>16:raise ValueError('文件体积条件括号层数过多。')
        if index<len(tokens) and tokens[index]=='(':
            index+=1;value=expression_or(depth+1)
            if index>=len(tokens) or tokens[index]!=')':raise ValueError('文件体积条件缺少右括号。')
            index+=1;return value
        if index>=len(tokens) or tokens[index] not in ('<','>','<=','>=','≤','≥'):
            raise ValueError('每个文件体积条件须以 < 或 > 开头，例如 >1 MB AND <4 MB。')
        direction=tokens[index][0];index+=1
        if direction=='≤':direction='<'
        if direction=='≥':direction='>'
        if index>=len(tokens) or not VALUE.fullmatch(tokens[index]):
            raise ValueError('比较符后须填写数值和单位，例如 400 KB；不支持 KiB/MiB。')
        match=VALUE.fullmatch(tokens[index]);index+=1
        amount=Decimal(match.group(1))
        if amount.adjusted()>18:raise ValueError('文件体积阈值过大。')
        threshold=amount.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)*UNITS[match.group(2).upper()]
        if threshold<=0:raise ValueError('文件体积阈值须大于 0，数值按小数点后 2 位使用。')
        if direction=='<':
            hi=int(threshold.to_integral_value(rounding=ROUND_FLOOR)) if inclusive else int(threshold.to_integral_value(rounding=ROUND_CEILING))-1
            interval=(0,hi);symbol='≤' if inclusive else '<'
        else:
            lo=int(threshold.to_integral_value(rounding=ROUND_CEILING)) if inclusive else int(threshold.to_integral_value(rounding=ROUND_FLOOR))+1
            interval=(lo,None);symbol='≥' if inclusive else '>'
        numeric=number(amount.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
        return (interval,),f'{symbol}{numeric} {match.group(2).upper()}'
    def expression_and(depth):
        nonlocal index
        value,label=term(depth)
        while index<len(tokens) and tokens[index]=='AND':
            index+=1;other,otherlabel=term(depth)
            value=_intersection(value,other);label=f'{label} 且 {otherlabel}'
        return value,label
    def expression_or(depth):
        nonlocal index
        value,label=expression_and(depth)
        while index<len(tokens) and tokens[index]=='OR':
            index+=1;other,otherlabel=expression_and(depth)
            value=_union((*value,*other));label=f'{label} 或 {otherlabel}'
        return value,label
    intervals,label=expression_or(0)
    if index!=len(tokens):raise ValueError('比较条件之间请使用 AND/OR；括号须成对。')
    if not intervals:raise ValueError('文件体积条件互相矛盾，结果必为空；请调整范围或改用 OR（或）。')
    # A plain normalized interval description also preserves grouping unambiguously.
    def interval_text(lo,hi):
        if lo==0 and hi is None:return '全部文件体积'
        if lo==0:return f'0–{number(hi)} B'
        if hi is None:return f'{number(lo)} B 及以上'
        return f'{number(lo)}–{number(hi)} B'
    if '(' in tokens:label=' 或 '.join(interval_text(lo,hi) for lo,hi in intervals)
    return SizeRules(intervals,label)

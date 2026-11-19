"""Display precision is separate from input precision and stored evidence."""
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP,localcontext
import numbers

def number(value,group=False):
    if value is None:return '—'
    if isinstance(value,bool):return '是' if value else '否'
    try:d=Decimal(str(value))
    except (InvalidOperation,ValueError):return str(value)
    if not d.is_finite():return '—'
    if not d:return '0'
    integral=d==d.to_integral_value()
    places=min(6,3-d.copy_abs().adjusted())
    with localcontext() as context:
        context.prec=max(40,len(d.as_tuple().digits)+abs(places)+8)
        rounded=d.quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)
        if rounded:
            new_places=min(6,3-rounded.copy_abs().adjusted())
            if new_places<places:
                places=new_places;rounded=d.quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)
        if not rounded:rounded=abs(rounded)
        if integral:return format(rounded,',.0f' if group else '.0f')
        return format(rounded,(',' if group else '')+'.'+str(max(1,places))+'f')

def input_float(value):
    """User-entered real numbers use two decimal places, never raw model data."""
    try:
        d=Decimal(str(value).strip())
        if not d.is_finite():raise ValueError('请输入有限数值。')
        with localcontext() as context:
            context.prec=max(40,len(d.as_tuple().digits)+abs(d.adjusted())+8)
            return float(d.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    except (InvalidOperation,OverflowError) as exc:raise ValueError('请输入有效数值。') from exc

def input_int(value):
    try:
        d=Decimal(str(value).strip())
        if not d.is_finite() or d!=d.to_integral_value():raise ValueError('本项只能输入整数。')
        return int(d)
    except InvalidOperation as exc:raise ValueError('本项只能输入整数。') from exc

INTEGER_METRICS={'width','height','frames','frame_count','jpg_quality','output_bytes','nearby_anchors','nearby_exclusions'}
def metric_input(value,metric):return input_int(value) if metric in INTEGER_METRICS else input_float(value)

def display_json(value,indent=0):
    import json
    if value is None:return 'null'
    if isinstance(value,bool):return 'true' if value else 'false'
    if isinstance(value,numbers.Real):return number(value)
    if isinstance(value,dict):
        return '{\n'+',\n'.join(' '*(indent+2)+json.dumps(str(k),ensure_ascii=False)+': '+display_json(v,indent+2) for k,v in value.items())+'\n'+' '*indent+'}'
    if isinstance(value,(list,tuple)):return '['+', '.join(display_json(v,indent) for v in value)+']'
    return json.dumps(value,ensure_ascii=False,default=str)

"""User-facing threshold directions; model internals keep their fixed calibration."""
def lower(value,limit,config):
    return value>=limit if getattr(config,'inclusive',True) else value>limit

def upper(value,limit,config):
    return value<=limit if getattr(config,'inclusive',True) else value<limit

def symbols(config):
    return ('≥','≤','<','>') if getattr(config,'inclusive',True) else ('>','<','≤','≥')

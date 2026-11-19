"""Naming shapes, not claims about the actual download platform."""
import re

def naming_method(name):
    stem = name.rsplit('.',1)[0] if '.' in name else name
    # Collision suffixes created by this program keep the naming family.
    stem=re.sub(r'__\d+$','',stem)
    # Thumbnail variants and resolution/frame suffixes do not create sources.
    stem=re.sub(r'(?i)_tmb$','',stem)
    hashed=re.fullmatch(r'([0-9a-fA-F]{32})(?:_\d+)?',stem)
    if hashed:
        return '32位大写哈希' if hashed[1].isupper() else '32位小写哈希'
    # QQ-like punctuation-rich tokens, with or without _tmb, are one family.
    if len(stem)>=16 and re.search(r'[$`~{}%@^()!]',stem) and re.fullmatch(r'[\x21-\x7e]+',stem):
        return '符号混合随机名'
    patterns = [
        (r'[0-9A-F]{32}', '32位大写哈希'),
        (r'[0-9a-f]{32}', '32位小写哈希'),
        (r'[0-9a-f]{40}', '40位小写哈希'),
        (r'[0-9A-F]{40}', '40位大写哈希'),
        (r'Cache_-?[0-9a-fA-F]{16}', 'Cache_16位哈希'),
        (r's\d+_s\d+_\d+_\d+', 's数字_s数字_数字_数字'),
        (r'v2-[0-9a-fA-F]{32}(?:_[A-Za-z]+)?', 'v2_哈希'),
        (r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', 'UUID'),
        (r'(?i)mmexport\d+', 'mmexport数字'),
        (r'(?i)(?:IMG|PXL|Screenshot)[_-]?\d{8}.*', '照片或截图日期'),
        (r'(?i)(?:DSC|DSCN|DSCF)[_-]?\d+.*', 'DSC相机编号'),
        (r'\d{13}', '13位时间戳式数字'),
        (r'\d{10}', '10位时间戳式数字'),
    ]
    for pattern,label in patterns:
        if re.fullmatch(pattern,stem):
            return label
    if re.fullmatch(r'\d+',stem):
        return '纯数字编号'
    if re.fullmatch(r'[A-Za-z0-9_-]{15,32}',stem) and re.search('[a-z]',stem) and re.search('[A-Z]',stem):
        return '混合大小写短标识'
    # Stable textual prefixes with changing numeric suffixes belong together.
    match = re.fullmatch(r'([A-Za-z]{2,16})[-_ ]?\d+(?:[-_]\d+)*',stem)
    if match:
        return match[1]+'_数字编号'
    if re.fullmatch(r'[A-Za-z0-9]{16,}',stem):
        return '长字母数字名'
    if re.search(r'[\u4e00-\u9fff]',stem):
        return '中文文字命名'
    return '其他文字或混合命名'

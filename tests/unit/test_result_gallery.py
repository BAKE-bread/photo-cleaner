import pytest
from PIL import Image

from photocleaner.moving import fingerprint
from photocleaner.result_gallery import decode_file


def test_thumbnail_orientation_alpha_and_bounds(tmp_path):
    path = tmp_path / 'portrait.JPG'
    image = Image.new('RGB', (600,300), 'blue')
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)
    image.close()
    before = path.read_bytes()
    thumb = decode_file(path, (184,145), fingerprint(path))
    assert thumb.width <= 184 and thumb.height <= 145
    assert thumb.height > thumb.width
    thumb.close()
    assert path.read_bytes() == before
    transparent = tmp_path / 'alpha.png'
    Image.new('RGBA',(50,50),(0,0,0,0)).save(transparent)
    thumb = decode_file(transparent,(100,100))
    assert thumb.mode == 'RGB' and thumb.getpixel((0,0)) == (255,255,255)
    thumb.close()


def test_changed_or_non_image_files_get_safe_errors(tmp_path):
    path = tmp_path / 'a.png'
    Image.new('RGB',(40,40)).save(path)
    with pytest.raises(OSError,match='变化'):
        decode_file(path,(100,100),(1,2))
    path = tmp_path / 'ordinary.txt'
    path.write_text('ordinary text',encoding='utf8')
    with pytest.raises(OSError):
        decode_file(path,(100,100))

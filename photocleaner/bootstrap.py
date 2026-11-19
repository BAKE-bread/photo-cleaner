"""Shared entry point for the launcher, python -m and installed console script."""
import sys


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    from .paths import prepare_editable_resources
    prepare_editable_resources()
    from .cli import main as cli_main
    args = sys.argv[1:] if argv is None else argv
    return cli_main(args or ['gui'])

"""Expensive media checks used at the Living Review approval boundary."""
from io import BytesIO
from pathlib import Path
import json
import subprocess
import warnings


RASTER_FORMATS = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'webp': 'WEBP'}


def validate_raster(data, extension, section_id):
    """Decode a supported raster and ensure its content matches its filename."""
    try:
        from PIL import Image, ImageSequence, UnidentifiedImageError
    except ImportError as exc:
        raise ValueError(f'{section_id}: install Pillow from the skill requirements.txt to check images') from exc

    expected = RASTER_FORMATS[extension]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != expected:
                    raise ValueError(f'{section_id}: expected {expected} image data, found {image.format}')
                image.verify()
            with Image.open(BytesIO(data)) as image:
                for frame in ImageSequence.Iterator(image):
                    frame.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(f'{section_id}:'):
            raise
        raise ValueError(f'{section_id}: invalid {expected} image: {exc}') from exc


def mermaid_diagrams(markdown):
    """Extract Mermaid code using the same Markdown tokenizer as the viewer."""
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(['node', str(root / 'scripts/extract_review_mermaid.mjs')],
                          input=markdown, text=True, capture_output=True, timeout=15)
    if proc.returncode:
        raise ValueError(f'Could not inspect Markdown Mermaid blocks: {proc.stderr.strip()}')
    return json.loads(proc.stdout)


def validate_svg(data, section_id):
    """Parse an SVG document and verify that its root element is SVG."""
    from xml.etree import ElementTree

    try:
        if ElementTree.fromstring(data).tag.split('}')[-1] != 'svg':
            raise ValueError(f'{section_id}: expected an SVG document')
    except ElementTree.ParseError as exc:
        raise ValueError(f'{section_id}: invalid SVG: {exc}') from exc

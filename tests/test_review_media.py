"""Approval checkpoint validation for raster and Mermaid review media."""
import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from scripts.living_review import CATALOG, checkpoint


class ReviewMediaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manifest = {'slice_review': {'selected': {}}}
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def catalog(self, section):
        path = self.root / CATALOG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'sections': [section]}))

    def test_rejects_corrupt_raster_section_source(self):
        (self.root / 'preview.png').write_bytes(b'not an image')
        self.catalog({'id': 'preview', 'source': 'preview.png'})
        with self.assertRaisesRegex(ValueError, 'preview.*invalid PNG'):
            checkpoint(self.root, self.manifest)

    def test_accepts_fully_decodable_raster_source(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new('RGB', (2, 2), (80, 120, 160)).save(buffer, format='PNG')
        (self.root / 'preview.png').write_bytes(buffer.getvalue())
        self.catalog({'id': 'preview', 'source': 'preview.png'})
        self.assertIn('preview', checkpoint(self.root, self.manifest))

    def test_rejects_raster_missing_its_end_marker(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new('RGB', (2, 2), (80, 120, 160)).save(buffer, format='PNG')
        (self.root / 'preview.png').write_bytes(buffer.getvalue()[:-12])
        self.catalog({'id': 'preview', 'source': 'preview.png'})
        with self.assertRaisesRegex(ValueError, 'preview: invalid PNG'):
            checkpoint(self.root, self.manifest)

    def test_rejects_corrupt_raster_reference(self):
        (self.root / 'notes.md').write_text('# Notes\n')
        (self.root / 'reference.webp').write_bytes(b'RIFFbrokenWEBP')
        self.catalog({'id': 'notes', 'source': 'notes.md', 'reference': 'reference.webp'})
        with self.assertRaisesRegex(ValueError, 'notes.*invalid WEBP'):
            checkpoint(self.root, self.manifest)

    def test_parses_mermaid_in_tilde_fence_before_approval(self):
        (self.root / 'diagram.md').write_text('~~~mermaid\nnot a diagram\n~~~\n')
        self.catalog({'id': 'diagram', 'source': 'diagram.md'})
        with self.assertRaises(ValueError) as caught:
            checkpoint(self.root, self.manifest)
        self.assertIn('diagram:', str(caught.exception))
        self.assertIn('No diagram type detected', str(caught.exception))

    def test_parses_mermaid_in_long_tilde_fence(self):
        (self.root / 'diagram.md').write_text('~~~~ mermaid\nnot a diagram\n~~~~\n')
        self.catalog({'id': 'diagram', 'source': 'diagram.md'})
        with self.assertRaises(ValueError) as caught:
            checkpoint(self.root, self.manifest)
        self.assertIn('diagram:', str(caught.exception))
        self.assertIn('No diagram type detected', str(caught.exception))

    def test_parses_mermaid_nested_in_a_blockquote(self):
        (self.root / 'diagram.md').write_text('> ~~~mermaid\n> not a diagram\n> ~~~\n')
        self.catalog({'id': 'diagram', 'source': 'diagram.md'})
        with self.assertRaises(ValueError) as caught:
            checkpoint(self.root, self.manifest)
        self.assertIn('diagram:', str(caught.exception))
        self.assertIn('No diagram type detected', str(caught.exception))

    def test_parses_mermaid_nested_in_a_list(self):
        (self.root / 'diagram.md').write_text('- Diagram\n  ~~~mermaid\n  not a diagram\n  ~~~\n')
        self.catalog({'id': 'diagram', 'source': 'diagram.md'})
        with self.assertRaises(ValueError) as caught:
            checkpoint(self.root, self.manifest)
        self.assertIn('diagram:', str(caught.exception))
        self.assertIn('No diagram type detected', str(caught.exception))

    def test_parses_mermaid_with_marked_language_title(self):
        (self.root / 'diagram.md').write_text('```mermaid optional-title\nnot a diagram\n```\n')
        self.catalog({'id': 'diagram', 'source': 'diagram.md'})
        with self.assertRaises(ValueError) as caught:
            checkpoint(self.root, self.manifest)
        self.assertIn('No diagram type detected', str(caught.exception))

    def test_parses_raw_html_mermaid_code_block(self):
        (self.root / 'diagram.md').write_text('<pre><code class="language-mermaid">not a diagram</code></pre>\n')
        self.catalog({'id': 'diagram', 'source': 'diagram.md'})
        with self.assertRaises(ValueError) as caught:
            checkpoint(self.root, self.manifest)
        self.assertIn('No diagram type detected', str(caught.exception))

    def test_rejects_corrupt_svg_reference(self):
        (self.root / 'notes.md').write_text('# Notes\n')
        (self.root / 'reference.svg').write_text('<svg><circle></svg>')
        self.catalog({'id': 'notes', 'source': 'notes.md', 'reference': 'reference.svg'})
        with self.assertRaisesRegex(ValueError, 'notes: invalid SVG'):
            checkpoint(self.root, self.manifest)

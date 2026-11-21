"""Capture actual component and page pixels from the same browser state."""

from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

from PIL import Image, ImageChops

from fixtures.blueprint_browser_probe import ItemsHandler


class VisualCaptureTests(unittest.TestCase):
    def run_driver(self, destination, payload):
        driver = Path(__file__).resolve().parents[1] / 'scripts/blueprint_browser.mjs'
        return subprocess.run(
            ['node', driver, '--visual-evidence', destination],
            input=json.dumps(payload), text=True, capture_output=True, timeout=30,
            env={**os.environ, 'P2P_CAPTURE_PYTHON': sys.executable},
        )

    def capture_page(self, page, *, assertions=()):
        browser = shutil.which('google-chrome') or shutil.which('chromium')
        if not browser:
            self.skipTest('Chromium is unavailable')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'index.html').write_text(page)
        with ThreadingHTTPServer(('127.0.0.1', 0), partial(ItemsHandler, directory=temp.name)) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            payload = {
                'browser': browser, 'appUrl': f'http://127.0.0.1:{server.server_port}',
                'interaction_assertions': list(assertions),
                'renderCase': {'route': '/', 'viewport': {'width': 800, 'height': 600},
                               'ready_selector': '#ready'},
            }
            try:
                result = self.run_driver(root / 'attempt', payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            finally:
                server.shutdown()

    def test_visible_assertions_detect_opaque_overlays(self):
        metadata = self.capture_page(
            '<style>body{margin:0}#ready{position:relative;width:200px;height:140px;background:white}'
            '#clear,#covered{background:red;width:100px;height:40px}'
            '#clear{opacity:.5}#overlay{position:absolute;top:40px;left:0;width:100px;height:40px;'
            'background:#123;pointer-events:none}</style>'
            '<main id="ready"><div id="clear">Visible</div><div id="covered">Covered</div>'
            '<div id="overlay"></div></main>',
            assertions=[
                {'selector': '#ready', 'probe': 'visible', 'expected': True},
                {'selector': '#clear', 'probe': 'visible', 'expected': True},
                {'selector': '#covered', 'probe': 'visible', 'expected': False},
                {'selector': '#covered', 'probe': 'visible', 'expected': True},
            ])
        self.assertEqual([(item['actual'], item['result']) for item in metadata['interactionAssertions']],
                         [(True, 'pass'), (True, 'pass'), (False, 'pass'), (False, 'fail')])

    def test_closeup_matches_full_capture_during_animation(self):
        metadata = self.capture_page(
            '<style>body{height:1100px;margin:0}#ready{width:200px;height:120px;'
            'animation:color 620ms linear infinite alternate}'
            '@keyframes color{from{background:#000}to{background:#fff}}</style>'
            '<main id="ready">Animated panel</main>')
        with Image.open(metadata['full']) as full, Image.open(metadata['detail']) as detail:
            self.assertEqual(full.size, (800, 1100))
            self.assertEqual(detail.size, (200, 120))
            self.assertIsNone(ImageChops.difference(detail, full.crop((0, 0, 200, 120))).getbbox())

    def test_open_component_capture_preserves_pixels_and_original_attempt(self):
        browser = shutil.which('google-chrome') or shutil.which('chromium')
        if not browser:
            self.skipTest('Chromium is unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'index.html').write_text(
                '<style>body{height:1100px;margin:0}details{width:200px;background:#e1e1e1}'
                'summary{height:40px}section{height:60px}</style>'
                '<details><summary>Open menu</summary><section>Choices</section></details>')
            with ThreadingHTTPServer(('127.0.0.1', 0), partial(ItemsHandler, directory=temp)) as server:
                Thread(target=server.serve_forever, daemon=True).start()
                payload = {
                    'browser': browser, 'appUrl': f'http://127.0.0.1:{server.server_port}',
                    'checks': [{'selector': 'details', 'property': 'width'},
                               {'selector': '#missing', 'property': 'width'}],
                    'interaction_assertions': [
                        {'selector': 'summary', 'probe': 'focused', 'expected': True},
                        {'selector': 'section', 'probe': 'visible', 'expected': True},
                        {'selector': 'section', 'probe': 'text', 'expected': 'Different choices'},
                    ],
                    'renderCase': {'route': '/', 'viewport': {'width': 800, 'height': 600},
                                   'setup': [{'action': 'press', 'key': 'Tab'}, {'action': 'press', 'key': 'Enter'}],
                                   'ready_selector': 'details[open]'},
                }
                try:
                    result = self.run_driver(root / 'attempt', payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    metadata = json.loads(result.stdout)
                    self.assertEqual([(item['actual'], item['result']) for item in metadata['interactionAssertions']],
                                     [(True, 'pass'), (True, 'pass'), ('Choices', 'fail')])
                    self.assertEqual(metadata['actual'], ['200px', None])
                    with Image.open(metadata['full']) as full, Image.open(metadata['detail']) as detail:
                        self.assertEqual(full.size, (800, 1100))
                        self.assertEqual(detail.size, (200, 100))
                        self.assertEqual(detail.tobytes(), full.crop((0, 0, 200, 100)).tobytes())
                    original = Path(metadata['full']).read_bytes()
                    repeated = self.run_driver(root / 'attempt', payload)
                    self.assertNotEqual(repeated.returncode, 0)
                    self.assertEqual(original, Path(metadata['full']).read_bytes())
                finally:
                    server.shutdown()

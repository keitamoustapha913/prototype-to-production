"""Check that the workflow test cache preserves the Mermaid process contract."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


TESTS = Path(__file__).resolve().parent
PARSER = TESTS.parent / 'scripts' / 'parse_mermaid.mjs'
SHIM_BIN = TESTS / 'fixtures' / 'mermaid-bin'


@unittest.skipUnless(os.name == 'posix', 'the test cache uses a POSIX executable shim')
class MermaidCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        self.node = shutil.which('node')
        self.assertIsNotNone(self.node, 'the Mermaid tests require Node.js')

    def parse(self, source, *, node=None):
        env = {
            **os.environ,
            'PATH': os.pathsep.join((str(SHIM_BIN), os.environ['PATH'])),
            'P2P_TEST_REAL_NODE': node or self.node,
            'P2P_TEST_MERMAID_CACHE': str(self.cache),
        }
        return subprocess.run(['node', str(PARSER)], input=json.dumps([source]), text=True,
                              capture_output=True, env=env, timeout=30)

    def test_reuses_identical_input_and_parses_changed_input(self):
        flowchart = 'flowchart TD\nA --> B'
        first = self.parse(flowchart)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)[0]['type'], 'flowchart-v2')

        replay = self.parse(flowchart, node=str(self.cache / 'unavailable-node'))
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(replay.stdout, first.stdout)

        changed = self.parse('stateDiagram-v2\nIdle --> Ready')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(json.loads(changed.stdout)[0]['state_edges'], [['Idle', 'Ready', '']])
        self.assertEqual(len(list(self.cache.iterdir())), 2)

    def test_preserves_parser_errors_without_caching_them(self):
        invalid = self.parse('this is not a Mermaid diagram')
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn('No diagram type detected', invalid.stderr)
        self.assertEqual(list(self.cache.iterdir()), [])

"""Minimal Codex session records for CLI confirmation tests."""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


class ConfirmationSession:
    def __init__(self, root, home, env):
        self.thread = str(uuid4())
        home = Path(home)
        # Keep the stand-in session store outside the application snapshot.
        self.path = home / 'sessions' / f'rollout-fixture-{self.thread}.jsonl'
        self.path.parent.mkdir(parents=True)
        env.update(CODEX_HOME=str(home), CODEX_THREAD_ID=self.thread)
        self.append('session_meta', {'id': self.thread, 'cwd': str(root)})

    def append(self, kind, payload):
        event = {'type': kind, 'payload': payload, 'timestamp': datetime.now(timezone.utc).isoformat()}
        with self.path.open('a') as stream:
            stream.write(json.dumps(event) + '\n')

    def prompt(self, text, *, phase="final"):
        self.append('response_item', {'id': str(uuid4()), 'type': 'message', 'role': 'assistant',
                                      'phase': phase, 'content': [{'type': 'output_text', 'text': text}]})

    def reply(self, text, *, provenance='user'):
        self.append('response_item', {
            'id': str(uuid4()), 'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': text}],
            'internal_chat_message_metadata_passthrough': {
                'content_item_kinds': ['user.text'], 'content_item_metadata': [{'provenance': {'type': provenance}}],
            },
        })

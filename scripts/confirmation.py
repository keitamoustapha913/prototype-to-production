"""Interpret scoped replies and bind them to Codex's current user message."""

from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re


QUESTIONS = {
    'initial_alignment': r'(?:do you confirm (?:my|this) understanding|is (?:my|this) understanding correct)',
    'discovery': r'(?:do you (?:confirm|approve) (?:this|the) discovery audit|is (?:this|the) discovery audit correct)',
    'roadmap': r'(?:does this roadmap capture the (?:direction|scope)|do you confirm (?:this|the) roadmap)',
    'workstream': r'(?:continue|continue to the next workstream|are you ready to (?:continue|move to the next workstream))',
    'plan_handoff': (r'(?:(?:would you like me to |may i )?(?:continue|proceed) (?:to|with) '
                     r'(?:the )?(?:detailed blueprint|blueprint creation|blueprint)|would you like me to create the detailed blueprint)'),
    'blueprint_approval': r'(?:(?:do you )?approve (?:this blueprint|blueprint [a-z0-9-]+))',
    'confirm_map': r'(?:(?:do you )?confirm (?:this|the) slice map)',
    'continue': r'(?:(?:are you ready to )?continue(?: to the next slice)?)',
    'approve': r'(?:(?:do you )?approve (?:this slice|this slice revision))',
    'delegate': r'(?:(?:do you want to )?delegate review of these slice revisions)',
    'lock': r'(?:(?:may i |should i )?lock (?:this|the) (?:reviewed )?package)',
    'start': r'(?:(?:may i |should i )?(?:start|begin) implementation(?: of (?:this|the) locked package)?)',
    'final_acceptance': r'(?:(?:do you )?accept (?:this|the) final audit)',
}
RELATIVE = {
    'initial_alignment': {'that looks correct', 'your understanding is correct', 'i confirm your understanding',
                          'confirm understanding', 'i confirm this understanding'},
    'discovery': {'that looks correct', 'the discovery audit looks correct', 'i confirm this discovery audit'},
    'roadmap': {'that looks correct', 'the roadmap matches the scope', 'i confirm this roadmap'},
    'workstream': {'continue', 'next', 'move to the next workstream', 'move on', "let’s move on", "let's move on"},
    'plan_handoff': {'continue to blueprint', 'proceed to the detailed blueprint', 'go ahead with the blueprint'},
    'blueprint_approval': {'approve this blueprint', 'i approve this blueprint', 'i approve this version', 'i approve'},
    'confirm_map': {'confirm this slice map', 'i confirm this slice map', 'i approve this slice map'},
    'continue': {'continue', 'next', 'move to the next slice', 'continue to the next slice'},
    'approve': {'approve this slice', 'i approve this slice', 'i approve this revision'},
    'delegate': {'delegate review of these slice revisions', 'i delegate review of these slice revisions'},
    'lock': {'lock this package', 'i approve locking this package', 'go ahead and lock it'},
    'start': {'start implementation', 'begin implementation', 'start coding', 'you can start implementation',
              'go ahead and start implementation'},
    'final_acceptance': {'accept this final audit', 'i accept this final audit', 'i accept this report', 'i accept'},
}
YES = {'yes', 'yes please', 'yes, please', 'yes go ahead', 'yes, go ahead'}
YES_CONTINUE = {'yes continue', 'yes, continue'}
ACKNOWLEDGMENTS = {'i understand', 'understood', 'okay', 'ok', 'got it', 'thanks', 'thank you'}
CONDITIONAL_REPLY = re.compile(
    r'^(?:maybe|possibly|probably|it depends|if|unless|only if|as long as|provided that)\b'
    r'|^(?:yes|sure|okay|ok|continue|i approve|i confirm|that looks correct)\b.*'
    r'\b(?:if|unless|but|as long as|provided that)\b'
)


def enabled(manifest):
    return manifest.get('confirmation_protocol_version') == 1


def normalize(value):
    return ' '.join(value.casefold().split()).rstrip('.!').rstrip()


def is_scoped_question(action, question):
    lines = question.strip().splitlines()
    prompt = normalize(lines[-1]) if lines else ''
    return prompt.endswith('?') and re.fullmatch(QUESTIONS[action], prompt[:-1]) is not None


def accepts(action, reply, question, *, explicit=()):
    if not isinstance(reply, str):
        return False
    text = normalize(reply)
    scoped = is_scoped_question(action, question)
    if action != 'start' and text in YES:
        return scoped
    if text in YES_CONTINUE:
        return scoped and action in {'workstream', 'plan_handoff', 'continue'}
    text = re.sub(r'^(?:yes[, ]+)?(?:please )?', '', text).removesuffix(' please').removesuffix(' now').rstrip(',')
    if text in {normalize(phrase) for phrase in explicit}:
        return action != 'start' or scoped
    return scoped and text in RELATIVE[action]


def clarify(state, action, reply, question, *, explicit=()):
    state.require(accepts(action, reply, question, explicit=explicit),
                  'The reply does not clearly authorize this step. Ask one short question about this action and wait.')


def contains_scope(text, value):
    return re.search(r'(?<![\w-])' + re.escape(str(value)) + r'(?![\w-])', text) is not None


def visible_text(payload, kind):
    content = payload.get('content', [])
    if not content or any(item.get('type') != kind or not isinstance(item.get('text'), str) for item in content):
        return None
    return '\n'.join(item['text'] for item in content)


def current_exchange(state):
    thread = os.environ.get('CODEX_THREAD_ID', '')
    state.require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', thread),
                  'A current Codex session is required to verify the user reply')
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    store = home / 'sessions'
    logs = list(store.rglob(f'rollout-*-{thread}.jsonl'))
    state.require(len(logs) == 1 and logs[0].resolve().is_relative_to(store),
                  'The current Codex session log is unavailable. Wait for a verifiable user reply.')
    prompt = answer = latest = None
    with logs[0].open(encoding='utf-8') as stream:
        session = json.loads(next(stream, '{}'))
        meta = session.get('payload', {})
        root = state.directory.parents[2].resolve()
        cwd = Path(meta.get('cwd') or '/').resolve()
        state.require(session.get('type') == 'session_meta' and meta.get('id') == thread
                      and (root.is_relative_to(cwd) or cwd.is_relative_to(root)),
                      'The Codex session does not match this project')
        for line in stream:
            if not line.endswith('\n'):
                break
            record = json.loads(line)
            payload = record.get('payload', {})
            if record.get('type') != 'response_item' or payload.get('type') != 'message':
                continue
            if payload.get('role') == 'assistant' and payload.get('phase') in {'commentary', 'final', 'final_answer'}:
                text = visible_text(payload, 'output_text')
                if text:
                    prompt = (payload.get('id'), record.get('timestamp'), text)
            elif payload.get('role') == 'user':
                attributes = payload.get('internal_chat_message_metadata_passthrough') or {}
                kinds = attributes.get('content_item_kinds', [])
                metadata = attributes.get('content_item_metadata', [])
                if 'user.text' not in kinds:
                    continue
                text = (visible_text(payload, 'input_text') if len(kinds) == len(metadata)
                        and all(item.get('provenance', {}).get('type') == 'user' for item in metadata) else None)
                answer = (payload.get('id'), record.get('timestamp'), text)
                latest, prompt = prompt, None
    state.require(latest and answer and all(latest) and all(answer),
                  'No visible Codex prompt and user-originated reply could be verified')
    return thread, latest, answer


def bind(state, question, response, scope, action, *, not_before=None, single_question=False):
    return bind_exchange(state, current_exchange(state), question, response, scope, action,
                         not_before=not_before, single_question=single_question)


def prompt_lines(text):
    return '\n'.join(line.rstrip(' \t') for line in text.strip().splitlines())


def bind_exchange(state, exchange, question, response, scope, action, *, not_before=None, single_question=False):
    thread, (prompt_id, shown_at, shown), (user_id, replied_at, reply) = exchange
    state.require(reply.strip() == response.strip(),
                  'The recorded reply must match the latest user exchange in the Codex session')
    state.require(prompt_lines(shown).endswith(prompt_lines(question)),
                  'The saved prompt must match the latest user exchange. '
                  'Put the exact saved question in plain text on the final line.')
    values = [state.manifest['initiative_id'], *scope]
    state.require(all(contains_scope(shown, value) for value in values),
                  'Show the initiative ID and exact review scope in the same message as the confirmation question')
    if single_question or normalize(response) in (YES | YES_CONTINUE):
        state.require(shown.count('?') == 1,
                      'Ask one specific confirmation question in its own message and wait for the reply')
    before = not_before or state.manifest['provenance']['created_at']
    timestamps = [datetime.fromisoformat(date.replace('Z', '+00:00')) for date in (before, shown_at, replied_at)]
    opened, displayed, replied = timestamps
    state.require(max(opened, displayed) <= replied,
                  'The reply predates this checkpoint. Present the current scope and ask again.')
    references = [record.get('confirmation_origin') for record in state.conversations()]
    if acceptance := state.manifest.get('visual_quality', {}).get('accept'):
        relative = f'visual-quality/accept/{acceptance}.json'
        saved = state.api.read_json(state.api.artifact_file(state.directory, relative))
        references.append(saved.get('confirmation_origin'))
    state.require(not any(ref and ref.get('session_id') == thread and ref.get('reply_id') == user_id
                          for ref in references), 'This user reply already confirmed another checkpoint')
    if action == 'start':
        state.require('source code' in shown.casefold() and re.search(r'\b(?:edit|edits|modify|change|changes)\b', shown.casefold()),
                      'Explain that starting implementation authorizes edits to source code')
    return {'session_id': thread, 'prompt_id': prompt_id, 'prompt_at': shown_at, 'reply_id': user_id,
            'reply_at': replied_at, 'action': action, 'scope': values,
            'prompt_sha256': sha256(shown.encode()).hexdigest(), 'reply_sha256': sha256(reply.encode()).hexdigest()}


REVIEW_CONFIRMATIONS = {
    'initial_alignment': ('confirm', 'initial_alignment'),
    'discovery': ('confirm', 'discovery'),
    'roadmap': ('confirm', 'roadmap'),
    'plan_workstream': ('continue', 'workstream'),
    'plan_completion': ('continue', 'plan_handoff'),
    'workstream': ('continue', 'workstream'),
    'blueprint_approval': ('confirm', 'blueprint_approval'),
}


def blueprint_replies(version):
    return (f'Approve blueprint {version}', f'I approve blueprint {version}', f'I approve version {version}')


def review_scope(checkpoint):
    scope = [checkpoint['review_version']]
    if name := checkpoint.get('review_workstream'):
        scope.append(name)
    return scope


def preview(state, checkpoint_id=None, *, check_reply=False):
    state.require(enabled(state.manifest), 'Confirmation preview requires scoped confirmations')
    checkpoints = [
        record for record in state.pending()
        if record.get('record_type') == 'review_checkpoint'
        and record.get('review_step') in REVIEW_CONFIRMATIONS
        and (checkpoint_id is None or record['exchange_id'] == checkpoint_id)
    ]
    state.require(checkpoints, 'No matching open confirmation checkpoint')
    state.require(len(checkpoints) == 1, 'Select an open confirmation checkpoint with --checkpoint')
    checkpoint = checkpoints[0]
    _, action = REVIEW_CONFIRMATIONS[checkpoint['review_step']]
    scope = review_scope(checkpoint)
    initiative = state.manifest['initiative_id']
    question = checkpoint['question'].strip()
    result = {
        'status': 'present_question', 'checkpoint_id': checkpoint['exchange_id'],
        'action': action, 'initiative_id': initiative, 'review_version': scope[0],
        'workstream': checkpoint.get('review_workstream'), 'question': question,
        'display': f"Initiative {initiative} · Review {' · '.join(scope)}\n\n{question}",
    }
    state.require(is_scoped_question(action, question) and question.count('?') == 1,
                  'The saved question is unsupported for scoped confirmation. '
                  'Use the review workflow to open a corrected checkpoint before asking for approval.')
    explicit = blueprint_replies(scope[0]) if action == 'blueprint_approval' else ()
    return preview_reply(state, result, scope, explicit=explicit,
                         not_before=checkpoint['recorded_at']) if check_reply else result


def preview_reply(state, result, scope, *, explicit=(), not_before=None, single_question=False):
    action, question = result['action'], result['question']
    try:
        exchange = current_exchange(state)
        reply = exchange[2][2]
        text = normalize(reply)
        if text in ACKNOWLEDGMENTS:
            result['status'] = 'acknowledgment'
        elif CONDITIONAL_REPLY.search(text):
            result['status'] = 'clarification_required'
        elif not accepts(action, reply, question, explicit=explicit):
            result['status'] = 'discussion_required'
        else:
            bind_exchange(state, exchange, question, reply, scope, action,
                          not_before=not_before, single_question=single_question)
            result.update(status='confirmation_ready', reply=reply)
    except state.api.StateError as error:
        result.update(status='reissue_required', reason=str(error))
    return result


def record_review(state, value):
    if value['status'] != 'decided':
        return
    step = value['review_step']
    expected, action = REVIEW_CONFIRMATIONS[step]
    chosen = value.get('review_action')
    state.require(chosen in {expected, 'revisit', 'revise'} and value.get('responds_to'),
                  'Record the actual reply to the exact review checkpoint')
    prompt = state.load('conversation', value['responds_to'])
    version = value.get('review_version')
    state.require(state.identifier(version), 'The review confirmation needs an exact version')
    explicit = blueprint_replies(version) if action == 'blueprint_approval' else ()
    response = normalize(value['response'])
    state.require(response not in ACKNOWLEDGMENTS,
                  'The reply is an acknowledgment. Keep the confirmation checkpoint open.')
    state.require(not CONDITIONAL_REPLY.search(response),
                  'The reply is conditional or uncertain. Ask what the user means and keep the checkpoint open.')
    if chosen == expected:
        clarify(state, action, value['response'], prompt['question'], explicit=explicit)
    value['confirmation_origin'] = bind(
        state, prompt['question'], value['response'], review_scope(value),
        action if chosen == expected else chosen, not_before=prompt['recorded_at'])

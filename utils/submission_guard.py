"""Durable, cross-process reservations for SABnzbd submissions.

The sidecar lock is held across checking, reserving, submitting and recording a
job.  The reservation is written *before* the network call, so a killed process
or an ambiguous HTTP timeout cannot silently turn into a duplicate submission.
"""
import uuid
import re
import json
import os
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from utils.json_store import atomic_write_json, file_lock, lock_path_for


@contextmanager
def try_run_lock(config_dir):
    """One mutating grabber process at a time; never queue a hidden second run."""
    path = lock_path_for(Path(config_dir) / 'grabber_run.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def intent_path(config_dir):
    return Path(config_dir) / 'submission_intents.json'


def load_intents(path):
    # Fail closed on corrupt state: read_json's default-on-error would silently
    # allow duplicate downloads. Missing file, unlike corruption, is normal.
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError('Invalid submission intents; refusing to submit')
    for token, entry in data.items():
        if (not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token)
                or not isinstance(entry, dict) or entry.get('token') != token
                or not isinstance(entry.get('keys'), list) or not entry['keys']
                or any(not isinstance(key, str) or not key for key in entry['keys'])):
            raise ValueError('Invalid submission intent; refusing to submit')
    return data


def validate_grabber_state(path):
    """Do not turn an unreadable prior grab record into permission to send."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError('Invalid grabber state; refusing to submit')
    for field in ('searched', 'grabbed', 'failed'):
        if field in data and not isinstance(data[field], dict):
            raise ValueError('Invalid grabber state; refusing to submit')


def make_intent(keys, title, mode, series=None):
    """Reserve one submission; ``series`` disambiguates overlapping session keys."""
    token = uuid.uuid4().hex
    intent = {'keys': list(keys), 'release': title, 'mode': mode,
              'created': datetime.now().isoformat(), 'token': token}
    if series:
        intent['series'] = series
    return token, intent


def intents_for_series(intents, series=None):
    """Select one series' reservations; old entries belong to the default."""
    from series import default_series

    wanted = series or default_series().key
    default = default_series().key
    return {
        token: intent for token, intent in (intents or {}).items()
        if (intent.get('series') or default) == wanted
    }


def save_intents(path, intents):
    """Commit the reservation and its directory entry before an external call."""
    atomic_write_json(path, intents)
    fd = os.open(str(Path(path).parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def clear_intent(path, token):
    """Explicit user acknowledgement, under the same lock as submission."""
    with file_lock(path):
        intents = load_intents(path)
        if token not in intents:
            return False
        del intents[token]
        save_intents(path, intents)
        return True


def find_sab_job(token, url, api_key, get):
    """Find an exact, unique intent marker; lack of proof is not proof of absence.

    Return an id only on an unambiguous match. A purged history, partial API
    failure or SAB version that drops the marker leaves the intent pending.
    """
    if not url or not api_key:
        return None
    marker = 'mgp-' + token
    ids = set()
    try:
        for mode in ('queue', 'history'):
            start = 0
            for _ in range(10):  # bounded even for a broken paging endpoint
                response = get(f'{url}/api', params={
                    'mode': mode, 'start': start, 'limit': 100,
                    'apikey': api_key, 'output': 'json',
                }, timeout=10)
                if response.status_code != 200:
                    return None
                section = response.json().get(mode)
                if not isinstance(section, dict) or not isinstance(section.get('slots'), list):
                    return None
                slots = section['slots']
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    names = [slot.get('filename', ''), slot.get('name', '')]
                    if any(re.search(re.escape(marker) + r'(?![a-zA-Z0-9])', str(name))
                           for name in names):
                        job_id = slot.get('nzo_id')
                        if not job_id:
                            return None
                        ids.add(job_id)
                if len(slots) < 100:
                    break
                start += len(slots)
            else:
                return None
    except Exception:
        return None
    return next(iter(ids)) if len(ids) == 1 else None

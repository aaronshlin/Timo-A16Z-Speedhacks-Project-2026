"""Preview the next fictional session without a trigger, HTTP client, or call."""
import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def preview(state_path=None):
    # Resolve the same .env and relative paths as the main app launched at root.
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.agent.engine import Engine

    path = Path(state_path or settings.CALL_ENGINE_STATE_PATH
                or ROOT / 'calling-mvp/.timo-state.json').resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('The backend is running. Stop it before previewing its state.') from None
        # Engine migrates pending fixtures by ID and retains TTLs and history.
        # No HTTP client or optional context adapter is constructed.
        engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, path, client=None)
        item = engine.next_pending('johnson')
        result = {'calls_enabled': False, 'opportunity_reserved': False,
                  'context_storage': 'local_only', 'next_session': None}
        if item:
            result['next_session'] = {
                key: item[key] for key in
                ('id', 'title', 'priority', 'say', 'confirmation',
                 'decision', 'status', 'created_at', 'expires_at')
            }
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-path', type=Path,
                        help='Optional Engine JSON state path; defaults to the configured main state.')
    args = parser.parse_args(argv)
    try:
        result = preview(args.state_path)
    except ValueError as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

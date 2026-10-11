"""Read the real calendar and reserve a dry-run opportunity; never enable writes/calls."""
import argparse
import json
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-path', required=True, help='Persistent validation SQLite file; reuse it for restart checks.')
    parser.add_argument('--ticks', type=int, default=2)
    args = parser.parse_args()
    os.environ.update(USE_MOCKS='0', LIVE_CALLS_ENABLED='false', CALL_OPT_IN='false', POLLER_ENABLED='false',
                      NOW_OVERRIDE='', CALL_STATE_PATH=args.state_path, RECEIPT_MODE='dashboard',
                      SMS_ENABLED='false', MOCK_DIAL='true')
    from app.agent.poller import run_tick_detailed
    from app.agent.calling import service
    from app.context.oauth import token_path
    from app.config import settings
    evidence = {'real_calendar_reads': True, 'google_token_present': token_path().is_file(),
                'calls_enabled': settings.LIVE_CALLS_ENABLED, 'calendar_writes_enabled': False, 'sms_enabled': False,
                'ticks': [run_tick_detailed() for _ in range(args.ticks)],
                'reservations_count': len(service().sessions())}
    print(json.dumps(evidence, indent=2))
    return 0 if all(t.get('snapshot_complete') for t in evidence['ticks']) else 2


if __name__ == '__main__':
    raise SystemExit(main())

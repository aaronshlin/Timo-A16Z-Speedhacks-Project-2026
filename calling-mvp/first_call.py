"""Compatibility command for one manual call through the same main app and Engine."""
import sys
from serve_calls import main

if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--call']:
            main(['--live', '--manual-call'])
        else:
            main(sys.argv[1:])
    except KeyboardInterrupt:
        print('\nStopped.')
    except Exception as error:
        print(str(error) if isinstance(error, ValueError) else 'Manual call failed; no automatic retry.', file=sys.stderr)
        sys.exit(1)

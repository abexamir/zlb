"""Exercise real route changes in a disposable Linux network namespace."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zlb.routes import Routes  # noqa: E402


def run(*args):
    return subprocess.check_output(args, text=True)


def command(*args):
    return run(*args)


def main():
    run('ip', 'link', 'set', 'lo', 'up')
    for interface, address in (('mgmt0', '10.0.0.2/24'), ('wan0', '198.51.100.2/24')):
        run('ip', 'link', 'add', interface, 'type', 'dummy')
        run('ip', 'link', 'set', interface, 'up')
        run('ip', 'address', 'add', address, 'dev', interface)
    run('ip', 'route', 'add', 'default', 'via', '10.0.0.1', 'metric', '100')
    run('ip', 'route', 'add', '203.0.113.0/24', 'via', '10.0.0.1')
    desired = [dict(destination='0.0.0.0/0', interface='wan0', gateway='198.51.100.1'),
               dict(destination='10.0.0.0/8', interface='mgmt0', gateway='10.0.0.1')]
    def dev(ip):
        return json.loads(run('ip', '-j', 'route', 'get', ip))[0]['dev']
    with tempfile.TemporaryDirectory() as directory:
        journal = Path(directory) / 'routes.json'
        routes = Routes(command, desired, journal)
        routes.reconcile()
        assert dev('1.1.1.1') == 'wan0'
        assert dev('10.8.0.2') == 'mgmt0'
        assert dev('203.0.113.7') == 'mgmt0'
        run('ip', 'route', 'del', 'default', 'metric', '100')
        Routes(command, desired, journal).reconcile()
        assert dev('1.1.1.1') == 'wan0'
        Routes(command, desired, journal).cleanup()
        assert dev('1.1.1.1') == 'mgmt0'
        assert dev('203.0.113.7') == 'mgmt0'
    print('PASS: public default, management return route, restart/drift recovery and original-route restoration', flush=True)


if __name__ == '__main__':
    if os.environ.get('ZLB_ROUTES_ISOLATED') != '1':
        os.execvpe('unshare', ['unshare', '-n', sys.executable, __file__], dict(os.environ, ZLB_ROUTES_ISOLATED='1'))
    main()

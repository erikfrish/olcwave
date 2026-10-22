#!/usr/bin/env python3
"""Reset selected OLCWave quotas once per calendar month, Europe/Moscow."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import ssl
import urllib.request
from zoneinfo import ZoneInfo
import fcntl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/olcwave/monthly-quota.json')
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    month = datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y-%m')
    state = Path(config['state_file'])
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        done = json.loads(state.read_text()) if state.exists() else {}
        pending = [u for u in config['users'] if done.get(u) != month]
        if args.plan:
            print(json.dumps({'month': month, 'resets_due': len(pending)}))
            return
        if not pending:
            print('No monthly resets due')
            return
        env = dict(line.split('=', 1) for line in Path(config['env_file']).read_text().splitlines() if '=' in line and not line.startswith('#'))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=config['ca_file'])))
        def call(path, body, token=None):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            request = urllib.request.Request(config['api_url'] + path, data=json.dumps(body).encode(), headers=headers, method='POST')
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        token = call('/auth/login', {'username': env['ADMIN_USERNAME'], 'password': env['ADMIN_PASSWORD']})['access_token']
        for user in pending:
            call('/users/traffic/reset?short_uuid=' + user, {}, token)
            done[user] = month
            fd, temp = tempfile.mkstemp(dir=state.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(done, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, state)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        print(f'Reset {len(pending)} monthly quotas for {month}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Read supported OLCWave APIs; never expose bearer credentials or subscription URLs."""
import json
import os
import time
import urllib.request

BASE = 'http://127.0.0.1:8000'


def request(path, token=None, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})})
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.load(response)


def main():
    result = {'api': False, 'nodes': [], 'users': [], 'subscriptions': [], 'timestamp': time.time()}
    try:
        token = request('/auth/login', body={'username': os.environ['ADMIN_USERNAME'], 'password': os.environ['ADMIN_PASSWORD']})['access_token']
        result['api'] = True
    except Exception:
        print(json.dumps(result))
        return
    try:
        result['nodes'] = request('/containers/all_status', token)['nodes']
    except Exception:
        result['nodes_error'] = True
    try:
        users = request('/users/all', token)
        result['users'] = [{'user': u['name'], 'used': u['traffic_used_bytes'], 'limit': u['traffic_limit_bytes']} for u in users]
        for user in users:
            if user['name'] not in ('erikfrish-ghostlane', 'oleg-ghostlane'):
                continue
            sample = {'user': user['name'], 'up': False, 'profiles': 0}
            try:
                req = urllib.request.Request('https://olcsub.nazavo.de/' + user['short_uuid'], headers={'User-Agent': 'Ghostlane/monitoring'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    text = response.read(262144).decode()
                    sample['profiles'] = sum(line.startswith('olcrtc://') for line in text.splitlines())
                    sample['up'] = response.status == 200 and sample['profiles'] == 6
            except Exception:
                pass
            result['subscriptions'].append(sample)
    except Exception:
        result['users_error'] = True
    print(json.dumps(result))


if __name__ == '__main__':
    main()

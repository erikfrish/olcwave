#!/usr/bin/env python3
"""Read supported OLCWave APIs; never expose bearer credentials or subscription URLs."""
import json
import os
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:8000'


def request(path, token=None, body=None, timeout=8):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def remnawave_up():
    """Any HTTP answer from the panel counts: the reconciler only needs reachability."""
    url = os.environ.get('RW_API_URL')
    if not url:
        return None
    # Remnawave drops plain-HTTP requests that lack proxy headers (same as rw/sdk.py).
    req = urllib.request.Request(url.rstrip('/') + '/api/system/health',
                                 headers={'x-forwarded-proto': 'https', 'x-forwarded-for': '127.0.0.1'})
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def expected_profiles(profiles, short_uuid):
    """Mirror Subscriptions.profiles_for_user: restricted tags list allowed short UUIDs."""
    restrictions = json.loads(os.environ.get('OLCWAVE_PROFILE_USERS_JSON') or '{}')
    return sum(p['tag'] not in restrictions or short_uuid in restrictions[p['tag']] for p in profiles)


def main():
    result = {'api': False, 'nodes': [], 'containers': [], 'users': [], 'subscriptions': [],
              'remnawave': remnawave_up(), 'timestamp': time.time()}
    try:
        token = request('/auth/login', body={'username': os.environ['ADMIN_USERNAME'], 'password': os.environ['ADMIN_PASSWORD']})['access_token']
        result['api'] = True
    except Exception:
        print(json.dumps(result))
        return
    try:
        # A dead node costs ~2x ssh ConnectTimeout before the API answers;
        # a short timeout here would report every node as down.
        status = request('/containers/all_status', token, timeout=30)
        result['nodes'] = status['nodes']
        result['containers'] = [{'node': c['node'], 'status': c['status']} for c in status.get('containers', [])]
    except Exception:
        result['nodes_error'] = True
    try:
        users = request('/users/all', token)
        profiles = request('/profiles/all', token)
        result['users'] = [{'user': u['name'], 'used': u['traffic_used_bytes'], 'limit': u['traffic_limit_bytes']} for u in users]
        for user in users:
            if user['name'] not in ('erikfrish-ghostlane', 'oleg-ghostlane'):
                continue
            sample = {'user': user['name'], 'up': False, 'profiles': 0,
                      'expected': expected_profiles(profiles, user['short_uuid'])}
            try:
                req = urllib.request.Request('https://olcsub.nazavo.de/' + user['short_uuid'], headers={'User-Agent': 'Ghostlane/monitoring'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    text = response.read(262144).decode()
                    sample['profiles'] = sum(line.startswith('olcrtc://') for line in text.splitlines())
                    sample['up'] = response.status == 200 and sample['profiles'] == sample['expected'] > 0
            except Exception:
                pass
            result['subscriptions'].append(sample)
    except Exception:
        result['users_error'] = True
    print(json.dumps(result))


if __name__ == '__main__':
    main()

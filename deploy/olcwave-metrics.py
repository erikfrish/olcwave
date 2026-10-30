#!/usr/bin/env python3
"""Private cached metrics endpoint; one bounded API sample per collection cycle."""
import argparse
import json
from pathlib import Path
import socket
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def metric(name, value, **labels):
    suffix = '{' + ','.join(k + '=' + json.dumps(str(v), ensure_ascii=False) for k, v in labels.items()) + '}' if labels else ''
    return f'{name}{suffix} {float(value)}\n'


def collect(snapshot, nodes, api_exec):
    text = ''
    for node in (n for n in nodes if n != 'local'):
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(2)
                sock.connect(f'/run/olcwave-nodes/{node}.sock')
            up = 1
        except OSError:
            up = 0
        text += metric('olcwave_relay_socket_up', up, node=node)
    try:
        proc = subprocess.run(api_exec, input=snapshot,
                              capture_output=True, text=True, timeout=50, check=True)
        data = json.loads(proc.stdout)
    except Exception:
        return text + metric('olcwave_collection_success', 0) + metric('olcwave_api_up', 0)
    text += metric('olcwave_collection_success', 1)
    text += metric('olcwave_collection_timestamp_seconds', data['timestamp'])
    text += metric('olcwave_api_up', data['api'])
    for node in data['nodes']:
        text += metric('olcwave_node_up', node['ok'], node=node['node'])
        if node['ok']:
            text += metric('olcwave_node_containers', node['containers'], node=node['node'])
    if data.get('nodes_error'):
        for node in nodes:
            text += metric('olcwave_node_up', 0, node=node)
    if not data.get('users_error') and data['api']:
        text += metric('olcwave_users', len(data['users']))
        for user in data['users']:
            text += metric('olcwave_user_traffic_used_bytes', user['used'], user=user['user'])
            text += metric('olcwave_user_traffic_limit_bytes', user['limit'], user=user['user'])
    for user in data['subscriptions']:
        text += metric('olcwave_subscription_up', user['up'], user=user['user'])
        text += metric('olcwave_subscription_profiles', user['profiles'], user=user['user'])
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='10.69.1.100:9465')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--nodes', default='local,vm1,vm2,vm3',
                        help="comma-separated panel nodes; every node except 'local' has a relay socket")
    parser.add_argument('--api-exec', default='docker exec -i olcwave-api python -',
                        help='command that runs the snapshot script (stdin) next to the API')
    args = parser.parse_args()
    nodes = tuple(n for n in args.nodes.split(',') if n)
    api_exec = shlex.split(args.api_exec)
    snapshot = Path(__file__).with_name('olcwave-metrics-snapshot.py').read_text()
    if args.once:
        print(collect(snapshot, nodes, api_exec), end='')
        return
    current = [metric('olcwave_collection_success', 0)]
    lock = threading.Lock()

    def loop():
        while True:
            text = collect(snapshot, nodes, api_exec)
            with lock:
                current[0] = text
            time.sleep(30)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404)
                return
            with lock:
                body = current[0].encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    threading.Thread(target=loop, daemon=True).start()
    host, port = args.listen.rsplit(':', 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == '__main__':
    main()

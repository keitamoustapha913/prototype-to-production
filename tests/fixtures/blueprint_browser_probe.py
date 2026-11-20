"""Observe the browser scenario through the CLI's executable evidence contract."""

from functools import partial
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
import sys
from threading import Thread


class ItemsHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/items':
            self.path = '/items.html'
        return super().do_GET()


def observation(check_id, expected, actual):
    return {'check_id': check_id, 'expected': expected, 'actual': actual,
            'result': 'PASS' if expected == actual else 'FAIL'}


def main(browser, kind):
    viewport = {'width': 800, 'height': 600}
    with ThreadingHTTPServer(('127.0.0.1', 0), partial(ItemsHandler, directory='src')) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/items'
        chrome = [browser, '--headless', '--no-sandbox', '--disable-background-networking',
                  '--disable-gpu', '--no-first-run', '--hide-scrollbars', '--window-size=800,600']
        dom = subprocess.check_output(
            [*chrome, '--dump-dom', url], text=True, stderr=subprocess.DEVNULL, timeout=15)
        observed = json.loads(unescape(re.search(r'<output id="observation">([^<]+)</output>', dom)[1]))
        visual = observation('gap', '16px', observed['gap'])
        if kind == 'render':
            subprocess.run([*chrome, '--screenshot=' + os.environ['P2P_SCREENSHOT'], url],
                           capture_output=True, check=True, timeout=15)
            return {'feature_id': 'items-feature', 'case_id': 'success-case', 'route': observed['route'],
                    'state': 'success', 'viewport': viewport, 'checks': [visual]}
        if kind == 'gap':
            return visual
        return observation('toggle', 'visible', observed['detail'])


if __name__ == '__main__':
    print(json.dumps(main(*sys.argv[1:])))

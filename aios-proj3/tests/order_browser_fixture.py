"""Offline browser acceptance server. Never contacts a provider."""
import argparse
import json
from pathlib import Path
import time

from cerebrum.llm.apis import LLMResponse
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole, create_server
from tests.test_order_project import fixed_files
from tests.native_browser_fixture import TEXT
from aios.tool.order_project import SOURCE


def main():
    parser = argparse.ArgumentParser();parser.add_argument('root');args = parser.parse_args()
    with AIOSKernelRuntime(storage_root=str(Path(args.root) / 'store')) as kernel:
        console = NativeConsole(kernel, Path(args.root) / 'jobs')
        original = console.model_service.request
        console.model_service.request = lambda op, *a, **kw: (
            {'configured': True, 'model': 'offline-fixed-fixture'} if op == 'status' else original(op, *a, **kw))
        order_calls = []
        def complete(query):
            if 'Repair this fixed Python package' in str(query.messages):
                order_calls.append(1)
                text = json.dumps({'files': SOURCE if len(order_calls) == 1 else fixed_files()})
                time.sleep(0.15)
                return LLMResponse(response_message=text, usage={'prompt_tokens': 100, 'completion_tokens': 200})
            for end in range(31, len(TEXT), 31):
                if query.stream_observer: query.stream_observer({'text': TEXT[:end], 'delta': TEXT[end-31:end]})
                time.sleep(0.005)
            return LLMResponse(response_message=TEXT, usage={'prompt_tokens': 20, 'completion_tokens': 100})
        console.model_service.complete = complete
        try:
            with create_server(console, 0) as server:
                print(f'http://127.0.0.1:{server.server_port}/', flush=True)
                server.serve_forever()
        finally: console.close()


if __name__ == '__main__': main()

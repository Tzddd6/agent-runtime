"""Test-only fixed model transport for browser rendering acceptance."""
import argparse
from pathlib import Path
import time

from cerebrum.llm.apis import LLMResponse
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole, create_server


TEXT = '**粗体**\n\n$$\\int_0^1 x\\,dx=\\frac12$$\n\n```python\nprint("<safe>")\n```\n\n|名字|数值|\n|---|---|\n|测试|42|\n\n' + '这是长回答的完整段落。\n\n' * 30


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    args = parser.parse_args()
    with AIOSKernelRuntime(storage_root=str(Path(args.root) / 'store')) as kernel:
        console = NativeConsole(kernel, Path(args.root) / 'jobs')
        def complete(query):
            for end in range(1, len(TEXT), 31):
                query.stream_observer({'text': TEXT[:end], 'delta': TEXT[max(0, end - 31):end]})
                time.sleep(0.008)
            return LLMResponse(response_message=TEXT, usage={'total_tokens': 100})
        console.model_service.complete = complete
        try:
            with create_server(console, 0) as server:
                print(f'http://127.0.0.1:{server.server_port}/', flush=True)
                server.serve_forever()
        finally:
            console.close()


if __name__ == '__main__':
    main()

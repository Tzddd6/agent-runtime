"""Bounded UTF-8 SSE framing. Never retries or stores provider payloads."""
import codecs


def payloads(response, limit, *, observe=None):
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    line = []; data = []; size = 0; event_size = 0; first = True; cr = False
    read = getattr(response, 'read1', None)
    if read is None:
        read = response.readline
    while True:
        chunk = read(min(limit + 1, 65536))
        if not chunk:
            decoder.decode(b'', final=True)
            # An unterminated event is not a valid terminal receipt.
            return
        size += len(chunk)
        if size > 8 * limit:
            raise ValueError('SSE wire limit exceeded')
        if observe:
            observe(size)
        text = decoder.decode(chunk)
        for char in text:
            if first:
                first = False
                if char == '\ufeff':
                    continue
            if char == '\n' and cr:
                cr = False
                continue
            cr = char == '\r'
            if char not in ('\r', '\n'):
                line.append(char)
                event_size += len(char.encode('utf-8'))
                if event_size > limit:
                    raise ValueError('SSE event limit exceeded')
                continue
            value = ''.join(line); line.clear()
            if not value:
                if data:
                    yield '\n'.join(data)
                data.clear(); event_size = 0
            elif not value.startswith(':'):
                field, _, value = value.partition(':')
                if value.startswith(' '):
                    value = value[1:]
                if field == 'data':
                    data.append(value)

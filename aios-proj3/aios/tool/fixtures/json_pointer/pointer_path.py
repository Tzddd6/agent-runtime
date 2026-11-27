from pointer_codec import escape, unescape


def join_pointer(parts):
    return '/' + '/'.join(escape(part) for part in parts)


def split_pointer(path):
    if not path:
        return []
    if not path.startswith('/'):
        raise ValueError('A nonempty pointer must start with /')
    return [unescape(part) for part in path[1:].split('/') if part]

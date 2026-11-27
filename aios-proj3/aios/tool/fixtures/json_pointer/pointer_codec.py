# Adapted from python-json-pointer 3.0.0. See LICENSE.txt and provenance.json.


def escape(s):
    return s.replace('~', '~0').replace('/', '~1')


def unescape(s):
    return s.replace('~0', '~').replace('~1', '/')

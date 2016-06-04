from io import BytesIO
from cStringIO import StringIO
from eventlet import sleep
from oiopy.utils import HeadersDict

CHUNK_SIZE = 1048576
EMPTY_CHECKSUM = 'd41d8cd98f00b204e9800998ecf8427e'


class FakeResponse(object):
    def __init__(self, status, body='', headers=None, slow=0):
        self.status = status
        self.body = body
        self.headers = HeadersDict(headers)
        self.stream = BytesIO(body)
        self.slow = slow

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        if 'Content-Length' not in self.headers:
            self.headers['Content-Length'] = len(self.body)
        return self.headers.items()

    def _slow(self):
        sleep(self.slow)

    def read(self, amt=0):
        if self.slow:
            self._slow()
        return self.stream.read(amt)


def decode_chunked_body(raw_body):
    body = ''
    remaining = raw_body
    trailers = {}
    reading_trailers = False
    while remaining:
        if reading_trailers:
            header, remaining = remaining.split('\r\n', 1)
            if header:
                header_key, header_value = header.split(': ', 1)
                trailers[header_key] = header_value
        else:
            # get the hexa_length
            hexa_length, remaining = remaining.split('\r\n', 1)
            length = int(hexa_length, 16)
            if length == 0:
                # reached the end
                reading_trailers = True
            else:
                # get the body
                body += remaining[:length]
                # discard the \r\n
                remaining = remaining[length + 2:]
    return body, trailers


def empty_stream():
    return StringIO("")

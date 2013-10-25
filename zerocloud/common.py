import re
from hashlib import md5
from swift.common.constraints import MAX_META_NAME_LENGTH, MAX_META_VALUE_LENGTH, \
    MAX_META_COUNT, MAX_META_OVERALL_SIZE
from swift.common.swob import Response
from swift.common.utils import split_path

try:
    import simplejson as json
except ImportError:
    import json

ACCESS_READABLE = 0x1
ACCESS_WRITABLE = 0x1 << 1
ACCESS_RANDOM = 0x1 << 2
ACCESS_NETWORK = 0x1 << 3
ACCESS_CDR = 0x1 << 4
ACCESS_CHECKPOINT = 0x1 << 5

DEVICE_MAP = {
    'stdin': ACCESS_READABLE,
    'stdout': ACCESS_WRITABLE,
    'stderr': ACCESS_WRITABLE,
    'input': ACCESS_RANDOM | ACCESS_READABLE,
    'output': ACCESS_RANDOM | ACCESS_WRITABLE,
    'debug': ACCESS_NETWORK,
    'image': ACCESS_CDR,
    'db': ACCESS_CHECKPOINT
}

TAR_MIMES = ['application/x-tar', 'application/x-gtar', 'application/x-ustar']
CLUSTER_CONFIG_FILENAME = 'boot/cluster.map'
NODE_CONFIG_FILENAME = 'boot/system.map'
STREAM_CACHE_SIZE = 128 * 1024

DEFAULT_EXE_SYSTEM_MAP = r'''
    [{
        "name": "executable",
        "exec": {
            "path": "{.object_path}",
            "args": "{.args}"
        },
        "file_list": [
            {
                "device": "stdout",
                "content_type": "{.content_type=text/plain}"
            }
        ]
    }]
    '''

POST_TEXT_ACCOUNT_SYSTEM_MAP = r'''
    [{
        "name": "script",
        "exec": {
            "path": "{.exe_path}",
            "args": "script"
        },
        "file_list": [
            {
                "device": "stdout",
                "content_type": "text/plain"
            }
        ]
    }]
'''

POST_TEXT_OBJECT_SYSTEM_MAP = r'''
    [{
        "name": "script",
        "exec": {
            "path": "{.exe_path}",
            "args": "script"
        },
        "file_list": [
            {
                "device": "stdin",
                "path": {.object_path}
            },
            {
                "device": "stdout",
                "content_type": "text/plain"
            }
        ]
    }]
'''

CHANNEL_TYPE_MAP = {
    'stdin': 0,
    'stdout': 0,
    'stderr': 0,
    'input': 3,
    'output': 3,
    'debug': 0,
    'image': 1,
    'sysimage': 3
}


MD5HASH_LENGTH = len(md5('').hexdigest())
ENV_ITEM = 'name=%s, value=%s\n'
STD_DEVICES = ['stdin', 'stdout', 'stderr']
REPORT_LENGTH = 6
REPORT_VALIDATOR = 0
REPORT_DAEMON = 1
REPORT_RETCODE = 2
REPORT_ETAG = 3
REPORT_CDR = 4
REPORT_STATUS = 5


def merge_headers(current, new):
    if hasattr(new, 'keys'):
        for key in new.keys():
            if not current[key.lower()]:
                current[key.lower()] = str(new[key])
            else:
                current[key.lower()] += ',' + str(new[key])
    else:
        for key, value in new:
            if not current[key.lower()]:
                current[key.lower()] = str(value)
            else:
                current[key.lower()] += ',' + str(value)


def has_control_chars(line):
    if line:
        RE_ILLEGAL = u'([\u0000-\u0008\u000b-\u000c\u000e-\u001f\ufffe-\uffff])' + \
                     u'|' + \
                     u'([%s-%s][^%s-%s])|([^%s-%s][%s-%s])|([%s-%s]$)|(^[%s-%s])' % \
                     (unichr(0xd800), unichr(0xdbff), unichr(0xdc00), unichr(0xdfff),
                      unichr(0xd800), unichr(0xdbff), unichr(0xdc00), unichr(0xdfff),
                      unichr(0xd800), unichr(0xdbff), unichr(0xdc00), unichr(0xdfff),)
        if re.search(RE_ILLEGAL, line):
            return True
        if re.search(r"[\x01-\x1F\x7F]", line):
            return True
    return False


def update_metadata(request, meta_data):
    if not meta_data:
        return None
    meta_count = 0
    meta_size = 0
    for key, value in meta_data.iteritems():
        meta_count += 1
        meta_size += len(key) + len(value)
        if len(key) > MAX_META_NAME_LENGTH:
            return 'Metadata name too long; max %d' % MAX_META_NAME_LENGTH
        elif len(value) > MAX_META_VALUE_LENGTH:
            return 'Metadata value too long; max %d' % MAX_META_VALUE_LENGTH
        elif meta_count > MAX_META_COUNT:
            return 'Too many metadata items; max %d' % MAX_META_COUNT
        elif meta_size > MAX_META_OVERALL_SIZE:
            return 'Total metadata too large; max %d' % MAX_META_OVERALL_SIZE
        request.headers['x-object-meta-%s' % key] = value


# quotes commas as \x2c for [env] stanza in nvram file
# see ZRT docs
def quote_for_env(val):
    return re.sub(r',', '\\x2c', str(val))


def can_run_as_daemon(node_conf, daemon_conf):
    if node_conf.exe != daemon_conf.exe:
        return False
    if not node_conf.channels:
        return False
    if len(node_conf.channels) != len(daemon_conf.channels):
        return False
    if node_conf.connect or node_conf.bind:
        return False
    channels = sorted(node_conf.channels, key=lambda ch: ch.device)
    for n, d in zip(channels, daemon_conf.channels):
        if n.device not in d.device:
            return False
    return True


class ObjPath:

    def __init__(self, url, path):
        self.url = url
        self.path = path

    def __eq__(self, other):
        if self.url == other.url:
            return True
        return False

    def __ne__(self, other):
        if self.url != other.url:
            return True
        return False


class SwiftPath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('swift:/')
        ObjPath.__init__(self, url, path)
        (account, container, obj) = split_path(path, 1, 3, True)
        self.account = account
        self.container = container
        self.obj = obj

    @classmethod
    def init(cls, account, container, obj):
        return cls('swift://%s/%s/%s' % (account, container, obj))


class ImagePath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('file://')
        ObjPath.__init__(self, url, path)
        parts = path.split(':', 1)
        if len(parts) > 1:
            self.image = parts[0]
            self.path = parts[1]
        else:
            self.image = 'image'


class ZvmPath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('zvm://')
        ObjPath.__init__(self, url, path)
        (host, device) = path.split(':', 1)
        self.host = host
        if device.startswith('/dev/'):
            self.device = device
        else:
            self.device = '/dev/%s' % device


class CachePath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('cache:/')
        ObjPath.__init__(self, url, path)
        (etag, account, container, obj) = split_path(path, 1, 4, True)
        self.etag = etag
        self.account = account
        self.container = container
        self.obj = obj
        self.path = '/%s/%s/%s' % (account, container, obj)


class NetPath(ObjPath):

    def __init__(self, url):
        (proto, path) = url.split('://')
        ObjPath.__init__(self, url, '%s:%s' % (proto, path))


def parse_location(url):
    if not url:
        return None
    if url.startswith('swift://'):
        return SwiftPath(url)
    elif url.startswith('file://'):
        return ImagePath(url)
    elif url.startswith('zvm://'):
        return ZvmPath(url)
    elif url.startswith('cache://'):
        return CachePath(url)
    elif url.startswith('tcp://') or url.startswith('udp://'):
        return NetPath(url)
    return None


def is_swift_path(location):
    if isinstance(location, SwiftPath):
        return True
    return False


def is_zvm_path(location):
    if isinstance(location, ZvmPath):
        return True
    return False


def is_image_path(location):
    if isinstance(location, ImagePath):
        return True
    return False


def is_cache_path(location):
    if isinstance(location, CachePath):
        return True
    return False


class ZvmNode(object):
    def __init__(self, nid, name, nexe_path, args=None, env=None, replicate=1):
        self.id = nid
        self.name = name
        self.exe = nexe_path
        self.args = args
        self.env = env
        self.channels = []
        self.connect = []
        self.bind = []
        self.replicate = replicate
        self.replicas = []
        self.skip_validation = False

    def add_channel(self, device, access, path=None,
                    content_type='application/octet-stream',
                    meta_data=None, mode=None,
                    removable='no', mountpoint='/'):
        channel = ZvmChannel(device, access, path,
                             content_type, meta_data, mode,
                             removable, mountpoint)
        self.channels.append(channel)

    def get_channel(self, device=None, path=None):
        if device:
            for chan in self.channels:
                if chan.device == device:
                    return chan
        if path:
            for chan in self.channels:
                if chan.path == path:
                    return chan
        return None

    def resolve_wildcards(self, param):
        if param.count('*') > 0:
            for wc in getattr(self, 'wildcards', []):
                param = param.replace('*', wc, 1)
            if param.count('*') > 0:
                raise Exception('Cannot resolve wildcard for node %s' % self.name)
        return param

    def add_connection(self, bind_name, nodes, src_device=None, dst_device=None):
        if not dst_device:
            dst_device = '/dev/in/' + self.name
        else:
            dst_device = self.resolve_wildcards(dst_device)
        if nodes.get(bind_name):
            bind_node = nodes.get(bind_name)
            if bind_node is self:
                raise Exception('Cannot bind to itself: %s' % bind_name)
            bind_node.bind.append((self.name, dst_device))
            if not src_device:
                self.connect.append((bind_name, '/dev/out/' + bind_name))
            else:
                src_device = bind_node.resolve_wildcards(src_device)
                self.connect.append((bind_name, src_device))
        elif nodes.get(bind_name + '-1'):
            i = 1
            bind_node = nodes.get(bind_name + '-1')
            while bind_node:
                if not bind_node is self:
                    bind_node.bind.append((self.name, dst_device))
                    if not src_device:
                        self.connect.append((bind_name + '-' + str(i),
                                            '/dev/out/' + bind_name + '-' + str(i)))
                    else:
                        src_device = bind_node.resolve_wildcards(src_device)
                        self.connect.append((bind_name + '-' + str(i), src_device))
                i += 1
                bind_node = nodes.get(bind_name + '-' + str(i))
        else:
            raise Exception('Non-existing node in connect %s' % bind_name)

    def copy_cgi_env(self, request):
        if not self.env:
            self.env = {}
        self.env['HTTP_HOST'] = request.host
        self.env['REMOTE_ADDR'] = request.remote_addr
        self.env['REMOTE_USER'] = request.remote_user
        self.env['HTTP_USER_AGENT'] = request.user_agent
        self.env['QUERY_STRING'] = request.query_string
        self.env['SERVER_NAME'] = request.environ.get('SERVER_NAME', 'localhost')
        self.env['SERVER_PORT'] = request.environ.get('SERVER_PORT', '80')
        self.env['SERVER_PROTOCOL'] = request.environ.get('SERVER_PROTOCOL', 'HTTP/1.0')
        self.env['SERVER_SOFTWARE'] = 'zerocloud'
        self.env['GATEWAY_INTERFACE'] = 'CGI/1.1'
        self.env['SCRIPT_NAME'] = self.exe
        self.env['PATH_INFO'] = request.path_info
        self.env['REQUEST_METHOD'] = 'GET'
        self.env['HTTP_REFERER'] = request.referer
        self.env['HTTP_ACCEPT'] = request.headers.get('accept')
        self.env['HTTP_ACCEPT_ENCODING'] = request.headers.get('accept-encoding')
        self.env['HTTP_ACCEPT_LANGUAGE'] = request.headers.get('accept-language')

    def create_sysmap_resp(self):
        sysmap = json.dumps(self, cls=NodeEncoder)
        #print json.dumps(self, cls=NodeEncoder, indent=2)
        sysmap_iter = iter([sysmap])
        return Response(app_iter=sysmap_iter,
                        headers={'Content-Length': str(len(sysmap))})

    def add_data_source(self, data_sources, resp, dev='sysmap', append=False):
        if append:
            data_sources.append(resp)
        else:
            data_sources.insert(0, resp)
        if not getattr(self, 'last_data', None) or append:
            self.last_data = resp
        resp.nodes = [{'node': self, 'dev': dev}]


class ZvmChannel(object):
    def __init__(self, device, access, path=None,
                 content_type='application/octet-stream', meta_data=None,
                 mode=None, removable='no', mountpoint='/'):
        self.device = device
        self.access = access
        self.path = path
        self.content_type = content_type
        self.meta = meta_data if meta_data else {}
        self.mode = mode
        self.removable = removable
        self.mountpoint = mountpoint


class NodeEncoder(json.JSONEncoder):

    def default(self, o):
        if isinstance(o, ZvmNode) or isinstance(o, ZvmChannel):
            return o.__dict__
        elif isinstance(o, Response):
            return str(o.__dict__)
        if isinstance(o, ObjPath):
            return o.url
        return json.JSONEncoder.default(self, o)

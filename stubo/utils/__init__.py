"""
    stubo.utils
    ~~~~~~~~~~~
    
    Utilities
     
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import os
import ConfigParser
import logging
from datetime import date, datetime
import json
import time
from textwrap import dedent
import contextlib
import shutil
from tempfile import mkdtemp
from asyncore import compact_traceback
from StringIO import StringIO
from importlib import import_module
import hashlib

from pytz import timezone
import redis
from tornado.template import Template

from requests.utils import get_encoding_from_headers
from requests.structures import CaseInsensitiveDict
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import HtmlFormatter

from dogpile.cache import make_region

from stubo.scripts import get_default_config

log = logging.getLogger(__name__)

_UTC = timezone('UTC')

truthy = frozenset(('t', 'true', 'y', 'yes', 'on', '1'))

def asbool(s):
    """ Return the boolean value ``True`` if the case-lowered value of string
    input ``s`` is any of ``t``, ``true``, ``y``, ``on``, or ``1``, otherwise
    return the boolean value ``False``.  If ``s`` is the value ``None``,
    return ``False``.  If ``s`` is already one of the boolean values ``True``
    or ``False``, return it."""
    if s is None:
        return False
    if isinstance(s, bool):
        return s
    s = str(s).strip()
    return s.lower() in truthy

def run_template(templ, **kwargs):
    log.debug(u"run_template-> {0}".format(kwargs))
    t = Template(templ)
    return t.generate(**kwargs)

def check_config_path(fpath):
    if not os.path.isfile(fpath):
        _cwd = os.getcwd()
        raise ValueError('No config file found at path: {0} from {1}'.format(
            fpath, _cwd))

def read_config(config_file_path=None, section=None):
    config_file_path = config_file_path or get_default_config()
    check_config_path(config_file_path)
    config = ConfigParser.ConfigParser()
    log.info('reading config from file: {0}'.format(config_file_path))
    
    with open(config_file_path, 'r') as config_file:
        content = config_file.read()
        content = run_template(content, getenv=lambda x : os.environ.get(x, ''))
        
    config.readfp(StringIO(content))
    section = section or 'DEFAULT'
    return dict(config.items(section))


def setup_redis(host='localhost', port=6379, db=0, password=None):
    return redis.Redis(host, port, db, password)

def init_redis(settings):
    host = settings.get('redis.host', '127.0.0.1')
    port = int(settings.get('redis.port', 6379))
    db = int(settings.get('redis.db', 0))
    passwd = settings.get('redis.password')            
    import stubo.cache.queue
    stubo.cache.queue.redis_server = setup_redis(host, port, db, passwd)
    return stubo.cache.queue.redis_server

def init_redis_main(settings):
    host = settings.get('redis_main.host', '127.0.0.1')
    port =  int(settings.get('redis_main.port', 6379))
    db =  int(settings.get('redis_main.db', 0))
    passwd = settings.get('redis_main.password')            
    import stubo.cache.queue
    stubo.cache.queue.redis_main_server = setup_redis(host, port, db, passwd)
    return stubo.cache.queue.redis_main_server

def start_redis(cfg):
    redis_local = (cfg.get('redis.host', '127.0.0.1'),
                   int(cfg.get('redis.port', 6379)),
                   int(cfg.get('redis.db', 0)))
    redis_main = (cfg.get('redis_main.host', '127.0.0.1'),
                    int(cfg.get('redis_main.port', 6379)),
                    int(cfg.get('redis_main.db', 0)))  
    retry_count = int(cfg.get('retry_count', 10)) 
    retry_interval = int(cfg.get('retry_interval', 10))
    redis_local_server = init_redis(cfg)
    if redis_local != redis_main:              
        log.info('connecting to redis main')
        for i in range(retry_count):
            # retry as the main may have started after the subordinate  
            try:         
                redis_main_server = init_redis_main(cfg)
                break
            except:
                log.warn('redis_main not available, try again in {0} '
                         'secs'.format(retry_interval))
                time.sleep(retry_interval)    
    else:
        import stubo.cache.queue
        redis_main_server = redis_local_server
        stubo.cache.queue.redis_main_server = redis_main_server
    return redis_local_server, redis_main_server

def init_ext_cache(settings):
    # set ext_db 1 higher than main 
    ext_db = int(settings.get('redis_main.db', 0)) + 1
    passwd = settings.get('redis_main.password') 
    region = make_region('ext_cache').configure(
        'dogpile.cache.redis',
        arguments = {
            'host': settings.get('redis_main.host', '127.0.0.1'),
            'port': int(settings.get('redis_main.port', 6379)),
            'db': ext_db,
            'redis_expiration_time': 60*60*48,   # 48 hours
            'distributed_lock':True
            }
    )
    return region
            
def init_mongo(dbenv=None):
    import stubo.model.db
    dbenv = dbenv or stubo.model.db.default_env   
    stubo.model.db.mongo_client = stubo.model.db.get_connection(dbenv)
    return stubo.model.db.mongo_client
    
def tsecs_to_date(tsecs):
    update_time = datetime.fromtimestamp(tsecs)
    return _UTC.localize(update_time)   

def get_tsecs():
    t = datetime.utcnow()
    return time.mktime(t.timetuple()) 

def as_date(date_str):
    return date(*(int(x) for x in date_str.split('-')))

def convert_to_script(data, var_name='_client_data'):
    if data:
        result = json.dumps(data)
        script = dedent("""\
            <script type="text/javascript">
            window.%s = %s;
            </script>""" % (var_name, result))
    else:
        # no data, no script.
        script = ''
    return script
    
@contextlib.contextmanager
def make_temp_dir(dirname=None):
    temp_dir = mkdtemp(dir=dirname)
    try:
        yield temp_dir
    finally:    
        shutil.rmtree(temp_dir)
        
def get_export_links(request, scenario_name_key, files):
    # the export uses the local file system so the link needs to refer to the
    # server that has performed the export.  
    local_server = 'http://{0}:{1}'.format(request.track.server, 
                                           request.track.port or 8001)
    return [(x[0], local_server + request.static_url(os.path.join(
        'exports', scenario_name_key.replace(':', '_'), x[0]))) for x in files]
    
def get_hostname(request):
    return request.host.partition(':')[0].lower()    

def get_graphite_datapoints(json_result, target):
    return [d['datapoints'] for d in json_result if d['target'] == target][0]
 
def get_graphite_stats(server, auth, target, from_str='-5min', to_str='now', **kwargs):
    from stubo.model.cmds import UrlFetch 
    params = {'format': 'json',
              'target': target,
              'from': from_str,
              'until': to_str}
    params.update(kwargs)
    return UrlFetch().get('%s/render' % server, auth=auth, params=params, 
                          verify=False)

def resolve_class(path_name):
    """ resolves a :term:`dotted Python name` to a module object.
    """
    parts = path_name.split('.')
    module_name = ".".join(parts[0:-1])
    cls_name = parts[-1]
    module = import_module(module_name)
    cls = getattr(module, cls_name)
    return cls()

    
def human_size(size_bytes):
    """
    format a size in bytes into a 'human' file size (now only KB)
    the unit is NOT output anymore
    e.g. 0.0009 KB, 1 KB, 1024 KB, 1048576 KB
    """
    num = float(size_bytes)
    formatted_size = num/1024
    if num < 1024 :
        formatted_size = round(formatted_size, 3)
    else:
        formatted_size = int(round(formatted_size, 0))
	formatted_size = "{:,}".format(formatted_size)
    return "%s" % (formatted_size)  

def pretty_format(text, name=None):
    name = name or 'XML'
    return highlight(text, get_lexer_by_name(name), 
                     HtmlFormatter(linenos='table')) 

def pretty_format_python(text):
    return pretty_format(text, 'PYTHON')

def get_unicode_from_request(r):
    """Returns the requested content back in unicode.

    :param r: Request object to get unicode content from.

    Try:
    1. charset from content-type
    2. fall back and assume utf-8
    3. latin-1 replacing unicode chars 
    """
    if isinstance(r.body, unicode):
        return r.body

    tried_encodings = []

    # Try charset from content-type
    # i.e. "Content-type: text/plain; charset=us-ascii"
    encoding = get_encoding_from_headers(CaseInsensitiveDict(r.headers))

    if encoding:
        try:
            return unicode(r.body, encoding)
        except UnicodeError:
            tried_encodings.append(encoding)
    
    # workaround if encoding is not specified, assume utf-8, then latin-1                 
    try:
        return unicode(r.body, 'utf-8')
    except:
        tried_encodings.append('utf-8')
        return unicode(r.body, 'latin-1', errors='replace')

def compact_traceback_info(tb):
    tbinfo = []
    while tb:
        tbinfo.append((
            tb.tb_frame.f_code.co_filename,
            tb.tb_frame.f_code.co_name,
            str(tb.tb_lineno)
            ))
        tb = tb.tb_next
     
    # just to be safe
    del tb
     
    # file, function, line = tbinfo[-1]
    return ' '.join(['[%s|%s|%s]' % x for x in tbinfo])

def compute_hash(data):
    if isinstance(data, unicode):
        _hash = hashlib.sha224(data.encode('utf-8')).hexdigest()
    else:
        _hash = hashlib.sha224(data).hexdigest()
    return _hash

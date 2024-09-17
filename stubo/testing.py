"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import os
from tempfile import mkdtemp
from tornado.util import ObjectDict
from tornado.testing import AsyncHTTPTestCase
from tornado.web import MissingArgumentError
import time

DummyModel = ObjectDict


def testdb_name():
    import uuid

    dbname = str(uuid.uuid4())
    return 'test_{0}'.format(dbname)


class Base(AsyncHTTPTestCase):
    def __init__(self, *args, **kwargs):
        self.cfg = kwargs.pop('cfg', {})
        super(Base, self).__init__(*args, **kwargs)

    def setUp(self):
        super(Base, self).setUp()
        # the default settting of 5 secs is not enough for my old mac
        os.environ['ASYNC_TEST_TIMEOUT'] = '50'

    def tearDown(self):
        from stubo.utils import setup_redis

        self.redis_server.flushdb()
        cache_db = setup_redis(db=9 + 1)
        cache_db.flushdb()
        self.db.connection.drop_database(self.testdb)
        # closing connections
        self.db.connection.close()
        self.mdb.connection.close()

        self.app.settings['process_executor'].shutdown()
        super(Base, self).tearDown()

    def get_app(self):
        from tornado.ioloop import IOLoop
        from stubo.service.run_stubo import TornadoManager
        from stubo.utils import init_mongo, start_redis, init_ext_cache
        import motor

        self.testdb = testdb_name()

        self.cfg.update({
            'redis.host': '127.0.0.1',
            'redis.port': 6379,
            'redis.db': 9,
            'redis_main.host': '127.0.0.1',
            'redis_main.port': 6379,
            'redis_main.db': 9,
            'request_cache_limit': 10,
        })

        self.db = init_mongo({
            'tz_aware': True,
            'db': self.testdb
        })
        args = {'capped': True, 'size': 100000}
        self.db.create_collection("tracker", **args)
        self.db.tracker.create_index('start_time', -1)

        # add motor driver
        client = motor.MotorClient()
        self.mdb = client[self.testdb]
        self.cfg.update({'mdb': self.mdb})

        # install() asserts that its not been initialised so setting it directly
        # self.io_loop.install()
        IOLoop._instance = self.io_loop
        tm = TornadoManager(os.environ.get('STUBO_CONFIG_FILE_PATH'))
        self.redis_server, _ = start_redis(self.cfg)
        tm.cfg['ext_cache'] = init_ext_cache(self.cfg)
        tm.cfg['mongo.db'] = self.testdb
        tm.cfg.update(self.cfg)
        app = tm.get_app()
        self.app = app
        from concurrent.futures import ProcessPoolExecutor

        self.app.settings['process_executor'] = ProcessPoolExecutor()
        return app


class DummyRequestHandler(object):
    application_url = 'http://example.com'
    host = 'example.com:80'
    _ARG_DEFAULT = []

    def __init__(self, request=None, **kwargs):
        super(DummyRequestHandler, self).__init__()

        tmp_static_dir = mkdtemp()
        self.application = DummyModel(application_url=self.application_url,
                                      host=self.host,
                                      static_path=tmp_static_dir)
        self.application.update(**kwargs)
        self.request = request or DummyModel(protocol='http',
                                             host='localhost:8001',
                                             path='/',
                                             headers={},
                                             arguments=kwargs,
                                             body='')
        host, port = self.request.host.split(':')
        self.track = DummyModel(host=host, server=host, port=port,
                                tracking_level='normal',
                                request_params=kwargs)

    def initialize(self):
        pass

    def prepare(self):
        """Called at the beginning of a request before `get`/`post`/etc."""
        self._start_time = time.time()

    def on_finish(self):
        """Called after the end of a request."""
        self._finish_time = time.time()

    def get_argument(self, name, default=_ARG_DEFAULT, strip=True):
        args = self.get_arguments(name, strip=strip)
        if not args:
            if default is self._ARG_DEFAULT:
                raise MissingArgumentError(name)
            return default
        return args[-1]

    def get_arguments(self, name, strip=True):
        """Returns a list of the arguments with the given name.

        If the argument is not present, returns an empty list.

        The returned values are always unicode.
        """

        values = []
        for v in self.request.arguments.get(name, []):
            """"v = self.decode_argument(v, name=name)
            if isinstance(v, unicode_type):
                # Get rid of any weird control chars (unless decoding gave
                # us bytes, in which case leave it alone)
                v = RequestHandler._remove_control_chars_regex.sub(" ", v)"""
            if strip:
                v = v.strip()
            values.append(v)
        return values

    @property
    def settings(self):
        """ settings are stored directly in application."""
        return self.application

    def static_url(self, path):
        return os.path.join('/static', path)

    def request_time(self):
        """Returns the amount of time it took for this request to execute."""
        if self._finish_time is None:
            return time.time() - self._start_time
        else:
            return self._finish_time - self._start_time


class DummyHash(object):
    def __init__(self, keys=None):
        self._keys = keys or {}

    def __call__(self, *args):
        return self

    def keys(self, name):
        return self._keys.get(name, {}).keys()

    def remove(self, name):
        return self._keys.pop(name, None)

    def exists(self, key, name):
        item = self._keys.get(key)
        if not item:
            return False
        return name in item

    def delete(self, name, keys):
        item = self._keys.get(name)
        deleted = 0
        if item:
            if isinstance(keys, basestring):
                del item[keys]
                deleted += 1
            else:
                for key in keys:
                    del item[key]
                    deleted += 1
            self._keys[name] = item
        return deleted

    def get_raw(self, name, key):
        value = self._keys.get(name, {}).get(key)
        return value

    def get(self, name, key):
        import json

        try:
            return json.loads(self.get_raw(name, key))
        except TypeError:
            return None

    def get_all_raw(self, name):
        return self._keys.get(name, {})

    def get_all(self, name):
        import json

        result = self.get_all_raw(name)
        if result:
            result = dict((k, json.loads(v)) for k, v in result.iteritems())
        return result

    def values(self, name):
        import json

        result = self.get_all_raw(name)
        if result:
            result = [json.loads(v) for v in result.itervalues()]
        return result

    def set_raw(self, name, key, value):
        if name not in self._keys:
            self._keys[name] = {}
        self._keys[name][key] = value

    def set(self, name, key, value):
        import json

        self.set_raw(name, key, json.dumps(value))

    def incr(self, name, key, amount=1):
        val = self.get(name, key)
        if val:
            val += amount
        else:
            val = amount
        self.set(name, key, val)
        return val


from stubo.cache import Cache
from stubo.model.db import Scenario
import ming

mg = ming.create_datastore('mim://')


class DummyScenario(Scenario):
    def __init__(self):
        dbname = testdb_name()
        client = getattr(mg.conn, dbname)
        Scenario.__init__(self, db=client)

    def __call__(self, **kwargs):
        return self

        # derive these methods as ming hasn't implemented '$regex' matching

    def get_all(self, name=None):
        if name and isinstance(name, dict) and '$regex' in name:
            # {'$regex': 'localhost:.*'}
            name = name['$regex'].partition(':')[0]
            all_names = list(Scenario.get_all(self))
            return [self.get(x.get('name')) for x in all_names if name in x.get('name')]
        else:
            return Scenario.get_all(self, name=name)

    def get_stubs(self, name=None):
        if name and isinstance(name, dict) and '$regex' in name:
            name = name['$regex'].partition(':')[0]
            all_names = list(self.db.scenario_stub.find({}))
            result = [x for x in all_names if name in x.get('scenario')]
        else:
            result = Scenario.get_stubs(self, name=name)
        return result


class DummyCache(Cache):
    def __init__(self, host):
        Cache.__init__(self, host)
        self._hash = DummyHash()

    def __call__(self, host):
        self.host = host
        return self

    def hash_cls(self):
        return self._hash

    def get_all_saved_request_index_data(self):
        return {}


from collections import defaultdict


class DummyQueue(object):
    _data = defaultdict(list)

    def __init__(self, name, server=None):
        self.name = name

    def _get_list(self):
        return self._data[self.name]

    def get(self, timeout=None):
        return self._get_list().pop()

    def get_item(self, index):
        return self._get_list()[index] if self._get_list() else None

    def put(self, msg):
        self._get_list().append(msg)

    def delete(self):
        self._data.pop(self.name, 0)

    def __len__(self):
        return len(self._get_list())


class DummyTracker(object):
    def __init__(self, db=None):
        # db should be similar to a multidict
        self.db = db or defaultdict(list)

    def __call__(self, **kwargs):
        return self

    def insert(self, track):
        _id = track.get('_id')
        if not _id:
            import time

            _id = int(time.time())
        self.db[_id] = track

    def find_tracker_data(self, tracker_filter, skip, limit):
        self.db['_filter'] = tracker_filter
        self.db['_skip'] = skip
        self.db['_limit'] = limit
        return self.db

    def find_tracker_data_full(self, _id):
        return self.db.get(_id)

    def session_last_used(self, scenario, session, mode):
        ''' Return the date this session was last used using the 
            last get/response time.
        '''
        import datetime

        return dict(start_time=datetime.datetime.now(),
                    remote_ip='::1')

    def get_last_playback(self, scenario, session, start_time):
        return self.db.values()

    get_last_recording = get_last_playback


def make_stub(matchers, response, delay_policy=None, module=None,
              recorded=None):
    request = {
        "method": "POST",
        "bodyPatterns": {"contains": matchers}
    }
    response = {
        "status": 200,
        "body": response
    }
    if delay_policy:
        response['delayPolicy'] = delay_policy
    payload = {
        "recorded": recorded or '2014-05-20',
        "request": request,
        "response": response,
    }
    if module:
        payload['module'] = module
    return payload


def make_cache_stub(matchers, response_ids, delay_policy=None, module=None,
                    recorded=None):
    stub = make_stub(matchers, response_ids, delay_policy=delay_policy,
                     module=module, recorded=recorded)
    stub['response'].pop('body')
    stub['response']['ids'] = response_ids
    return stub

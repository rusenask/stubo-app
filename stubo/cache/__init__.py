"""
    stubo.cache
    ~~~~~~~~~~~
    
    Redis Caching
     
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import logging
import datetime
import time

from .queue import String, Hash, Queue, get_redis_main, get_redis_subordinate
from stubo.exceptions import exception_response
from stubo.utils import asbool
from stubo.model.db import Scenario
from stubo.model.stub import Stub, StubCache, response_hash

log = logging.getLogger(__name__)

"""
Redis is used for caching
Keys are replicated from main to subordinate redis instances in distributed envs

Data Structures Used

(Hash)
name                            key->value (json)
host:delay_policy               name->delay_policy_data 

(Hash)
name                            key->value (raw)
host:sessions                   session_name->scenario_name 

(Hash)
name                            key->value (json)
host:scenario_name              session_name -> session_data 

e.g.

>>> print json.dumps(cache.get_session('first', 'first_1'), indent=4)
{
    "status": "dormant", 
    "system_date": "2014-11-21", 
    "scenario": "localhost:first", 
    "last_used": "2014-11-21 11:54:27", 
    "scenario_id": "546f27c681875907691e30f6", 
    "session": "first_1"
} 

begin/session?scenario=first&session=first_1&mode=playback
>>> print json.dumps(cache.get_session('first', 'first_1'), indent=4)
{
    "status": "playback", 
    "system_date": "2014-11-25", 
    "scenario": "localhost:first", 
    "last_used": "2014-11-25 15:20:08", 
    "stubs": [
        {
            "recorded": "2014-11-21", 
            "args": {
                "session": "first_1"
            }, 
            "request": {
                "bodyPatterns": [
                    {
                        "contains": [
                            "get my stub\n"
                        ]
                    }
                ], 
                "method": "POST"
            }, 
            "response": {
                "status": 200, 
                "ids": [
                    "1a90f47bb0af291264a6c06868b97cd62b372d41de26c3fd21cef61b"
                ]
            }
        }
    ], 
    "scenario_id": "546f27c681875907691e30f6", 
    "session": "first_1"
}


(Hash)
name                            key->value (json)
host:scenario_name:response     session_name:response_id->response_text 

redis 127.0.0.1:6379> hgetall "localhost:first:response"
1) "first_1:1a90f47bb0af291264a6c06868b97cd62b372d41de26c3fd21cef61b"
2) "\"Hello {{1+1}} World\\n\""

(Hash)
name                            key->value (json)
host:scenario_name:request      session_name:request_id->[[response_ids], delay_policy_name, recorded, system_date,
                                module_info, request_index_key]

Note this key is only populated after get/response unless begin/session?warm_cache=true is supplied

redis 127.0.0.1:6379> hgetall "localhost:first:request"
1) "first_1:4c95b66e52ecae4009dccdb95e2f30975031553649a29f1a62326958"
2) "[[\"1a90f47bb0af291264a6c06868b97cd62b372d41de26c3fd21cef61b\"], \"\", \"2014-11-21\", \"2014-11-25\", {},
      \"84dab03cd9cbf56782635f95ef74efc641d993e768b79b4344452b45\"]"

(Hash)
name                               key->value (raw)
host:scenario_name:request_index   session_name:request_index_key->index

(Hash)
name                                     key->value (json)
host:scenario_name:saved_request_index   name-> {request_index_key : index}
"""


class Cache(object):
    """Most keys in the cache are scoped by host. This class encapsulates the
    key lookup via host and redis IO.
    """

    def __init__(self, host):
        self.host = host

    def hash_cls(self):
        # testing
        return Hash

    def get(self, name, key, local=False):
        return self.hash_cls()(get_redis_server(local)).get(name, key)

    def set(self, name, key, value, local=False):
        return self.hash_cls()(get_redis_server(local)).set(name, key, value)

    def set_raw(self, name, key, value, local=False):
        return self.hash_cls()(get_redis_server(local)).set_raw(name, key, value)

    def exists(self, name, key, local=False):
        return self.hash_cls()(get_redis_server(local)).exists(name, key)

    def get_scenario_key(self, session_name):
        """Lookup the scenario from => host:sessions
           returns 'host:scenario_name' or  None if not found
        """
        log.debug("get_scenario_key: host={0}, session_name={1}".format(
            self.host, session_name))
        key = '{0}:sessions'.format(self.host)
        scenario = self.hash_cls()(get_redis_subordinate()).get_raw(key, session_name)
        if not scenario:
            return None
        return '{0}:{1}'.format(self.host, scenario)

    def find_scenario_key(self, session_name):
        scenario_key = self.get_scenario_key(session_name)
        if not scenario_key:
            raise exception_response(400,
                                     title='session not found - {0}:{1}'.format(self.host,
                                                                                session_name))
        return scenario_key

    def scenario_key_name(self, scenario_name):
        return '{0}:{1}'.format(self.host, scenario_name)

    def set_session(self, scenario_name, session_name, session_payload):
        return self.set(self.scenario_key_name(scenario_name), session_name,
                        session_payload)

    def set_session_map(self, scenario_name, session_name):
        return self.set_raw(self.get_sessions_map_key(), session_name,
                            scenario_name)

    def get_active_sessions(self, scenario_name, local=True):
        return self.get_sessions_status(scenario_name,
                                        status=('record', 'playback'),
                                        local=local)

    def get_sessions(self, scenario_name, local=True):
        key = self.scenario_key_name(scenario_name)
        sessions = self.hash_cls()(get_redis_server(local)).get_all(key)
        for session_name, session_data in sessions.iteritems():
            yield session_name, session_data

    def get_scenario_sessions_information(self, scenario_name, local=True):
        """
        Returns a generator for session information for specified scenario.
        Example output:
        {
            status: "playback"
            system_date: "2015-07-20"
            name: "playback_100"
            last_used: "2015-07-20 13:09:16"
        }
        :param scenario_name: <string>
        :param local: <boolean>
        """
        key = self.scenario_key_name(scenario_name)
        sessions = self.hash_cls()(get_redis_server(local)).get_all(key)
        for session_name, session_data in sessions.iteritems():
            session_info = {
                'name': session_name,
                'status': session_data.get('status', None),
                'loaded': session_data.get('system_date', None),
                'last_used': session_data.get('last_used', None)
            }
            yield session_info

    def get_all_saved_request_index_data(self):
        main = get_redis_main()
        keys = main.keys('{0}:*:saved_request_index'.format(self.host))
        info = {}
        for key in keys:
            scenario_name = key.split(':')[1]
            info[scenario_name] = self.hash_cls()(main).get_all(key)
        return info

    def get_sessions_status(self, scenario_name, status=None, local=True):
        status = status or ('dormant', 'record', 'playback')
        sessions = [(x[1]['session'], x[1]['status']) for x in self.get_sessions(scenario_name,
                                                                                 local=local)
                    if x[1]['status'] in status]
        return sessions

    def delete_caches(self, scenario_name):
        key = self.scenario_key_name(scenario_name)
        main = get_redis_main()
        deleted_responses = self.hash_cls()(main).remove(self.get_response_key(
            scenario_name))
        deleted_requests = self.hash_cls()(main).remove(self.get_request_key(
            scenario_name))

        # delete request indexes
        deleted_request_indexes = []
        for k in (self.get_request_index_key(scenario_name),
                  self.get_saved_request_index_key(scenario_name)):
            deleted_request_indexes.append(self.hash_cls()(main).remove(k))

        session_names = self.hash_cls()(main).keys(key)
        # log.debug('session_names: {0}'.format(session_names))
        deleted_sessions_map = 0
        if session_names:
            sessions_key = '{0}:sessions'.format(self.host)
            for k in session_names:
                deleted_sessions_map += self.hash_cls()(main).delete(sessions_key, k)
        deleted_sessions = self.hash_cls()(main).remove(key)
        log.debug('deleted_response: {0}, deleted_requests: {1}, '
                  ', deleted_sessions_map: {2}, deleted_sessions: {3}, '
                  'deleted_request_indexes: {4}'.format(deleted_responses,
                                                        deleted_requests, deleted_sessions_map, deleted_sessions,
                                                        deleted_request_indexes))

    def get_sessions_map_key(self):
        return '{0}:sessions'.format(self.host)

    def get_delay_policy_key(self):
        return '{0}:delay_policy'.format(self.host)

    def get_delay_policy(self, name, local=True):
        key = self.get_delay_policy_key()
        if name:
            return self.hash_cls()(get_redis_server(local)).get(key, name)
        else:
            return self.hash_cls()(get_redis_server(local)).get_all(key)

    def set_delay_policy(self, name, data):
        key = self.get_delay_policy_key()
        return self.hash_cls()(get_redis_main()).set(key, name, data)

    def delete_delay_policy(self, names):
        num_deleted = 0
        key = self.get_delay_policy_key()
        if names:
            for name in names:
                num_deleted += self.hash_cls()(get_redis_server(local=False)).delete(key, name)
        else:
            num_deleted = self.hash_cls()(get_redis_server(local=False)).remove(key)
        return num_deleted

    def key_name(self, scenario_name, key):
        return '{0}:{1}'.format(self.scenario_key_name(scenario_name), key)

    def get_response_key(self, scenario_name):
        return self.key_name(scenario_name, "response")

    def get_request_key(self, scenario_name):
        return self.key_name(scenario_name, "request")

    def get_request_index_key(self, scenario_name):
        return self.key_name(scenario_name, "request_index")

    def get_saved_request_index_key(self, scenario_name):
        return self.key_name(scenario_name, "saved_request_index")

    def get_request_index_data(self, scenario_name):
        main = get_redis_main()
        return self.hash_cls()(main).get_all_raw(self.get_request_index_key(
            scenario_name))

    def reset_request_index(self, scenario_name):
        for k in self.get_request_index_data(scenario_name).iterkeys():
            self.set_request_index_item(scenario_name, k, 0)

    def set_saved_request_index_data(self, scenario_name, name, data):
        return self.set(self.get_saved_request_index_key(scenario_name), name,
                        data)

    def delete_saved_request_index(self, scenario_name, name):
        main = get_redis_main()
        return self.hash_cls()(main).delete(self.get_saved_request_index_key(
            scenario_name), name)

    def get_saved_request_index_data(self, scenario_name, name):
        return self.get(self.get_saved_request_index_key(scenario_name), name)

    def set_request_index_item(self, scenario_name, name, value):
        return self.set_raw(self.get_request_index_key(scenario_name), name,
                            value)

    def request_index_item_exists(self, scenario_name, name):
        return self.exists(self.get_request_index_key(scenario_name), name)

    def request_index_exists(self, scenario_name):
        return key_exists(self.get_request_index_key(scenario_name))

    def delete_session_data(self, scenario_name, session):
        main = get_redis_main()
        keys = (self.get_request_key(scenario_name),
                self.get_response_key(scenario_name),
                self.get_request_index_key(scenario_name))
        hashes = [x.format(self.scenario_key_name(scenario_name)) for x in keys]
        for _hash in hashes:
            keys = self.hash_cls()(main).keys(_hash)
            session_keys = [x for x in keys if x.startswith('{0}:'.format(
                session))]
            if session_keys:
                log.debug('deleting {0} from {1}'.format(session_keys, _hash))
                num_deleted = 0
                for k in session_keys:
                    num_deleted += self.hash_cls()(main).delete(_hash, k)
                log.debug('deleted {0}'.format(num_deleted))

    def assert_valid_session(self, scenario_name, session_name):
        scenario_key = self.scenario_key_name(scenario_name)
        # if session exists it can only be dormant
        if self.exists(scenario_key, session_name):
            session = self.get_session(scenario_name, session_name, local=False)
            session_status = session['status']
            if session_status != 'dormant':
                raise exception_response(400, title='Session already exists '
                                                    '- {0}:{1} in {2} mode.'.format(scenario_key, session_name,
                                                                                    session_status))

                # Limitation: as the session name is not passed in as an arg to get/response
        # we need a mapping to find the session for a scenario. 
        # host:sessions -> session_name->scenario_name
        # we can therefore only have one session name per host/scenario
        sessions_key = '{0}:sessions'.format(self.host)
        scenario_found = self.hash_cls()(get_redis_main()).get_raw(sessions_key,
                                                                     session_name)
        if scenario_found and scenario_found != scenario_name:
            raise exception_response(400, title='Session {0} can not be '
                                                'used for scenario: {1}. This session is already being used '
                                                'with another scenario: {2} on host: {3}.'.format(session_name,
                                                                                                  scenario_name,
                                                                                                  scenario_found,
                                                                                                  self.host))

    def set_response(self, scenario, session_name, response_id, val):
        response_key = '{0}:{1}'.format(session_name, response_id)
        self.set(self.get_response_key(scenario), response_key, val)

    def get_request(self, scenario_name, session_name, request_id, local=True):
        """
        Look up cached requests
        if the request is found returns
            response_ids, delay_policy_name, recorded, system_date, module_info,
            request_index_key
        else
            None
        """
        log.debug('get_request: {0}:{1} {2} {3}'.format(self.host,
                                                        scenario_name, session_name, request_id))
        request_key = '{0}:{1}'.format(session_name, request_id)
        return self.get(self.get_request_key(scenario_name), request_key, local)

    def get_response(self, scenario_name, session_name, response_ids,
                     request_index_key):
        """
        returns response or None
        """
        num_responses = len(response_ids)
        index = 0
        if num_responses > 1:
            # stateful response: lookup the response index value stored on main
            main = get_redis_main()
            request_index_name = self.get_request_index_key(scenario_name)
            request_index_key = '{0}:{1}'.format(session_name, request_index_key)
            index = self.get(request_index_name, request_index_key)
            if not index or index < num_responses:
                index = self.hash_cls()(main).incr(request_index_name, request_index_key)
            index -= 1
        response_key = '{0}:{1}'.format(session_name, response_ids[index])
        return self.get(self.get_response_key(scenario_name), response_key,
                        local=True)

    def get_session(self, scenario_name, session_name, local=True):
        return self.get(self.scenario_key_name(scenario_name), session_name,
                        local=local) or {}

    def get_session_with_delay(self, scenario_name, session_name, retry_count=5,
                               retry_interval=1):
        for i in range(retry_count):
            scenario_key = self.scenario_key_name(scenario_name)
            session = self.get_session(scenario_name, session_name)
            if not session:
                raise exception_response(500,
                                         title="session {0} not found!".format(session_name))
            if session['status'] == 'playback':
                # indicates session has been replicated
                break
            elif session['status'] == 'record':
                raise exception_response(409, title="session '{0}' for scenario"
                                                    "'{1}' in record mode, "
                                                    "playback expected ...".format(session_name, scenario_key))
            else:
                log.warn("subordinate session data not available! try again in {0} "
                         'secs'.format(retry_interval))
                time.sleep(retry_interval)
        return session, i

    def create_session_cache(self, scenario_name, session_name,
                             system_date=None):
        scenario_key = self.scenario_key_name(scenario_name)
        log.debug("create_session_cache: scenario_key={0}, session_name={1}".format(
            scenario_key, session_name))
        session = self.get(scenario_key, session_name)
        if not session:
            # must be using a different session name for playback than record
            session = {
                'session': session_name,
                'scenario': scenario_key
            }
            # add to sessions map
            self.set_raw('{0}:sessions'.format(self.host), session_name, scenario_name)

        session['status'] = 'playback'
        session['system_date'] = system_date or datetime.date.today().strftime(
            '%Y-%m-%d')
        session['last_used'] = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        cache_info = []

        # copy mongo scenario stubs to redis cache
        scenario_col = Scenario()
        stubs_cursor = scenario_col.get_stubs(scenario_key)
        stubs = list(stubs_cursor)
        if not stubs:
            raise exception_response(412,
                                     title="Precondition failed: no stubs were"
                                           " found in database for scenario: {0}".format(scenario_key))
        from stubo.ext.module import Module

        for scenario_stub in stubs:
            stub = Stub(scenario_stub['stub'], scenario_stub['scenario'])
            if stub.module():
                module_name = stub.module()['name']
                # tag this stub with the latest version of the module
                module = Module(self.host)
                version = module.latest_version(module_name)
                if not version:
                    raise exception_response(500,
                                             title="module '{0}' not found in cache".format(
                                                 module.key(module_name)))
                stub.module()['version'] = version

            response_ids = []
            response_bodys = stub.response_body()
            # cache each response id -> response (text, status) etc
            for response_text in response_bodys:
                stub.set_response_body(response_text)
                response_id = response_hash(response_text, stub)
                self.set_response(scenario_name, session_name, response_id,
                                  stub.response())
                response_ids.append(response_id)

                # replace response text with response hash ids for session cache
            stub.response().pop('body', None)
            stub.response()['ids'] = response_ids
            delay_policy_name = stub.delay_policy()
            if delay_policy_name:
                # Note: the delay policy is not really cached with the session.
                # The get/response call will just use the name to get the latest
                # delay value from the 'delay_policy' key in redis.
                delay_policy_key = '{0}:delay_policy'.format(self.host)
                delay_policy = self.get(delay_policy_key, delay_policy_name)
                if not delay_policy:
                    log.warn('unable to find delay_policy: {0}'.format(
                        delay_policy_name))
                stub.set_delay_policy(delay_policy)
            # _id = ObjectId(scenario_stub['_id'])
            # stub['recorded'] = str(_id.generation_time.date())
            cache_info.append(stub.payload)
        session['stubs'] = cache_info
        # log.debug('stubs: {0}'.format(session['stubs']))
        self.set(scenario_key, session_name, session)
        log.debug('created session cache: {0}:{1}'.format(session['scenario'],
                                                          session['session']))
        return session

    def set_stubo_setting(self, setting, value, all_hosts=False):
        key = 'stubo_setting'
        if not all_hosts:
            key = '{0}:{1}'.format(self.host, key)
        return self.hash_cls()(get_redis_main()).set(key, setting, value)

    def get_stubo_setting(self, setting=None, all_hosts=False):
        key = 'stubo_setting'
        if not all_hosts:
            key = '{0}:{1}'.format(self.host, key)
        if setting:
            result = self.hash_cls()(get_redis_subordinate()).get(key, setting)
        else:
            result = self.hash_cls()(get_redis_subordinate()).get_all(key)
        return result

    def blacklisted(self):
        return asbool(self.get_stubo_setting('blacklisted'))


def key_exists(key, local=False):
    return get_redis_server(local).exists(key)


def get_request_index_hash_key(session, stub_number):
    """
    Return the self.hash_cls() of the scenario_name+matchers
    """
    scenario_key = session['scenario']
    session_name = session['session']
    matching_stub = StubCache(session['stubs'][stub_number], scenario_key,
                              session_name)
    return matching_stub.request_index_id()


def add_request(session, request_id, stub, system_date, stub_number,
                request_cache_limit=10):
    scenario_key = session['scenario']
    session_name = session['session']
    host, scenario_name = scenario_key.split(':')
    cache = Cache(host)
    request_key = '{0}:{1}'.format(session_name, request_id)
    request_index_key = get_request_index_hash_key(session, stub_number)

    request_values = cache.hash_cls()(get_redis_subordinate()).values(cache.get_request_key(
        scenario_name))
    cached_requests = [x for x in request_values if stub.response_ids() == x[0]]
    if len(cached_requests) < request_cache_limit:
        # Note only cache the first of request_cache_limit requests that have 
        # the same response. Stops memory exhaustion if a test generates a 
        # unique request each time.
        result = cache.set(cache.get_request_key(scenario_name),
                           request_key,
                           (stub.response_ids(), stub.delay_policy_name(),
                            stub.recorded(), system_date, stub.module(),
                            request_index_key))
        log.debug('add_request: {0} {1} {2} {3} {4} stub_number={5} '
                  'request_index_key={6}, result={7}'.format(scenario_key,
                                                             session_name, request_id, stub.response_ids(),
                                                             stub.delay_policy_name(), stub_number, request_index_key,
                                                             result))
    return request_index_key


def get_keys(key_pattern, local=False):
    return get_redis_server(local=local).keys(key_pattern)


def get_redis_server(local=True):
    return get_redis_subordinate() if local else get_redis_main()

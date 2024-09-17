import unittest
import mock
from stubo.testing import (
    make_stub, make_cache_stub, DummyModel, DummyQueue, DummyHash
)

class Base(unittest.TestCase):
    def setUp(self):
        self.hash = DummyHash({})
        self.hash_patch = mock.patch('stubo.cache.Hash', self.hash)
        self.hash_patch.start()
        
        from stubo.testing import DummyScenario
        self.scenario = DummyScenario()
        self.db_patch = mock.patch('stubo.cache.Scenario', self.scenario)
        self.db_patch.start()
        
        self.patch_module = mock.patch('stubo.ext.module.Module', DummyModule)
        self.patch_module.start()
        
    def tearDown(self):
        self.hash_patch.stop()   
        self.db_patch.stop()
        self.patch_module.stop()
        
    def _get_cache(self):
        from stubo.cache import Cache
        return Cache('localhost')  
        
    def _make_scenario(self, name,  **kwargs):
        doc = dict(name=name, **kwargs)
        self.scenario.insert(**doc)  
    
class Test_create_session_cache(Base):
            
    def test_no_stubs(self):
        from stubo.exceptions import HTTPClientError
        with self.assertRaises(HTTPClientError):
            self._get_cache().create_session_cache('foo', 'bar')  
                                                                         
    def test_new_session(self):
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        cache = self._get_cache()
        cache.create_session_cache('foo', 'bar')  
        session = self.hash.get('localhost:foo', 'bar') 
        self.assertEqual(session["status"], "playback")
        self.assertEqual(session['session'], 'bar')
        self.assertEqual(session["scenario"], "localhost:foo")
        self.assertTrue('stubs' in session)
        stubs = session['stubs']
        self.assertEqual(len(stubs), 1)
        from stubo.model.stub import StubCache, response_hash
        stub = StubCache(stubs[0], 'localhost:foo', 'bar') 
        self.assertEqual(stub.contains_matchers(), ["<test>match this</test>"])
        self.assertEqual(stub.response_ids(), [response_hash('<test>OK</test>', stub)])        
        self.assertEqual(self.hash.get_raw('localhost:sessions', 'bar'), 'foo')
        
    def test_new_session_with_state(self):
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        
        stub2 = Stub(create('<test>match this</test>', '<test>BAD</test>'),
                    'localhost:foo')
        doc2 = dict(scenario='localhost:foo', stub=stub2)
        self.scenario.insert_stub(doc2, stateful=True)  
        
        cache = self._get_cache()
        cache.create_session_cache('foo', 'bar')  
        session = self.hash.get('localhost:foo', 'bar') 
        self.assertEqual(session["status"], "playback")
        self.assertEqual(session['session'], 'bar')
        self.assertEqual(session["scenario"], "localhost:foo")
        
        self.assertTrue('stubs' in session)
        stubs = session['stubs']
        self.assertEqual(len(stubs), 1)
        from stubo.model.stub import StubCache
        stub = StubCache(stubs[0], session["scenario"], session['session'])
        responses = stub.response_ids()
        self.assertEqual(len(responses), 2)
        self.assertEqual(stub.contains_matchers(), ["<test>match this</test>"])
        self.assertEqual(responses, [response_hash('<test>OK</test>', stub), 
                                     response_hash('<test>BAD</test>', stub2)])          
        self.assertEqual(self.hash.get_raw('localhost:sessions', 'bar'), 'foo')    
        
    def test_update_dormant_session_with_stubs(self):
        self.hash.set('somehost:foo', 'bar', {'status' : 'dormant',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'})
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        
        cache = self._get_cache()
        cache.create_session_cache('foo', 'bar')  
        session = self.hash.get('localhost:foo', 'bar') 
        self.assertEqual(session["status"], "playback")
        self.assertEqual(session['session'], 'bar')
        self.assertEqual(session["scenario"], "localhost:foo")
        
        self.assertTrue('stubs' in session)
        stubs = session['stubs']
        self.assertEqual(len(stubs), 1)
        from stubo.model.stub import StubCache
        stub = StubCache(stubs[0], session["scenario"], session['session']) 
        self.assertEqual(stub.contains_matchers(), ["<test>match this</test>"])
        self.assertEqual(stub.response_ids(), [response_hash('<test>OK</test>',
                                                             stub)])        
    
    def test_update_dormat_session_with_stubs_and_delay(self):
        self.hash.set('localhost:foo', 'bar', {'status' : 'dormant',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'})
        delay_policy = {"delay_type": "fixed", "name": "slow",
                        "milliseconds": "500"}                                     
        self.hash.set('localhost:delay_policy', 'slow', delay_policy)
        
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        stub.set_delay_policy('slow')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        
        self._get_cache().create_session_cache('foo', 'bar')  
        session = self.hash.get('localhost:foo', 'bar') 
        self.assertTrue('stubs' in session)
        stubs = session['stubs']
        self.assertEqual(len(stubs), 1)
        from stubo.model.stub import StubCache
        stub = StubCache(stubs[0], session["scenario"], session['session']) 
        self.assertEqual(stub.delay_policy(), delay_policy)
    
    def test_update_dormat_session_with_stubs_and_module(self):
        self.hash.set('localhost:foo', 'bar', {'status' : 'dormant',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'}) 
        module = {"system_date": "2013-09-24", "version": 1, "name": "funcky"}                                  
        
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        stub.set_module(module)
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        self._get_cache().create_session_cache('foo', 'bar')  
        session = self.hash.get('localhost:foo', 'bar') 
        self.assertTrue('stubs' in session)
        stubs = session['stubs']
        self.assertEqual(len(stubs), 1)
        from stubo.model.stub import StubCache
        stub = StubCache(stubs[0], session["scenario"], session['session'])
        self.assertEqual(stub.module(), module)    
            
            
class TestCache(unittest.TestCase):
    
    def setUp(self):
        self.hash = DummyHash()
        self.patch = mock.patch('stubo.cache.Hash', self.hash)
        self.patch.start()
        self.patch2 = mock.patch('stubo.cache.get_redis_server', lambda x: x)
        self.patch2.start()
        
    def tearDown(self):
        self.patch.stop() 
        self.patch2.stop() 
    
    def _get_cache(self):
        from stubo.cache import Cache
        return Cache('localhost')
    
    def test_get(self):
        cache = self._get_cache()
        response = cache.get('bogus', 'somekey')
        self.assertEqual(response, None)  
        
    def test_get_scenario_key_return_none_if_not_found(self):  
        self.assertTrue(self._get_cache().get_scenario_key('y') is None)
        
    def test_get_scenario_key(self): 
        self.hash.set_raw('localhost:sessions', 'bar', 'foo')
        self.assertEqual(self._get_cache().get_scenario_key('bar'), 
                         'localhost:foo')          
    
    def test_get_session(self):
        cache = self._get_cache()
        session = {u'status': u'playback', u'system_date': u'2013-10-31', u'session': u'joe', u'scenario_id': u'527287af31588e', u'scenario': u'localhost:conversation'}
        self.hash.set('localhost:conversation', 'joe', session) 
        response, retries = cache.get_session_with_delay(
            'conversation', 'joe') 
        self.assertEqual(response, session)
        self.assertEqual(retries, 0) 
                       
    def test_get_session_retry(self):
        cache = self._get_cache()
        session = {u'status': u'dormant', u'system_date': u'2013-10-31', u'session': u'joe', u'scenario_id': u'527287af31588e', u'scenario': u'localhost:conversation'}
        self.hash.set('localhost:conversation', 'joe', session) 
         
        response, retries = cache.get_session_with_delay('conversation', 'joe') 
        self.assertTrue(retries > 0) 
               
    def test_get_session_retry2(self):
        cache = self._get_cache()
        session = {u'status': u'dormant', u'system_date': u'2013-10-31', u'session': u'joe', u'scenario_id': u'527287af31588e', u'scenario': u'localhost:conversation'}
        self.hash.set('localhost:conversation', 'joe', session) 
        response, retries = cache.get_session_with_delay('conversation', 'joe',
                                                         retry_count=3, 
                                                         retry_interval=1) 
        self.assertEqual(retries, 2) 
               
    def test_get_session_in_record(self):
        from stubo.exceptions import HTTPClientError
        cache = self._get_cache()
        session = {u'status': u'record', u'system_date': u'2013-10-31', u'session': u'joe', u'scenario_id': u'527287af31588e', u'scenario': u'localhost:conversation'}
        self.hash.set('localhost:conversation', 'joe', session) 
        
        with self.assertRaises(HTTPClientError):
            cache.get_session_with_delay('conversation', 'joe')                          
            
    def test_get_bogus_session_raises(self):
        from stubo.exceptions import HTTPServerError
        cache = self._get_cache()
        with self.assertRaises(HTTPServerError):
            cache.get_session_with_delay('conversation', 'bogus')
            
    def test_delay_not_found(self):   
        self.assertEqual(self._get_cache().get_delay_policy('slow'), None)
        
    def test_get_delay_policy(self):  
        self.hash.set('localhost:delay_policy', 'slow', {"delay_type": "fixed", 
                      "name": "slow", "milliseconds": "500"}) 
        self.assertEqual(self._get_cache().get_delay_policy('slow'), 
                         {"delay_type": "fixed", "name": "slow", 
                          "milliseconds": "500"})  
        
    def test_delete_caches_empty(self):
        response = self._get_cache().delete_caches('foo') 
        self.assertTrue(1) 
        
    def test_delete_caches_one_session(self):
        self.hash.set('localhost:foo:request', 'bar:1', ["1", ""])
        self.hash.set_raw('localhost:foo:response', 'bar:1',
                          "Hello {{1+1}} World")
        self.hash.set('localhost:foo', 'bar', {'status' : 'dormant',
                                              'session' : 'bar',
                                              'scenario' : 'somehost:foo'})
        self.hash.set_raw('localhost:sessions', 'bar', 'foo')        
        self._get_cache().delete_caches('foo') 
        self.assertEqual(self.hash._keys, {'localhost:sessions': {}}) 
        
    def test_delete_caches_two_sessions(self):
        self.hash.set('localhost:foo:request', 'bar:1', ["1", ""])
        self.hash.set_raw('localhost:foo:response', 'bar:1', 
                          "Hello {{1+1}} World")
        self.hash.set('localhost:foo:request', 'bar2:1', ["1", ""])
        self.hash.set_raw('localhost:foo:response', 'bar2:1', 
                          "Hello {{1+1}} World")
        self.hash.set('localhost:foo', 'bar', {'status' : 'dormant',
                                              'session' : 'bar',
                                              'scenario' : 'somehost:foo'})
        self.hash.set('localhost:foo', 'bar2', {'status' : 'dormant',
                                              'session' : 'bar2',
                                              'scenario' : 'somehost:foo'})
        self.hash.set_raw('localhost:sessions', 'bar', 'foo')   
        self.hash.set_raw('localhost:sessions', 'bar2', 'foo')        
        self._get_cache().delete_caches('foo') 
        self.assertEqual(self.hash._keys, {'localhost:sessions': {}})  
        
    def _func(self, host, scenario_name, status, local=True):    
        from stubo.cache import get_sessions_status
        return get_sessions_status(host, scenario_name, status=status, 
                                   local=local)  
    
    def test_get_sessions_status_empty(self): 
        self.assertEqual(self._get_cache().get_sessions_status('foo', 
                         status=('record', 'playback')), []) 
        
    def test_get_sessions_status_zero_active(self):
        self.hash.set('localhost:foo', 'bar', {'status' : 'dormant',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'})
        self.assertEqual(self._get_cache().get_sessions_status('foo', 
                         status=('record', 'playback')), []) 
        
    def test_get_sessions_status_active(self):
        self.hash.set('localhost:foo', 'bar', {'status' : 'playback',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'})
        self.assertEqual(self._get_cache().get_sessions_status('foo', 
                         status=('record', 'playback')), [('bar', 'playback')])   
                     
    def test_get_sessions_status_active2(self):
        self.hash.set('localhost:foo', 'bar', {'status' : 'record',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'})
        self.assertEqual(self._get_cache().get_sessions_status('foo', 
                         status=('record', 'playback')), [('bar', 'record')])
        
    def test_get_sessions_status_all_status(self):
        self.hash.set('localhost:foo', 'bar', {'status' : 'playback',
                                              'session' : 'bar',
                                              'scenario' : 'localhost:foo'})
        self.hash.set('localhost:foo', 'bar2', {'status' : 'record',
                                              'session' : 'bar2',
                                              'scenario' : 'localhost:foo'})
        self.hash.set('localhost:foo', 'bar3', {'status' : 'dormant',
                                                'session' : 'bar3',
                                                'scenario' : 'localhost:foo'})
        
        self.assertEqual(sorted(self._get_cache().get_sessions_status('foo', 
                         status=('record', 'playback', 'dormant'))),  
                         sorted([('bar', 'playback'), ('bar2', 'record'),
                                ('bar3', 'dormant')]))  
        
    def test_get_request_not_found(self):
        self.assertEqual(self._get_cache().get_request('foo', 'bar', '1'), None)
        
    def test_get_request(self):
        self.hash.set('localhost:foo:request', 'bar:1', ["1", ""])
        self.assertEqual(self._get_cache().get_request('foo', 'bar', '1'), 
                         ["1", ""])                   
            
class Test_get_response_text(unittest.TestCase):
    '''
    redis> hgetall "localhost:first:response"
1. "first_2:1a90f47bb0af291264a6c06868b97cd62b372d41de26c3fd21cef61b"
2. "Hello {{1+1}} World\n"
    ''' 
    def setUp(self):
        self.hash = DummyHash()
        self.patch = mock.patch('stubo.cache.Hash', self.hash)
        self.patch.start()
        self.patch2 =  mock.patch('stubo.cache.get_redis_server', lambda x: x)
        self.patch2.start()    

    def tearDown(self):
        self.patch.stop() 
        self.patch2.stop() 
    
    def _get_cache(self):
        from stubo.cache import Cache
        return Cache('localhost')      
        
    def _func(self, scenario_name, session_name, response_id, request_id):    
        return self._get_cache().get_response(scenario_name, session_name, 
                                              response_id, request_id) 
    
    def test_no_state(self):
        self.hash.set('localhost:foo:response', 'bar:1', "Hello {{1+1}} World")
        self.assertEqual(self._func('foo', 'bar', ['1'], '1'), 
                        "Hello {{1+1}} World")  
        
    def test_with_state(self):
        self.hash.set('localhost:foo:response', 'bar:1', "Hello {{1+1}} World")
        self.hash.set('localhost:foo:response', 'bar:2', "Hello {{1+1}} World 2")
        self.assertEqual(self._func('foo', 'bar', ['1', '2'], '1'), 
                        "Hello {{1+1}} World")  
        self.assertEqual(self._func('foo', 'bar', ['1', '2'], '1'), 
                        "Hello {{1+1}} World 2") 
        self.assertEqual(self._func('foo', 'bar', ['1', '2'], '1'), 
                        "Hello {{1+1}} World 2")          
        
    def test_not_found(self):
        self.assertEqual(self._func('foo', 'bar', ['1'], '2'), None)
                            

class Test_add_request(Base):  
        
    def _func(self, session, stub, request_id='1', response_id='1'):      
        from stubo.model.stub import StubCache
        from stubo.cache import add_request
        stub_cache = StubCache(stub.payload, session.get('scenario'), session.get('session'))
        stub_cache.payload['response']['ids'] = [response_id]
        return add_request(session, request_id, stub_cache, '2013-09-05', 0)
    
    def test_with_delay(self):
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import Stub, create
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        stub.set_delay_policy(dict(name='slow'))
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        cache = self._get_cache()
        session = cache.create_session_cache('foo', 'bar')
        self._func(session, stub) 
        self.assertEqual(self.hash.get('localhost:foo:request',
            'bar:1'),
           [[u'1'], u'slow', None, u'2013-09-05', {}, u'12b0a0eced1ec13b53d186be7bd4909fa94d1916cca4daa25bd24d48'])
                   
    def test_it(self):  
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        cache = self._get_cache()
        session = cache.create_session_cache('foo', 'bar') 
        self._func(session, stub)  
        self.assertEqual(self.hash.get('localhost:foo:request',
            'bar:1'),
           [[u'1'], u'', None, u'2013-09-05', {}, u'12b0a0eced1ec13b53d186be7bd4909fa94d1916cca4daa25bd24d48'])
        
    def test_it_with_module(self):   
        module = {"system_date": "2013-08-07", "version": 1, "name": "mymodule"}
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        stub.set_module(module)
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        cache = self._get_cache()
        session = cache.create_session_cache('foo', 'bar') 
        self._func(session, stub)  
        self.assertEqual(self.hash.get('localhost:foo:request',
            'bar:1'),
           [[u'1'], u'', None, u'2013-09-05', module, u'12b0a0eced1ec13b53d186be7bd4909fa94d1916cca4daa25bd24d48'])
        
    def test_request_cache_limit(self): 
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        cache = self._get_cache()
        session = cache.create_session_cache('foo', 'bar') 
        
        for i in range(15):  
            self._func(session, stub, request_id='{0}'.format(i))  
        self.assertEqual(len(self.hash.get_all('localhost:foo:request')), 10)
          
    def test_request_cache_limit2(self): 
        scenario_name = 'foo'
        self._make_scenario('localhost:foo')
        from stubo.model.stub import create, Stub, response_hash
        stub = Stub(create('<test>match this</test>', '<test>OK</test>'),
                    'localhost:foo')
        doc = dict(scenario='localhost:foo', stub=stub)
        self.scenario.insert_stub(doc, stateful=True)  
        cache = self._get_cache()
        session = cache.create_session_cache('foo', 'bar') 
        self._func(session, stub, request_id='x', response_id=2)  
        for i in range(15):  
            self._func(session, stub, request_id='{0}'.format(i))  
        self.assertEqual(len(self.hash.get_all('localhost:foo:request')), 11)
                
class TestRequestIndex(unittest.TestCase):
    
    def setUp(self):
        self.hash = DummyHash({})
        self.hash_patch = mock.patch('stubo.cache.Hash', self.hash)
        self.hash_patch.start()
        self.main = DummyMain()
        self.patch_main = mock.patch('stubo.cache.get_redis_main',
                                       self.main)
        self.patch_main.start()

    def tearDown(self):
        self.hash_patch.stop() 
        self.patch_main.stop()
        
    def _get_cache(self):
        from stubo.cache import Cache
        return Cache('localhost')        
        
    def test_get_all_saved_request_index_data(self):
        self.hash.set('localhost:converse:saved_request_index', 'first', 
                      ["1", 0])
        self.main._keys = ['localhost:converse:saved_request_index']              
        response = self._get_cache().get_all_saved_request_index_data()
        self.assertEqual(response, {'converse': {'first': [u'1', 0]}})
        
    def test_get_all_saved_request_index_data2(self):
        self.hash.set('localhost:converse:saved_request_index', 'first', 
                      ["1", 0])
        self.hash.set('localhost:converse2:saved_request_index', 'first', 
                      ["1", 0])
        
        self.main._keys = ['localhost:converse:saved_request_index',
                             'localhost:converse2:saved_request_index']              
        response = self._get_cache().get_all_saved_request_index_data()
        self.assertEqual(response, {'converse': {'first': [u'1', 0]},
                                    'converse2': {'first': [u'1', 0]}})
        
    def test_delete_saved(self):
        self.hash.set('localhost:first:saved_request_index', "xxx", ["1", 0])
        response = self._get_cache().delete_saved_request_index('first:', "xxx")
        self.assertEqual(response, 0)
        
    def test_get_saved(self):
        self.hash.set('localhost:converse:saved_request_index', 'xxx', 
                      ["1", 0])             
        response = self._get_cache().get_saved_request_index_data('converse', 
                                                                  'xxx')
        self.assertEqual(response, [u'1', 0])
                    
         

class DummyModule(object):
    def __init__(self, host):
        pass
    
    def latest_version(self, name):
        return 1
       

class DummyMain(object):
    
    def __init__(self, keys=None):
        self._keys = keys or []
    
    def __call__(self, redis=None):
        return self
    
    def keys(self, host):
        return self._keys
    
    def exists(self, key):
        return key in self._keys
    
class DummyObjectId(object):
    """A MongoDB ObjectId.
    """
    __slots__ = ('__id', '__dt')

    def __init__(self, oid=None, dt=None):
        import datetime
        self.__id = oid 
        self.__dt = dt or datetime.datetime.utcnow() 
         
    @property
    def generation_time(self):      
        return self.__dt
                             
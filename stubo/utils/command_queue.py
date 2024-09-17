"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import logging
from urlparse import urlparse, parse_qs
from tornado.util import ObjectDict
from stubo.cache.queue import Queue, get_redis_main, get_redis_subordinate
from stubo.exceptions import HTTPServerError
from stubo.service.api import delete_module

log = logging.getLogger(__name__)

class InternalCommandQueue(object):
    """
    Command queue used to store commands that can be asynchronously processed.
    Commands are put to redis main and read & processed on subordinates.
    """
    name = 'task_queue'
    
    def __init__(self, redis_server=None):
        self.queue = Queue(self.name, 
                           server=redis_server or get_redis_subordinate())
    
    def process(self):
        try:
            # we are executing outside the context of a request so
            # get host from payload
            # { 
            #    'host' : 'localhost', 
            #    'cmd' : 'delete/module?name=amadeus'
            # } 
            num_cmds = len(self.queue)
            if num_cmds:
                log.debug('found {0} commands on queue'.format(num_cmds))
                while 1:
                    cmd = self.queue.get(1)
                    if not cmd:
                        break
                    log.debug('found command: {0}'.format(cmd))
                    self.run_cmd(cmd)
        except Exception, e:
            log.error(u"error processing command queue: {0}".format(e), 
                      exc_info=True)
                       
    def add(self, host, cmd):
        payload =  { 
            'host' : host, 
            'cmd' : cmd
        } 
        queue = Queue(self.name, get_redis_main())
        queue.put(payload)              

    def run_cmd(self, payload):
        host = payload['host']
        request = ObjectDict(host=host)
        request.track = ObjectDict(host=host)
        uri = urlparse(payload['cmd'])
        cmd = uri.path
        if cmd == 'delete/module':
            result = delete_module(request, parse_qs(uri.query)['name'])
            log.debug('result: {0}'.format(result))
        else:
            raise HTTPServerError(title="command '{0}' is not supported".format(
                cmd))
        log.info('background processed command: {0}'.format(payload['cmd']))
       
# -*- coding: utf-8 -*-
"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import logging
import time
import sys
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import socket

import tornado.web, tornado.ioloop, tornado.httpserver
from tornado.util import ObjectDict
from statsd import StatsClient

from stubo.service.handlers import HandlerFactory
from stubo.utils import (
    read_config, init_mongo, start_redis, asbool, init_ext_cache, resolve_class
)
from stubo.utils.command_queue import InternalCommandQueue
from stubo.utils.stats import StatsdStats
from stubo import version, static_path
from stubo.model.db import default_env, coerce_mongo_param
from stubo.service.urls import url_patterns

log = logging.getLogger(__name__)


class TornadoManager(object):
    """Set up and start Tornado ioloop.
    """

    def __init__(self, config_path):
        cfg = ObjectDict(read_config(config_path))
        cfg.db_name = cfg['mongo.db']
        cfg['num_processes'] = int(cfg.get('num_processes', 0))
        cfg['stubo_version'] = version
        cfg['debug'] = asbool(cfg.get('debug', False))
        max_workers = int(cfg.get('max_workers', 100))
        log.info('started with {0} worker threads'.format(max_workers))
        cfg['executor'] = ThreadPoolExecutor(max_workers)

        try:
            cfg['statsd_client'] = StatsClient(host=cfg.get('statsd.host',
                                                            'localhost'), prefix=cfg.get('statsd.prefix', 'stubo'))
            cfg['stats'] = StatsdStats()
            log.info('statsd host addr={0}, prefix={1}'.format(cfg['statsd_client']._addr,
                                                               cfg['statsd_client']._prefix))
        except socket.gaierror, e:
            log.warn("unable to connect to statsd: {0}".format(e))

        cfg['cluster_name'] = self.get_cluster_name()
        cfg['request_cache_limit'] = cfg.get('request_cache_limit', 10)
        cfg['decompress_request'] = cfg.get('decompress_request', True)
        cfg['compress_response'] = cfg.get('compress_response', False)
        self.cfg = cfg

    def get_cluster_name(self):
        name = 'unknown'
        try:
            with open(r'/opt/stubo_ng/etc/cluster_name.txt') as f:
                name = f.read().strip()
        except Exception, e:
            log.warn(e)
        return name

    def get_app(self):
        hooks_cls = self.cfg.get('hooks_cls',
                                 'stubo.ext.transformer.StuboDefaultHooks')
        self.cfg['hooks'] = resolve_class(hooks_cls)
        tornado_app = tornado.web.Application(
            static_path=static_path(),
            template_path=static_path('templates'),
            xheaders=True,
            **self.cfg)
        tornado_app.add_handlers('.*$', self._make_route_list())
        return tornado_app

    def start_server(self):
        """Make Tornado app, start server and Tornado ioloop.
        """
        dbenv = default_env.copy()
        dbenv.update((k[6:], coerce_mongo_param(k[6:], v)) for k, v in \
                     self.cfg.iteritems() if k.startswith('mongo.'))
        log.debug('mongo params: {0}'.format(dbenv))
        retry_count = int(self.cfg.get('retry_count', 10))
        retry_interval = int(self.cfg.get('retry_interval', 10))
        # getting database
        mongo_client = None
        for i in range(retry_count):
            try:
                mongo_client = init_mongo(dbenv)
                break
            except Exception as ex:
                log.warn('mongo not available, try again in {0} secs. Error: {1}'.format(retry_interval, ex))
                time.sleep(retry_interval)
        if not mongo_client:
            log.critical('Unable to connect to mongo, exiting ...')
            sys.exit(1)
        log.info('mongo server_info: {0}'.format(
            mongo_client.connection.server_info()))

        subordinate, main = start_redis(self.cfg)
        self.cfg['is_cluster'] = False
        if subordinate != main:
            log.info('redis main is not the same as the subordinate')
            self.cfg['is_cluster'] = True
        self.cfg['ext_cache'] = init_ext_cache(self.cfg)
        tornado_app = self.get_app()
        log.info('Started with "{0}" config'.format(tornado_app.settings))

        server = tornado.httpserver.HTTPServer(tornado_app)
        server.conn_params.decompress = self.cfg['decompress_request']
        tornado_port = self.cfg['tornado.port']
        try:
            server.bind(tornado_port)
        except Exception:
            # see http://stackoverflow.com/questions/16153804/tornado-socket-error-on-arm    
            server.bind(tornado_port, '0.0.0.0')
        server.start(self.cfg['num_processes'])

        max_process_workers = self.cfg.get('max_process_workers')
        if max_process_workers:
            max_process_workers = int(max_process_workers)
        tornado_app.settings['process_executor'] = ProcessPoolExecutor(max_process_workers)
        log.info('started with {0} worker processes'.format(tornado_app.settings['process_executor']._max_workers))

        cmd_queue = InternalCommandQueue()
        cmd_queue_poll_interval = self.cfg.get('cmd_queue_poll_interval',
                                               60 * 1000)
        tornado.ioloop.PeriodicCallback(cmd_queue.process,
                                        cmd_queue_poll_interval).start()
        tornado.ioloop.IOLoop.instance().start()

    def _make_route_list(self):
        """Make the links between the url called and the handler to use.
        uses url_patterns imported from stubo.service.urls.py
        """
        static_bootstrap_route = (
            r"/static/bootstrap/(.*)",
            tornado.web.StaticFileHandler,
            dict(path=static_path('bootstrap')))

        config_route_list = [(uri, HandlerFactory.make(handler_name))
                             for uri, handler_name in url_patterns]
        static_route = (r"/exports/(.*)", tornado.web.StaticFileHandler,
                        {"path": static_path('exports')})
        static_route2 = (r"/static/exports/(.*)", tornado.web.StaticFileHandler,
                         {"path": static_path('exports')})
        static_route3 = (r"/static/(.*)", tornado.web.StaticFileHandler,
                         {"path": static_path()})
        route_list = [static_route, static_route2, static_route3,
                      static_bootstrap_route] + config_route_list
        return route_list


def main():
    TornadoManager(sys.argv[1]).start_server()


if __name__ == "__main__":
    main()

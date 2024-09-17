"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
from argparse import ArgumentParser
import sys
import logging
import logging.config
from datetime import datetime, timedelta

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid

from stubo.utils import init_mongo, start_redis, as_date, read_config
from stubo.service.api import list_scenarios, get_status, delete_stubs
from stubo.model.db import default_env, coerce_mongo_param
from stubo.testing import DummyRequestHandler
from stubo.scripts import get_default_config

log = logging.getLogger(__name__)


def delete_test_dbs():
    parser = ArgumentParser(
        description="Delete test databases"
    )
    parser.add_argument('-l', '--list', action='store_const', const=True,
                        dest='list_only', help="Just list the test databases.")
    args = parser.parse_args()
    list_only = args.list_only
    db_conn = init_mongo().connection
    test_dbs = [x for x in db_conn.database_names() if x.startswith('test_')]
    if list_only:
        print test_dbs
    else:
        if test_dbs:
            print 'deleting databases: {0}'.format(", ".join(test_dbs))
            for dbname in test_dbs:
                db_conn.drop_database(dbname)
            print 'deleted databases'
        else:
            print 'no test databases to delete'


def create_tracker_collection():
    parser = ArgumentParser(
        description="Create tracker collection"
    )
    parser.add_argument('-s', '--size', default=1000000000,
                        dest='size', help="size of the collection in bytes, default is 1GB")
    parser.add_argument('-c', '--config', dest='config',
                        help='Path to configuration file (defaults to $CWD/etc/dev.ini)',
                        metavar='FILE')

    args = parser.parse_args()
    size = int(args.size)
    config = args.config or get_default_config()
    logging.config.fileConfig(config)
    db = init_mongo()
    log.info('creating tracker collection: size={0}b in db={1}'.format(size,
                                                                       db.name))
    args = {'capped': True, 'size': size}
    try:
        db.create_collection("tracker", **args)
    except CollectionInvalid, e:
        log.fatal(e)
        sys.exit(-1)

    log.info('creating tracking indexes')

    db.tracker.create_index([('start_time', DESCENDING)], background=True)
    db.tracker.create_index([('host', DESCENDING), ('start_time', DESCENDING)], background=True)
    db.tracker.create_index([('return_code', ASCENDING)], background=True)
    db.tracker.create_index([('host', DESCENDING)], background=True)
    db.tracker.create_index([('duration_ms', ASCENDING)], background=True)
    db.tracker.create_index([('session', DESCENDING)], background=True)
    db.tracker.create_index([('scenario', DESCENDING)], background=True)
    db.tracker.create_index([('function', DESCENDING)], background=True)

    log.info('created indexes: {0}'.format(db.tracker.index_information()))


def purge_stubs():
    parser = ArgumentParser(
        description="Purge stubs older than given expiry date."
    )
    parser.add_argument('-l', '--list', action='store_const', const=True,
                        dest='list_only', help="Just list the stubs to delete.")
    parser.add_argument('-e', '--expiry', default=14, dest='expiry',
                        help="expiry is number of days from now (default is 14).")
    parser.add_argument('--host', default='all', dest='host',
                        help="specify the host uri to use (defaults to all)")
    parser.add_argument('-c', '--config', dest='config',
                        help='Path to configuration file (defaults to $CWD/etc/dev.ini)',
                        metavar='FILE')

    args = parser.parse_args()
    list_only = args.list_only or False
    expiry_days = args.expiry
    expiry = datetime.today().date() - timedelta(int(expiry_days))
    host = args.host
    config = args.config or get_default_config()
    logging.config.fileConfig(config)

    settings = read_config(config)
    dbenv = default_env.copy()
    dbenv.update((k[6:], coerce_mongo_param(k[6:], v)) for k, v in \
                 settings.iteritems() if k.startswith('mongo.'))
    log.debug('mongo params: {0}'.format(dbenv))

    log.info('purge stubs whereby all sessions in the scenario were last used before {0}'.format(expiry))

    db_conn = init_mongo(dbenv).connection
    subordinate, main = start_redis(settings)
    response = list_scenarios(host)
    if 'error' in response:
        print response['error']
        sys.exit(-1)

    handler = DummyRequestHandler()
    session_handler = DummyRequestHandler()

    for scenario_key in response['data']['scenarios']:
        log.debug("*** scenario '{0}' ***".format(scenario_key))
        hostname, scenario = scenario_key.split(':')
        if host != 'all' and host != hostname:
            continue
        handler.host = hostname
        handler.request.host = '{0}:8001'.format(hostname)
        session_handler.host = hostname
        session_handler.request.host = '{0}:8001'.format(hostname)
        handler.request.arguments['scenario'] = [scenario]
        status = get_status(handler)
        if 'error' in status:
            log.warn('get_status error: {0}'.format(status['error']))
        else:
            scenario_last_used = []
            sessions = status['data']['sessions']
            for session in zip(*sessions)[0]:
                log.debug("*** -> session '{0}' ***".format(session))
                session_handler.request.arguments['session'] = [session]
                session_status = get_status(session_handler)
                if 'error' in session_status:
                    log.warn('get_status error: {0}'.format(status['error']))
                else:
                    last_used = session_status['data']['session'].get('last_used', '-')
                    if last_used != '-':
                        scenario_last_used.append(as_date(last_used[0:10]))

            if scenario_last_used and (max(scenario_last_used) < expiry):
                log.info("sessions in scenario '{0}' were last used '{1}' which"
                         " is before expiry date '{2}'".format(scenario_key,
                                                               max(scenario_last_used), expiry))
                if not list_only:
                    response = delete_stubs(handler, scenario_name=scenario,
                                            force=True)
                    if 'error' in response:
                        log.error('delete stubs error: {0}'.format(response['error']))
                    else:
                        log.info('deleted stubs: {0}'.format(response['data']))

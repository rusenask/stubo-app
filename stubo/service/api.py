"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import os
import zipfile
import tarfile
import shutil
import logging
import random
import sys
import copy
from urlparse import urlparse
from StringIO import StringIO
from contextlib import closing

from tornado.web import MissingArgumentError

from tornado.util import ObjectDict

from stubo.model.db import (
    Scenario, get_mongo_client, session_last_used, Tracker
)
import stubo.model.db
from stubo.model.importer import YAMLImporter, UriLocation, UrlFetch
from stubo.model.exporter import Exporter
from stubo.model.cmds import (
    TextCommandsImporter, form_input_cmds
)
from stubo.model.stub import Stub, StubCache, parse_stub
from stubo.exceptions import (
    exception_response, StuboException
)
from stubo import version
from stubo.cache import (
    Cache, add_request, get_redis_server, get_keys
)
from stubo.utils import (
    asbool, make_temp_dir, get_export_links, get_hostname,
    pretty_format_python, as_date
)
from stubo.utils.track import TrackTrace
from stubo.match import match
from stubo.model.request import StuboRequest
from stubo.ext import today_str
from stubo.ext.transformer import transform
from stubo.ext.module import Module
from .delay import Delay
from stubo.model.export_commands import export_stubs_to_commands_format

DummyModel = ObjectDict

log = logging.getLogger(__name__)


def get_dbenv(handler):
    dbenv = None
    if 'mongo.host' in handler.settings:
        dbenv = stubo.model.db.default_env.copy()
        dbenv.update({
            'host': handler.settings['mongo.host'],
            'port': int(handler.settings['mongo.port'])
        })
    return dbenv


def export_stubs(handler, scenario_name):
    from stubo.model.exporter import YAML_FORMAT_SUBDIR
    # export stubs in the old format
    command_links = export_stubs_to_commands_format(handler, scenario_name)
    # continue stub export in the new format
    cache = Cache(get_hostname(handler.request))
    scenario_name_key = cache.scenario_key_name(scenario_name)

    exporter = Exporter(static_dir=handler.settings['static_path'])
    runnable = asbool(handler.get_argument('runnable', False))
    playback_session = handler.get_argument('playback_session', None)
    export_dir_path, files, runnable_info = exporter.export(scenario_name_key,
                                                            runnable=runnable,
                                                            playback_session=playback_session,
                                                            session_id=handler.get_argument('session_id', None),
                                                            export_dir=handler.get_argument('export_dir', None))

    # getting export links
    yaml_links = get_export_links(handler, scenario_name_key + "/" + YAML_FORMAT_SUBDIR, files)

    payload = dict(scenario=scenario_name, export_dir_path=export_dir_path,
                   command_links=command_links, yaml_links=yaml_links)
    if runnable_info:
        payload['runnable'] = runnable_info
    return dict(version=version, data=payload)


def list_stubs(handler, scenario_name, host=None):
    cache = Cache(host or get_hostname(handler.request))
    scenario = Scenario()
    stubs = scenario.get_stubs(cache.scenario_key_name(scenario_name))
    result = dict(version=version, data=dict(scenario=scenario_name))
    if stubs:
        result['data']['stubs'] = [x['stub'] for x in stubs]
    return result


def list_scenarios(host):
    response = {'version': version}
    scenario_db = Scenario()
    if host == 'all':
        scenarios = [x['name'] for x in scenario_db.get_all()]
    else:
        # get all scenarios for host
        scenarios = [x['name'] for x in scenario_db.get_all(
            {'$regex': '{0}:.*'.format(host)})]
    response['data'] = dict(host=host, scenarios=scenarios)
    return response


def stub_count(host, scenario_name=None):
    if host == 'all':
        scenario_name_key = None
    else:
        if not scenario_name:
            # get all stubs for this host
            value = '{0}:.*'.format(host)
            scenario_name_key = {'$regex': value}
        else:
            scenario_name_key = ":".join([host, scenario_name])
    scenario = Scenario()
    result = {'version': version}
    count = scenario.stub_count(scenario_name_key)
    result['data'] = {'count': count,
                      'scenario': scenario_name or 'all',
                      'host': host}
    return result


def get_stubs(host, scenario_name=None):
    if not scenario_name:
        # get all stubs for this host
        scenario_name_key = {'$regex': '{0}:.*'.format(host)}
    else:
        scenario_name_key = ":".join([host, scenario_name])
    scenario = Scenario()
    return scenario.get_stubs(scenario_name_key)


def run_command_file(cmd_file_url, request, static_path):
    def run(cmd_file_path):
        response = {
            'version': version
        }
        is_legacy_text_format = cmd_file_path.endswith('.commands')
        location = UriLocation(request)
        cmd_processor = TextCommandsImporter(location, cmd_file_path) if is_legacy_text_format else YAMLImporter(
            location, cmd_file_path)
        responses = cmd_processor.run()
        log.debug('responses: {0}'.format(responses))
        response['data'] = responses
        return response

    file_type = os.path.basename(urlparse(cmd_file_url).path).rpartition(
        '.')[-1]
    supported_types = ('zip', 'gz', 'tar', 'jar')
    if file_type in supported_types:
        # import compressed contents and run contained config file
        import_dir = os.path.join(static_path, 'imports')
        with make_temp_dir(dirname=import_dir) as temp_dir:
            temp_dir_name = os.path.basename(temp_dir)
            response, headers, status_code = UrlFetch().get(
                UriLocation(request)(cmd_file_url)[0])
            content_type = headers["Content-Type"]
            log.debug('received {0} file.'.format(content_type))
            config_types = ('.yaml', '.commands')

            def run_config(files):
                # find the config file in the extract and run it
                config_file = [x for x in files if x.endswith(config_types)]
                if not config_file:
                    raise exception_response(400, title='Config file not'
                                                        ' found in archive: {0}'.format(cmd_file_url))
                return run(os.path.join('static', 'imports', temp_dir_name,
                                        config_file[0]))

            if content_type == 'application/x-tar' or file_type == 'tar':
                with closing(tarfile.open(fileobj=StringIO(response))) as tar:
                    tar.extractall(path=temp_dir)
                    response = run_config(tar.getnames())

            elif content_type in ('application/zip',
                                  'application/java-archive') or file_type in \
                    ('zip', 'jar'):
                with zipfile.ZipFile(StringIO(response)) as zipf:
                    zipf.extractall(path=temp_dir)
                    response = run_config(zipf.namelist())
            else:
                raise exception_response(400, title='Expected Content-Type has'
                                                    ' to be one of these: {0} not {1}'.format(supported_types,
                                                                                              content_type))
    else:
        response = run(cmd_file_url)
    return response


def run_commands(handler, cmds_text):
    response = {
        'version': version
    }
    host = get_hostname(handler.request)

    cmd_processor = TextCommandsImporter(UriLocation(handler.request))
    cmds = cmd_processor.parse(cmds_text)
    if any(x for x in cmds if urlparse(x).path not in form_input_cmds):
        raise exception_response(400, title='command/s not supported, must be '
                                            'one of these: {0}'.format(form_input_cmds))

    responses = cmd_processor.run_cmds(cmds)
    log.debug('responses: {0}'.format(responses))

    response['data'] = {
        'executed_commands': responses,
        'number_of_requests': len(responses['commands']),
        'number_of_errors': len([x for x in responses['commands'] if x[1] > 399])
    }

    def get_links(cmd):
        cmd_uri = urlparse(cmd)
        scenario_name = cmd_uri.query.partition('=')[-1]
        scenario_name_key = '{0}:{1}'.format(host, scenario_name)
        files = [(scenario_name + '.zip',), (scenario_name + '.tar.gz',),
                 (scenario_name + '.jar',)]
        links = get_export_links(handler, scenario_name_key, files)
        return links

    export_links = [(x, get_links(x)) for x in cmds if 'get/export' in x]
    if export_links:
        response['data']['export_links'] = export_links

    return response


def delete_module(request, names):
    module = Module(get_hostname(request))
    removed = []
    for name in names:
        loaded_versions = [x for x in sys.modules.keys() if '{0}_v'.format(name) in x]
        for loaded in loaded_versions:
            module.remove_sys_module(loaded)
        if module.remove(name):
            removed.append('{0}:{1}'.format(module.host(), name))
    return {
        'version': version,
        'data': {'message': 'delete modules: {0}'.format(names),
                 'deleted': removed}
    }


def list_module(handler, names):
    module = Module(get_hostname(handler.request))
    info = {}
    if not names:
        names = [x.rpartition(':')[-1] for x in get_keys(
            '{0}:modules:*'.format(module.host()))]
    for name in names:
        loaded_sys_versions = [x for x in sys.modules.keys() if '{0}_v'.format(name) in x]
        lastest_code_version = module.latest_version(name)
        info[name] = {
            'latest_code_version': lastest_code_version,
            'loaded_sys_versions': loaded_sys_versions
        }
    payload = dict(message='list modules', info=info)
    return {
        'version': version,
        'data': payload
    }


def put_module(handler, names):
    module = Module(handler.track.host)
    added = []
    result = dict(version=version)
    for name in names:
        uri, module_name = UriLocation(handler.request)(name)
        log.info('uri={0}, module_name={1}'.format(uri, module_name))
        response, _, code = UrlFetch().get(uri)
        module_name = module_name[:-3]
        last_version = module.latest_version(module_name)
        module_version_name = module.sys_module_name(module_name,
                                                     last_version + 1)
        if last_version and response == module.get_source(module_name,
                                                          last_version):
            msg = 'Module source has not changed for {0}'.format(
                module_version_name)
            result['data'] = dict(message=msg)
        try:
            code, mod = module.add_sys_module(module_version_name, response)
            log.debug('{0}, {1}'.format(mod, code))
        except Exception, e:
            msg = 'error={0}'.format(e)
            raise exception_response(400,
                                     title='Unable to compile {0}:{1}, {2}'.format(module.host(),
                                                                                   module_version_name, msg))
        module.add(module_name, response)
        added.append(module_version_name)
    result['data'] = dict(message='added modules: {0}'.format(added))
    return result


def put_stub(handler, session_name, delay_policy, stateful, priority,
             recorded=None, module_name=None, recorded_module_system_date=None):
    log.debug('put_stub request: {0}'.format(handler.request))
    request = handler.request
    stubo_request = StuboRequest(request)
    session_name = session_name.partition(',')[0]
    cache = Cache(get_hostname(request))
    scenario_key = cache.find_scenario_key(session_name)
    trace = TrackTrace(handler.track, 'put_stub')
    url_args = handler.track.request_params
    err_msg = 'put/stub body format error - {0}, for session: {1}'
    try:
        stub = parse_stub(stubo_request.body_unicode, scenario_key, url_args)
    except Exception, e:
        raise exception_response(400, title=err_msg.format(e.message,
                                                           session_name))

    log.debug('stub: {0}'.format(stub))
    if delay_policy:
        stub.set_delay_policy(delay_policy)
    stub.set_priority(priority)

    session = cache.get_session(scenario_key.partition(':')[-1],
                                session_name,
                                local=False)
    if not session:
        raise exception_response(400, title='session not found - {0}'.format(
            session_name))
    stub.set_recorded(recorded or today_str('%Y-%m-%d'))
    if module_name:
        stub.set_module({
            'name': module_name,
            # TODO: is module['system_date'] used?
            'system_date': today_str('%Y-%m-%d'),
            'recorded_system_date': recorded_module_system_date or today_str(
                '%Y-%m-%d')
        })
        trace.info('module used', stub.module())
        source_stub = copy.deepcopy(stub)
        stub, _ = transform(stub, stubo_request, function='put/stub',
                            cache=handler.settings['ext_cache'],
                            hooks=handler.settings['hooks'],
                            stage='put/stub',
                            trace=trace,
                            url_args=url_args)
        if source_stub != stub:
            trace.diff('stub was transformed', source_stub.payload,
                       stub.payload)
            trace.info('stub was transformed into', stub.payload)

    scenario_name = session['scenario']
    handler.track.scenario = scenario_name.partition(':')[2]
    session_status = session['status']
    if session_status != 'record':
        raise exception_response(400, title='Scenario not in record '
                                            'mode - {0} in {1} mode.'.format(scenario_name, session_status))
    doc = dict(scenario=scenario_name, stub=stub)
    scenario_col = Scenario()
    result = scenario_col.insert_stub(doc, stateful)
    response = {'version': version, 'data': {'message': result}}
    return response


def calculate_delay(policy):
    delay = 0
    delay_type = policy.get('delay_type')
    if delay_type == 'fixed':
        delay = policy['milliseconds']
    elif delay_type == 'normalvariate':
        # Calculate from the normal distribution, but set minimum at zero
        delay = max(0.0, random.normalvariate(int(policy['mean']),
                                              int(policy['stddev'])))
    else:
        log.warn('unknown delay type: {0} encountered'.format(delay_type))
    return float(delay)


def get_response(handler, session_name):
    request = handler.request
    stubo_request = StuboRequest(request)
    cache = Cache(get_hostname(request))
    if cache.blacklisted():
        raise exception_response(400, title="Sorry the host URL '{0}' has been "
                                            "blacklisted. Please contact Stub-O-Matic support.".format(cache.host))
    scenario_key = cache.find_scenario_key(session_name)
    scenario_name = scenario_key.partition(':')[-1]
    handler.track.scenario = scenario_name
    request_id = stubo_request.id()
    module_system_date = handler.get_argument('system_date', None)
    url_args = handler.track.request_params
    if not module_system_date:
        # LEGACY
        module_system_date = handler.get_argument('stubbedSystemDate', None)
    trace_matcher = TrackTrace(handler.track, 'matcher')
    user_cache = handler.settings['ext_cache']
    # check cached requests
    cached_request = cache.get_request(scenario_name, session_name, request_id)
    if cached_request:
        response_ids, delay_policy_name, recorded, system_date, module_info, request_index_key = cached_request
    else:
        retry_count = 5 if handler.settings.get('is_cluster', False) else 1
        session, retries = cache.get_session_with_delay(scenario_name,
                                                        session_name,
                                                        retry_count=retry_count,
                                                        retry_interval=1)
        if retries > 0:
            log.warn("replication was slow for session: {0} {1}, it took {2} secs!".format(
                scenario_key, session_name, retries + 1))
        if session['status'] != 'playback':
            raise exception_response(500,
                                     title='cache status != playback. session={0}'.format(session))

        system_date = session['system_date']
        if not system_date:
            raise exception_response(500,
                                     title="subordinate session {0} not available for scenario {1}".format(
                                         session_name, scenario_key))

        session['ext_cache'] = user_cache
        result = match(stubo_request, session, trace_matcher,
                       as_date(system_date),
                       url_args=url_args,
                       hooks=handler.settings['hooks'],
                       module_system_date=module_system_date)
        if not result[0]:
            raise exception_response(400,
                                     title='E017:No matching response found')
        _, stub_number, stub = result
        response_ids = stub.response_ids()
        delay_policy_name = stub.delay_policy_name()
        recorded = stub.recorded()
        module_info = stub.module()
        request_index_key = add_request(session, request_id, stub, system_date,
                                        stub_number,
                                        handler.settings['request_cache_limit'])

        if not stub.response_body():
            _response = stub.get_response_from_cache(request_index_key)
            stub.set_response_body(_response['body'])

        if delay_policy_name:
            stub.load_delay_from_cache(delay_policy_name)

    if cached_request:
        stub = StubCache({}, scenario_key, session_name)
        stub.load_from_cache(response_ids, delay_policy_name, recorded,
                             system_date, module_info, request_index_key)
    trace_response = TrackTrace(handler.track, 'response')
    if module_info:
        trace_response.info('module used', str(module_info))
    response_text = stub.response_body()
    if not response_text:
        raise exception_response(500,
                                 title='Unable to find response in cache using session: {0}:{1}, '
                                       'response_ids: {2}'.format(scenario_key, session_name, response_ids))

    # get latest delay policy
    delay_policy = stub.delay_policy()
    if delay_policy:
        delay = Delay.parse_args(delay_policy)
        if delay:
            delay = delay.calculate()
            msg = 'apply delay: {0} => {1}'.format(delay_policy, delay)
            log.debug(msg)
            handler.track['delay'] = delay
            trace_response.info(msg)

    trace_response.info('found response')
    module_system_date = as_date(module_system_date) if module_system_date \
        else module_system_date
    stub, _ = transform(stub,
                        stubo_request,
                        module_system_date=module_system_date,
                        system_date=as_date(system_date),
                        function='get/response',
                        cache=user_cache,
                        hooks=handler.settings['hooks'],
                        stage='response',
                        trace=trace_response,
                        url_args=url_args)
    transfomed_response_text = stub.response_body()[0]
    # Note transformed_response_text can be encoded in utf8
    if response_text[0] != transfomed_response_text:
        trace_response.diff('response:transformed',
                            dict(response=response_text[0]),
                            dict(response=transfomed_response_text))
    if stub.response_status() != 200:
        handler.set_status(stub.response_status())
    if stub.response_headers():
        for k, v in stub.response_headers().iteritems():
            handler.set_header(k, v)
    return transfomed_response_text


def delete_stubs(handler, scenario_name=None, host=None, force=False):
    """delete all data relating to one named scenario or host/s."""
    log.debug('delete_stubs')
    response = {
        'version': version
    }
    scenario_db = Scenario()
    static_dir = handler.settings['static_path']

    def delete_scenario(sce_name_key, frc):
        log.debug(u'delete_scenario: {0}'.format(sce_name_key))
        # getting host and scenario names
        hst, sce_name = sce_name_key.split(':')
        cache = Cache(hst)
        if not frc:
            active_sessions = cache.get_active_sessions(sce_name,
                                                        local=False)
            if active_sessions:
                raise exception_response(400,
                                         title='Sessons in playback/record, can not delete. '
                                               'Found the following active sessions: {0} '
                                               'for scenario: {1}'.format(active_sessions, sce_name))

        scenario_db.remove_all(sce_name_key)
        cache.delete_caches(sce_name)

    scenarios = []
    if scenario_name:
        # if scenario_name exists it takes priority 
        handler.track.scenario = scenario_name
        hostname = host or get_hostname(handler.request)
        scenarios.append(':'.join([hostname, scenario_name]))
    elif host:
        if host == 'all':
            scenarios = [x['name'] for x in scenario_db.get_all()]
            export_dir = os.path.join(static_dir, 'exports')
            if os.path.exists(export_dir):
                log.info('delete export dir')
                shutil.rmtree(export_dir)
        else:
            # get all scenarios for host
            scenarios = [x['name'] for x in scenario_db.get_all(
                {'$regex': '{0}:.*'.format(host)})]
    else:
        raise exception_response(400,
                                 title='scenario or host argument required')
    for scenario_name_key in scenarios:
        delete_scenario(scenario_name_key, force)

    response['data'] = dict(message='stubs deleted.', scenarios=scenarios)
    return response


def begin_session(handler, scenario_name, session_name, mode, system_date=None,
                  warm_cache=False):
    log.debug('begin_session')
    response = {
        'version': version
    }
    scenario_col = Scenario()
    cache = Cache(get_hostname(handler.request))
    if cache.blacklisted():
        raise exception_response(400, title="Sorry the host URL '{0}' has been "
                                            "blacklisted. Please contact Stub-O-Matic support.".format(cache.host))
    scenario_name_key = cache.scenario_key_name(scenario_name)
    scenario = scenario_col.get(scenario_name_key)
    cache.assert_valid_session(scenario_name, session_name)
    if mode == 'record':
        log.debug('begin_session, mode=record')
        # precond: delete/stubs?scenario={scenario_name} 
        if scenario:
            err = exception_response(400,
                                     title='Duplicate scenario found - {0}'.format(scenario_name_key))
            raise err
        if scenario_col.stub_count(scenario_name_key) != 0:
            raise exception_response(500,
                                     title='stub_count !=0 for scenario: {0}'.format(
                                         scenario_name_key))
        scenario_id = scenario_col.insert(name=scenario_name_key)
        log.debug('new scenario: {0}'.format(scenario_id))
        session_payload = {
            'status': 'record',
            'scenario': scenario_name_key,
            'scenario_id': str(scenario_id),
            'session': str(session_name)
        }
        cache.set_session(scenario_name, session_name, session_payload)
        log.debug('new redis session: {0}:{1}'.format(scenario_name_key,
                                                      session_name))
        response["data"] = {
            'message': 'Record mode initiated....',
        }
        response["data"].update(session_payload)
        cache.set_session_map(scenario_name, session_name)
        log.debug('finish record')

    elif mode == 'playback':
        if not scenario:
            raise exception_response(400,
                                     title='Scenario not found - {0}'.format(scenario_name_key))
        recordings = cache.get_sessions_status(scenario_name,
                                               status='record',
                                               local=False)
        if recordings:
            raise exception_response(400, title='Scenario recordings taking '
                                                'place - {0}. Found the following '
                                                'record sessions: {1}'.format(scenario_name_key, recordings))
        cache.create_session_cache(scenario_name, session_name, system_date)
        if warm_cache:
            # iterate over stubs and call get/response for each stub matchers
            # to build the request & request_index cache
            # reset request_index to 0
            log.debug("warm cache for session '{0}'".format(session_name))
            scenario_col = Scenario()
            for payload in scenario_col.get_stubs(scenario_name_key):
                stub = Stub(payload['stub'], scenario_name_key)
                mock_request = " ".join(stub.contains_matchers())
                handler.request.body = mock_request
                get_response(handler, session_name)
            cache.reset_request_index(scenario_name)

        response["data"] = {
            "message": "Playback mode initiated...."
        }
        response["data"].update({
            "status": "playback",
            "scenario": scenario_name_key,
            "session": str(session_name)
        })
    else:
        raise exception_response(400,
                                 title='Mode of playback or record required')
    return response


def store_source_recording(scenario_name_key, record_session):
    host, scenario_name = scenario_name_key.split(':')
    # use original put/stub payload logged in tracker
    tracker = Tracker()
    last_used = tracker.session_last_used(scenario_name_key,
                                          record_session, 'record')
    if not last_used:
        # empty recordings are currently supported!
        log.debug('Unable to find a recording for session={0}, scenario={1}'.format(record_session, scenario_name_key))
        return

    recording = tracker.get_last_recording(scenario_name, record_session,
                                           last_used['start_time'])
    recording = list(recording)
    if not recording:
        raise exception_response(400,
                                 title="Unable to find a recording for scenario='{0}', record_session='{1}'".format(
                                     scenario_name, record_session))

    number_of_requests = len(recording)
    scenario_db = Scenario()
    for nrequest in range(number_of_requests):
        track = recording[nrequest]
        request_text = track.get('request_text')
        if not request_text:
            raise exception_response(400, title='Unable to obtain recording details, was full tracking enabled?')

        priority = int(track['request_params'].get('priority', nrequest + 1))
        stub = parse_stub(request_text, scenario_name_key,
                          track['request_params'])
        stub.set_priority(priority)
        scenario_db.insert_pre_stub(scenario_name_key, stub)


def end_session(handler, session_name):
    response = {
        'version': version
    }
    cache = Cache(get_hostname(handler.request))
    scenario_key = cache.get_scenario_key(session_name)
    if not scenario_key:
        # end/session?session=x called before begin/session
        response['data'] = {
            'message': 'Session ended'
        }
        return response

    host, scenario_name = scenario_key.split(':')

    session = cache.get_session(scenario_name, session_name, local=False)
    if not session:
        # end/session?session=x called before begin/session
        response['data'] = {
            'message': 'Session ended'
        }
        return response

    handler.track.scenario = scenario_name
    session_status = session['status']
    if session_status not in ('record', 'playback'):
        log.warn('expecting session={0} to be in record or playback for '
                 'end/session'.format(session_name))

    session['status'] = 'dormant'
    # clear stubs cache & scenario session data
    session.pop('stubs', None)
    cache.set(scenario_key, session_name, session)
    cache.delete_session_data(scenario_name, session_name)
    if session_status == 'record':
        log.debug('store source recording to pre_scenario_stub')
        store_source_recording(scenario_key, session_name)

    response['data'] = {
        'message': 'Session ended'
    }
    return response


def end_sessions(handler, scenario_name):
    response = {
        'version': version,
        'data': {}
    }
    cache = Cache(get_hostname(handler.request))
    sessions = list(cache.get_sessions_status(scenario_name,
                                              status=('record', 'playback')))
    for session_name, session in sessions:
        session_response = end_session(handler, session_name)
        response['data'][session_name] = session_response.get('data')
    return response


def update_delay_policy(handler, doc):
    """Record delay policy in redis to be available globally to any
    users for their sessions.
    put/delay_policy?name=rtz_1&delay_type=fixed&milliseconds=700
    put/delay_policy?name=rtz_2&delay_type=normalvariate&mean=100&stddev=50
    """
    cache = Cache(get_hostname(handler.request))
    response = {
        'version': version
    }
    err = None
    if 'name' not in doc:
        err = "'name' param not found in request"
    if 'delay_type' not in doc:
        err = "'delay_type' param not found in request"
    if doc['delay_type'] == 'fixed':
        if 'milliseconds' not in doc:
            err = "'milliseconds' param is required for 'fixed' delays"
    elif doc['delay_type'] == 'normalvariate':
        if 'mean' not in doc or 'stddev' not in doc:
            err = "'mean' and 'stddev' params are required for " \
                  "'normalvariate' delays"
    elif doc['delay_type'] == 'weighted':
        if 'delays' not in doc:
            err = "'delays' are required for 'weighted' delays"
        else:
            try:
                Delay.parse_args(doc)
            except Exception, e:
                err = 'Unable to parse weighted delay arguments: {0}'.format(str(e))
    else:
        err = 'Unknown delay type: {0}'.format(doc['delay_type'])
    if err:
        raise exception_response(400,
                                 title=u'put/delay_policy arg error: {0}'.format(err))
    result = cache.set_delay_policy(doc['name'], doc)
    updated = 'new' if result else 'updated'
    response['data'] = {
        'message': 'Put Delay Policy Finished',
        'name': doc['name'],
        'delay_type': doc['delay_type'],
        'status': updated
    }
    return response


def get_delay_policy(handler, name, cache_loc):
    cache = Cache(get_hostname(handler.request))
    response = {
        'version': version
    }
    delay = cache.get_delay_policy(name, cache_loc)
    response['data'] = delay or {}
    return response


def delete_delay_policy(handler, names):
    cache = Cache(get_hostname(handler.request))
    response = {
        'version': version
    }
    num_deleted = cache.delete_delay_policy(names)
    response['data'] = {
        'message': 'Deleted {0} delay policies from {1}'.format(num_deleted,
                                                                names)
    }
    return response


def put_setting(handler, setting, value, host):
    response = {
        'version': version
    }
    all_hosts = True if host == 'all' else False
    if all_hosts:
        host = get_hostname(handler.request)
    cache = Cache(host)
    new_setting = cache.set_stubo_setting(setting, value, all_hosts)
    response['data'] = {
        'host': host,
        'all': all_hosts,
        'new': 'true' if new_setting else 'false', setting: value
    }
    return response


def get_setting(handler, host, setting=None):
    all_hosts = True if host == 'all' else False
    if all_hosts:
        host = get_hostname(handler.request)
    cache = Cache(host)
    result = cache.get_stubo_setting(setting, all_hosts)
    response = dict(version=version, data=dict(host=host, all=all_hosts))
    if setting:
        response['data'][setting] = result
    else:
        response['data']['settings'] = result
    return response


def get_status(handler):
    """Check status. 
       query args: 
         scenario=name 
         session=name
         check_database=true|false (default true)
         local_cache=true|false (default true)
    """
    request = handler.request
    cache = Cache(get_hostname(request))
    response = dict(version=version, data={})
    args = dict((key, value[0]) for key, value in request.arguments.iteritems())
    local_cache = asbool(args.get('local_cache', True))
    redis_server = get_redis_server(local_cache)
    response['data']['cache_server'] = {'local': local_cache}
    response['data']['info'] = {
        'cluster': handler.settings.get('cluster_name'),
        'graphite_host': handler.settings.get('graphite.host')
    }

    try:
        result = redis_server.ping()
        response['data']['cache_server']['status'] = 'ok' if result else 'bad'
    except Exception, e:
        response['data']['cache_server']['status'] = 'bad'
        response['data']['cache_server']['error'] = str(e)
        return response

    scenario_name = args.get('scenario')
    session_name = args.get('session')
    # session takes precedence
    if session_name:
        scenario_key = cache.get_scenario_key(session_name)
        session = {}
        if scenario_key:
            session = cache.get_session(scenario_key.partition(':')[-1],
                                        session_name)
        response['data']['session'] = session
    elif scenario_name:
        sessions = list(cache.get_sessions_status(scenario_name,
                                                  local=local_cache))
        response['data']['sessions'] = sessions

    check_database = asbool(args.get('check_database', True))
    if check_database:
        response['data']['database_server'] = {'status': 'bad'}
        try:
            if get_mongo_client().connection.alive():
                response['data']['database_server']['status'] = 'ok'
        except:
            response['data']['database_server']['error'] = "mongo down"
    return response


def manage_request_api(handler):
    """
    Generate data for /manage page.
    :param handler: instance of RequestHandler or TrackRequest
    :return: dictionary with information about stubo instance - modules, delays, sessions..
    """
    cache = Cache(get_hostname(handler.request))
    action = handler.get_argument('action', None)
    all_hosts = asbool(handler.get_argument("all_hosts", False))
    message = error_message = ""
    module_info = {}
    if action is not None:
        # handle btn action 
        try:
            name = handler.get_argument('name')
            # It would be nice to really track these actions
            handler.track = DummyModel()
            if action == 'delete':
                _type = handler.get_argument('type')
                if _type == 'module':
                    result = delete_module(handler.request, [name])
                elif _type == 'delay_policy':
                    result = delete_delay_policy(handler, [name])
                else:
                    result = 'error: unexpected action type={0}'.format(_type)
            else:
                result = 'error: unexpected action={0}'.format(action)

            if 'error' not in result:
                message = result
            else:
                error_message = result
        except MissingArgumentError, e:
            error_message = "Error: {0}".format(e)
        except StuboException, e:
            error_message = "Error: {0}".format(e.title)

    cmd_file = handler.get_argument('cmdFile', '')
    response = dict(host_scenarios=get_session_status(handler, all_hosts=all_hosts))
    cache_loc = handler.get_argument('cache', 'main')
    # get delays and format output (splitting weighted delays into a list)
    delays = get_delay_policy(handler, None, cache_loc).get('data')
    delays = _format_delay_types(delays)

    response['delays'] = delays
    modules = list_module(handler, None)['data'].get('info')
    for name in modules.keys():
        source_text = pretty_format_python(Module(cache.host).get_source(name))
        modules[name]['code'] = source_text
    response['modules'] = modules
    response['cmdFile'] = cmd_file
    response['message'] = message
    response['error_message'] = error_message
    response['module_info'] = module_info
    response['stubo_version'] = version
    response['host'] = cache.host
    response['page_name'] = 'Manage'
    return response


def _format_delay_types(delays):
    """
    Format delay types, removing colon and moving delay types into separate lines
    :param delays: delays dictionary
    :return: delays dictionary
    """
    if delays:
        for name, delay_data in delays.iteritems():
            if delay_data['delay_type'] == "weighted":
                delay_data['delays'] = delay_data['delays'].split(":")
    return delays


from collections import defaultdict


def get_session_status(handler, all_hosts=True):
    scenario = Scenario()
    host_scenarios = defaultdict()

    # getting a dictionary with sizes for all scenarios
    scenario_sizes = scenario.size()
    scenarios_recorded = scenario.recorded()

    for s in scenario.get_all():
        host, scenario_name = s['name'].split(':')
        if not all_hosts and get_hostname(handler.request) != host:
            continue
        if host not in host_scenarios:
            host_scenarios[host] = {}

        # getting session data
        sessions = []
        cache = Cache(host)

        for session_name, session in cache.get_sessions(scenario_name):
            # try and get the last_used from the last tracker get/response
            # else when the begin/session playback was called
            last_used = session_last_used(s['name'], session_name, 'playback')
            if last_used:
                last_used = last_used['start_time'].strftime('%Y-%m-%d %H:%M:%S')
            else:
                # session has never been used for playback 
                last_used = session.get('last_used', '-')
            session['last_used'] = last_used
            # removing stub information since we aren't using it anyway and it can consume a lot of memory
            session.pop('stubs', None)
            # creating sessions list
            sessions.append(session)
        # getting stub count
        stub_counts = stub_count(host, scenario_name)['data']['count']
        recorded = '-'

        # adding session information
        if sessions:
            if stub_counts:
                # getting scenario size and recorded values
                scenario_size = 0
                try:
                    scenario_size = scenario_sizes[s['name']]
                    recorded = scenarios_recorded[s['name']]
                except KeyError:
                    log.debug("Could not get scenario size for: %s" % s['name'])
                except Exception as ex:
                    log.warn("Failed to get scenario size for: %s, got error: %s" % (s['name'], ex))
                # creating a dict with scenario information
                host_scenarios[host][scenario_name] = (sessions, stub_counts, recorded, round(scenario_size, 0))

            else:
                host_scenarios[host][scenario_name] = (sessions, 0, '-', 0)

    return host_scenarios

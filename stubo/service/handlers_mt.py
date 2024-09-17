"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import datetime
import logging
from functools import partial, wraps
from urlparse import urlparse, parse_qs, unquote

import tornado.ioloop
import tornado.web
from tornado.util import ObjectDict

from .api import (
    export_stubs, list_stubs, run_command_file, run_commands,
    update_delay_policy, stub_count, begin_session, put_stub,
    get_response, delete_stubs, get_status, get_delay_policy, put_module,
    delete_module, list_module, delete_delay_policy, manage_request_api, put_setting, get_setting, end_sessions,
    list_scenarios
)
from .admin import get_tracks, get_track, get_stats
from stubo import version
from stubo.model.cmds import verbs
from stubo.model.stub import Stub
from stubo.exceptions import StuboException, exception_response
from stubo.utils import (
    asbool, get_hostname, convert_to_script, pretty_format, human_size,
    compact_traceback_info
)
from stubo.utils.track import TrackRequest
from stubo.utils.command_queue import InternalCommandQueue

DummyModel = ObjectDict

log = logging.getLogger(__name__)

# based on http://lbolla.info/blog/2013/01/22/blocking-tornado
def stubo_async(f):
    @tornado.web.asynchronous
    @wraps(f)
    def wrapper(*args, **kwargs):
        self = args[0]  # closure for request handler
        EXECUTOR = self.settings['executor']

        def callback(future):
            err = future.exception()
            if not err:
                stubo_response = future.result() or ""
            else:
                stubo_response = {
                    'version': version
                }
                if isinstance(err, StuboException):
                    stubo_response['error'] = {
                        'code': err.code,
                        'message': err.title
                    }
                    if hasattr(err, 'traceback'):
                        stubo_response['error']['traceback'] = err.traceback

                    self.set_status(err.code)
                else:
                    status = self.get_status()
                    stubo_response['error'] = dict(code=500,
                                                   message=u'{0}: {1}'.format(err.__class__.__name__,
                                                                              str(err)))
                    if not status or status == 200:
                        # if error has not been set use internal server error
                        self.set_status(500)
                    if hasattr(future, '_traceback'):
                        stubo_response['error']['traceback'] = compact_traceback_info(future._traceback)

            self.write(stubo_response)
            self.set_header('x-stubo-version', version)

            def _finish_request():
                self.finish()

            delay = 0
            if hasattr(self, 'track'):
                # Note: stubo_response being set as an attribute of the request 
                # as self._write_buffer (set by self.write()) is cleared on 
                # 304 responses
                self.track.stubo_response = stubo_response
                delay = self.track.get('delay')

            if delay:
                loop = tornado.ioloop.IOLoop.instance()
                loop.add_timeout(datetime.timedelta(milliseconds=delay),
                                 _finish_request)
            else:
                _finish_request()

        EXECUTOR.submit(
            partial(f, *args, **kwargs)
        ).add_done_callback(
            lambda future: tornado.ioloop.IOLoop.instance().add_future(
                future, callback))

    return wrapper


def get_arg(handler, arg):
    value = handler.get_argument(arg, None)
    if not value:
        raise exception_response(400,
                                 title="'{0}' parameter not supplied.".format(arg))
    return value


def get_args(handler, arg):
    value = handler.get_arguments(arg)
    if not value:
        raise exception_response(400, title="'{0}' param not supplied.".format(
            arg))
    return value


def get_scenario_arg(handler):
    return get_arg(handler, 'scenario')


def get_session_arg(handler):
    return get_arg(handler, 'session')


def command_handler_request(cmd_file_url, request, static_path):
    cmd_file_url = unquote(cmd_file_url)
    log.debug(u'command_handler_request: cmd_file={0}'.format(cmd_file_url))
    return run_command_file(cmd_file_url, request, static_path)


@stubo_async
def command_handler_form_request(handler):
    cmds = handler.get_argument('cmds', None)
    cmd_file_url = handler.get_argument('cmdfile', None)
    if not (cmds or cmd_file_url):
        # TODO: use form validation instead
        raise exception_response(400,
                                 title="'cmds' or 'cmdFile' parameter not supplied.")

    log.debug(u'command_handler_form_request: cmd_file={0},cmds={1}'.format(
        cmd_file_url, cmds))
    if cmd_file_url:
        request = DummyModel(protocol=handler.request.protocol,
                             host=handler.request.host,
                             arguments=handler.request.arguments)
        response = run_command_file(cmd_file_url, request,
                                    handler.settings['static_path'])
    elif cmds:
        response = run_commands(handler, cmds)
    links = dict((k, v) for k, v in response['data'].get('export_links', []))
    return handler.render_string("commands.html", page_title='Commands',
                                 executed=response['data'].get('executed_commands'),
                                 export_links=links)


@stubo_async
def export_stubs_request(handler):
    scenario_name = get_scenario_arg(handler)
    handler.track.scenario = scenario_name
    response = export_stubs(handler, scenario_name)
    html = asbool(handler.get_argument('html', False))
    if html:
        payload = response['data']
        title = 'Exported files for Scenario'
        if 'runnable' in payload:
            title = 'Exported files for Runnable Scenario'
        response = handler.render_string("export_stubs.html",
                                         page_title=title, **payload)
    return response


@stubo_async
def list_stubs_request(handler, html=False):
    scenario_name = get_scenario_arg(handler)
    if not html:
        handler.track.scenario = scenario_name
    response = list_stubs(handler, scenario_name,
                          handler.get_argument('host', None)).get('data')
    if html:
        stubs = response['stubs']
        response['stubs'] = [Stub(x, scenario_name) for x in stubs]
        response = handler.render_string("list_stubs.html",
                                         pretty_format=pretty_format,
                                         page_title='Stubs',
                                         indent=int(handler.get_argument('indent', 4)),
                                         **response)
    return response


@stubo_async
def list_scenarios_request(handler):
    host = handler.get_argument('host', get_hostname(handler.request))
    return list_scenarios(host)


@stubo_async
def stub_count_request(handler):
    host = handler.get_argument('host', get_hostname(handler.request))
    return stub_count(host, handler.get_argument('scenario', None))


from stubo.model.db import Scenario
from stubo.cache import Cache
from stubo.service.api import end_session


@stubo_async
def rename_scenario(handler, scenario_name, new_name):
    """
    Renames specified scenario, renames Stubs, reloads cache
    :param handler: TrackRequest handler
    :param scenario_name: <string> scenario name
    :param new_name: <string> new scenario name
    :return: <tuple> containing status code and message that will be returned
    """
    response = {
        'version': version
    }

    scenario = Scenario()
    # getting hostname
    host = handler.get_argument('host', get_hostname(handler.request))
    # full names hostname:scenario_name
    full_scenario_name = "{0}:{1}".format(host, scenario_name)
    new_full_scenario_name = "{0}:{1}".format(host, new_name)
    # getting scenario object
    scenario_obj = scenario.get(full_scenario_name)
    # checking if scenario exist, if not - quit
    if scenario_obj is None:
        handler.set_status(400)
        handler.track.scenario = scenario_name
        response['error'] = "Scenario not found. Name provided: {0}, host checked: {1}.".format(scenario_name, host)
        log.debug("Scenario not found. Name provided: {0}, host checked: {1}.".format(scenario_name, host))
        return response

    # renaming scenario and all stubs, getting a dict with results
    try:
        response = scenario.change_name(full_scenario_name, new_full_scenario_name)
    except Exception as ex:
        handler.set_status()
        log.debug("Failed to change scenario name, got error: %s" % ex)
        response['error']['database'] = "Failed to change scenario name, got error: %s" % ex
    try:

        cache = Cache(host)
        # change cache
        scenario_sessions = cache.get_sessions_status(scenario_name)
        # scenario sessions contains tuples [(u'myscenario_session2_1', u'dormant'), ....]
        session_info = []

        cache.delete_caches(scenario_name)

        # rebuild cache
        for session_name, mode in scenario_sessions:
            cache.create_session_cache(new_name, session_name)
            session_info.append({'name': session_name})
            # sessions after creation go into playback mode, ending them
            end_session(handler, session_name)

        response['Remapped sessions'] = session_info
    except Exception as ex:
        log.debug("Failed to repopulate cache, got error: %s" % ex)
        response['error']['cache'] = "Failed to repopulate cache, got error: %s" % ex
    return response


@stubo_async
def delete_stubs_request(handler):
    return delete_stubs(handler,
                        scenario_name=handler.get_argument('scenario', None),
                        host=handler.get_argument('host', None),
                        force=asbool(handler.get_argument('force', False)))


@stubo_async
def get_response_request(handler):
    session_name = handler.get_argument('session', None)
    request = handler.request

    def get_session_from_headers():
        session_name = request.headers.get('Stubo-Request-Session')
        if session_name:
            return session_name

            # NOTE: legacy support for session_name = stb_scenario + '_' + stb_session
        session = [(k, v) for k, v in request.headers.iteritems() \
                   if 'stb_session' in k or 'Stb_session' in k]
        scenario = [(k, v) for k, v in request.headers.iteritems() \
                    if 'stb_scenario' in k or 'Stb_scenario' in k]
        if not session:
            raise exception_response(400,
                                     title="session not supplied in headers.")
        if not scenario:
            raise exception_response(400,
                                     title="scenario parameter not supplied in headers.")
        _, session_name = session[0]
        scenario_key, scenario_name = scenario[0]
        session_name = '{0}_{1}'.format(scenario_name, session_name)
        # put into standard key for tracker display
        handler.track.request_headers['Stubo-Request-Session'] = session_name
        return session_name

    if not session_name:
        session_name = get_session_from_headers()
        if not session_name:
            raise exception_response(400, title="session not found in headers.")

    handler.track.function = 'get/response'
    log.debug('Found session: {0}, for route: {1}'.format(session_name,
                                                          request.path))
    return get_response(handler, session_name)


@stubo_async
def begin_session_request(handler):
    scenario = handler.track.scenario = get_scenario_arg(handler)
    session = get_session_arg(handler)
    mode = handler.get_argument('mode', None)
    warm_cache = asbool(handler.get_argument('warm_cache', False))
    if not mode:
        raise exception_response(400,
                                 title="'mode' of playback or record required")
    return begin_session(handler, scenario, session, mode,
                         handler.get_argument('system_date', None), warm_cache)


@stubo_async
def end_session_request(handler):
    session = get_session_arg(handler)
    return end_session(handler, session)


@stubo_async
def end_sessions_request(handler):
    scenario_name = get_scenario_arg(handler)
    return end_sessions(handler, scenario_name)


@stubo_async
def put_stub_request(handler):
    session = get_session_arg(handler)
    delay_policy = handler.get_argument('delay_policy', None)
    stateful = asbool(handler.get_argument('stateful', True))
    recorded = handler.get_argument('stub_created_date', None)
    module_name = handler.get_argument('ext_module', None)
    if not module_name:
        # legacy
        module_name = handler.get_argument('stubbedSystem', None)

    recorded_module_system_date = handler.get_argument('stubbedSystemDate',
                                                       None)
    priority = int(handler.get_argument('priority', -1))
    return put_stub(handler, session, delay_policy=delay_policy,
                    stateful=stateful, priority=priority, recorded=recorded,
                    module_name=module_name,
                    recorded_module_system_date=recorded_module_system_date)


@stubo_async
def put_module_request(request):
    names = request.get_arguments('name')
    log.debug('names: {0}'.format(names))
    return put_module(request, names)


@stubo_async
def put_setting_request(handler):
    setting = handler.get_argument('setting')
    value = handler.get_argument('value')
    host = handler.get_argument('host', get_hostname(handler.request))
    return put_setting(handler, setting, value, host)


@stubo_async
def get_setting_request(handler):
    setting = handler.get_argument('setting', None)
    host = handler.get_argument('host', get_hostname(handler.request))
    return get_setting(handler, host, setting)


@stubo_async
def delete_module_request(handler):
    names = handler.get_arguments('name')
    log.debug('names: {0}'.format(names))
    cmdq = InternalCommandQueue()
    for name in names:
        # Note: delete and unload from all subordinates not just the executing one
        cmdq.add(handler.track.host, 'delete/module?name={0}'.format(name))
    return delete_module(handler.request, names)


@stubo_async
def delete_modules_request(handler):
    result = list_module(handler, None)
    names = result['data']['info'].keys()
    log.debug('names: {0}'.format(names))
    cmdq = InternalCommandQueue()
    for name in names:
        # Note: delete and unload from all subordinates not just the executing one
        cmdq.add(handler.track.host, 'delete/module?name={0}'.format(name))
    return delete_module(handler.request, names)


@stubo_async
def list_module_request(handler):
    names = handler.get_arguments('name')
    log.debug('names: {0}'.format(names))
    return list_module(handler, names)


@stubo_async
def delay_policy_request(handler):
    request = handler.request
    doc = dict((key, value[0]) for key, value in request.arguments.iteritems())
    return update_delay_policy(handler, doc)


@stubo_async
def get_delay_policy_request(handler):
    name = handler.get_argument('name', None)
    cache = handler.get_argument('cache', 'main')
    return get_delay_policy(handler, name, cache)


@stubo_async
def delete_delay_policy_request(handler):
    names = handler.get_arguments('name', None)
    return delete_delay_policy(handler, names)


@stubo_async
def status_request(handler):
    return get_status(handler)


@stubo_async
def analytics_request(handler):
    status = get_status(handler)
    return handler.render_string("analytics.html",
                                 client_data=convert_to_script(status))


@stubo_async
def manage_request(handler):
    response = manage_request_api(handler)
    return handler.render_string("manage.html", page_title='Manage', **response)


@stubo_async
def tracker_request(handler):
    http_req = handler.request
    host = http_req.host.split(":")[0]
    scenario_filter = handler.get_argument('scenario_filter', '')
    session_filter = handler.get_argument('session_filter', '')
    start_time = handler.get_argument('start_time', '')
    latency = int(handler.get_argument('latency', 0))
    show_only_errors = asbool(handler.get_argument("show_only_errors",
                                                   False))
    all_hosts = asbool(handler.get_argument("all_hosts", False))
    function = handler.get_argument('function', 'all')
    skip = int(handler.get_argument('skip', 0))
    limit = int(handler.get_argument('limit', 100))

    results = get_tracks(handler, scenario_filter, session_filter, show_only_errors, skip,
                         limit, start_time, latency, all_hosts, function)
    total_tracks = results.count()
    log.debug('track count: {0}'.format(total_tracks))

    def format_response(stubo_response):
        if isinstance(stubo_response, dict) and 'version' in stubo_response:
            # assume it's one of ours
            if 'data' in stubo_response:
                stubo_response = stubo_response['data'].get('message')
            elif 'error' in stubo_response:
                error = stubo_response['error']
                if isinstance(error, dict):
                    stubo_response = 'message: <em>{0}</em>'.format(
                        error.get('message'))
                else:
                    stubo_response = error

        if not stubo_response:
            stubo_response = ""
        return stubo_response

    uri = handler.request.uri
    urlargs = parse_qs(urlparse(uri).query)
    urlargs.pop('limit', None)
    urlargs.pop('skip', None)
    query = '&'.join('{0}={1}'.format(k, v[0]) for k, v in urlargs.items())

    def pagination(total_tracks, skip, limit, url_params):
        page_no = int(skip / float(limit)) + 1
        total_pages = int(float(total_tracks) / limit) + 1
        page_class = 'disabled' if page_no >= total_pages else ''
        previous_page_class = 'disabled' if page_no <= 1 else ''
        if page_no > 1:
            prev_pg = '<a href="/tracker?skip=' + str(skip - limit) + \
                      '&limit=' + str(limit) + '&' + url_params + \
                      '">&lt;&lt;&lt;</a>'
        else:
            prev_pg = ""
        if page_no < total_pages:
            next_pg = '<a href="/tracker?skip=' + str(skip + limit) + \
                      '&limit=' + str(limit) + '&' + url_params + \
                      '">&gt;&gt;&gt;</a>'
        else:
            next_pg = ""
        return """
<div class="pagination pagination-right">
{8} <span>Page {3} of {4}</span> {9}
</div>""".format(skip + limit, limit, url_params, page_no,
                 total_pages, page_class, previous_page_class, skip - limit,
                 prev_pg, next_pg)

    def get_option(option, function):
        return '<option{0}>{1}</option>'.format(' selected="selected"' \
                                                    if option == function else "", option)

    response = dict(raw_data=results,
                    scenario_filter=scenario_filter,
                    session_filter=session_filter,
                    errors_value='checked' if show_only_errors else '',
                    pagination=pagination(total_tracks, skip, limit, query),
                    start_time=start_time,
                    latency=latency or 0,
                    function_options="\n".join(get_option(x, function) \
                                               for x in ['all'] + sorted(verbs)),
                    stubo_version=version,
                    host=host,
                    format_response=format_response,
                    max_response=TrackRequest.max_response_size,
                    total=total_tracks)
    return handler.render_string("tracker.html", **response)


@stubo_async
def tracker_detail_request(handler, tracker_id):
    track_info = get_track(handler, tracker_id)
    error_message = ""
    if not track_info:
        error_message = "tracker record with id={0}, does not exist".format(
            tracker_id)
    scenario = track_info.get('scenario')
    _stub_count = stub_count(get_hostname(handler.request),
                             scenario)['data'].get('count')
    tracking_data = track_info or {}
    trace = tracking_data.pop('trace', {})
    return handler.render_string("tracker_record.html",
                                 page_title='Tracker Record',
                                 tracking_data=tracking_data,
                                 stub_count=_stub_count,
                                 error_message=error_message,
                                 pretty_format=pretty_format,
                                 client_data=convert_to_script(trace),
                                 human_size=human_size)


@stubo_async
def stats_request(handler):
    return get_stats(handler)

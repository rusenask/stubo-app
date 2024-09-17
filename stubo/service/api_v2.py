# coding=utf-8
"""
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""

from stubo.model.db import (
    Scenario, get_mongo_client, session_last_used, Tracker
)
from stubo.service.delay import Delay
from stubo.model.stub import Stub
from stubo import version
from stubo.service.api import get_response
from stubo.utils import get_hostname
from stubo.cache import Cache
from stubo.exceptions import exception_response
import logging

log = logging.getLogger(__name__)


def begin_session(handler, scenario_name, session_name, mode, system_date=None,
                  warm_cache=False):
    """
    Begins session for given scenario
    :param handler: request handler class
    :param scenario_name: scenario name
    :param session_name: session name
    :param mode: mode - record, playback
    :param system_date:
    :param warm_cache:
    :return: :raise exception_response:
    """
    log.debug('begin_session')
    response = {
        'version': version
    }
    scenario_manager = Scenario()
    cache = Cache(get_hostname(handler.request))
    if cache.blacklisted():
        raise exception_response(400, title="Sorry the host URL '{0}' has been "
                                            "blacklisted. Please contact Stub-O-Matic support.".format(cache.host))
    # checking whether full name (with hostname) was passed, if not - getting full name
    # scenario_name_key = "localhost:scenario_1"
    if ":" not in scenario_name:
        scenario_name_key = cache.scenario_key_name(scenario_name)
    else:
        # setting scenario full name
        scenario_name_key = scenario_name
        # removing hostname from scenario name
        scenario_name = scenario_name.split(":")[1]

    # get scenario document
    scenario_doc = scenario_manager.get(scenario_name_key)
    if not scenario_doc:
        raise exception_response(404,
                                 title='Scenario not found - {0}. To begin a'
                                       ' session - create a scenario.'.format(scenario_name_key))

    cache.assert_valid_session(scenario_name, session_name)

    if mode == 'record':
        log.debug('begin_session, mode=record')
        # check if there are any existing stubs in this scenario
        if scenario_manager.stub_count(scenario_name_key) > 0:
            err = exception_response(400,
                                     title='Scenario ({0}) has existing stubs, delete them before '
                                           'recording or create another scenario!'.format(scenario_name_key))
            raise err

        scenario_id = scenario_doc['_id']
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

        recordings = cache.get_sessions_status(scenario_name,
                                               status='record',
                                               local=False)
        if recordings:
            raise exception_response(400, title='Scenario recordings taking '
                                                'place - {0}. Found the '
                                                'following record sessions: {1}'.format(scenario_name_key, recordings))
        cache.create_session_cache(scenario_name, session_name, system_date)
        if warm_cache:
            # iterate over stubs and call get/response for each stub matchers
            # to build the request & request_index cache
            # reset request_index to 0
            log.debug("warm cache for session '{0}'".format(session_name))
            scenario_manager = Scenario()
            for payload in scenario_manager.get_stubs(scenario_name_key):
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


def update_delay_policy(handler):
    """Record delay policy in redis to be available globally to any
    users for their sessions.
    Creates a delay policy. Examples:
        { “name”: “delay_name”,
          “delay_type”: “fixed”,
          “milliseconds”: 50}
        or
        { “name”: “delay_name”,
          “delay_type”: “normalvariate”,
          “mean”: “mean_val”,
          “stddev”: “val”}

    :return: :response: <dict> with status code (400 - bad request, 409 - something is wrong, conflict of values,
    200 - delay policy updated, 201 - delay policy created)
    """
    cache = Cache(get_hostname(handler.request))
    response = {
        'version': version
    }
    doc = handler.request.arguments
    err = None
    status_code = None
    # checking name
    if 'name' not in doc:
        err = "'name' parameter not found in request"
        status_code = 400
    else:
        if " " in doc['name']:
            err = "'name' parameter should not contain white space"
            status_code = 400

    if 'delay_type' not in doc:
        err = "'delay_type' param not found in request"
        status_code = 400
    if not err:
        # checking for fixed delays
        if doc['delay_type'] == 'fixed':
            if 'milliseconds' not in doc:
                err = "'milliseconds' param is required for 'fixed' delays"
                status_code = 409

        # checking for normalvariate delays
        elif doc['delay_type'] == 'normalvariate':
            if 'mean' not in doc or 'stddev' not in doc:
                err = "'mean' and 'stddev' params are required for " \
                      "'normalvariate' delays"
                status_code = 409

        # checking for weighted delays
        elif doc['delay_type'] == 'weighted':
            if 'delays' not in doc:
                err = "'delays' are required for 'weighted' delays"
                status_code = 409
            else:
                try:
                    Delay.parse_args(doc)
                except Exception, e:
                    err = 'Unable to parse weighted delay arguments: {0}'.format(str(e))
                    status_code = 409
        # delay_type not known, creating error
        else:
            err = 'Unknown delay type: {0}'.format(doc['delay_type'])
            status_code = 400
    # if errors are present - add key error
    if err:
        response['error'] = err
        response['status_code'] = status_code
        return response
    # no errors were detected - updating cache and returning results
    result = cache.set_delay_policy(doc['name'], doc)

    if result:
        updated = 'new'
        status_code = 201
    else:
        updated = 'updated'
        status_code = 200

    response['status_code'] = status_code
    response['data'] = {
        'message': 'Put Delay Policy Finished',
        'name': doc['name'],
        'delay_type': doc['delay_type'],
        'delayPolicyRef': '/stubo/api/v2/delay-policy/objects/%s' % doc['name'],
        'status': updated
    }
    return response


def get_delay_policy(handler, name, cache_loc):
    """
    Gets specified or all delays, returns dictionary

    :param handler: RequestHandler (or TrackRequest, BaseHandler, etc..)
    :param name: Delay name, if None is passed - gets all delays
    :param cache_loc: Cache location, usually just 'main'
    :return: dictionary of dictionaries with scenario information and reference URI and status_code for response
    """
    cache = Cache(get_hostname(handler.request))
    response = {
        'version': version
    }
    status_code = 200
    delays = cache.get_delay_policy(name, cache_loc)

    # if delay policy not found but name was specified, return error message
    if delays is None and name is not None:
        status_code = 404
        response['error'] = "Delay policy '%s' not found" % name
        return response, status_code

    # list for storing delay objects since cache.get_delay_policy(name, cache_loc) returns a dict
    delay_list = []
    # adding references
    if name is None:
        if delays is not None:
            # All stored delays should be returned
            for k, v in delays.items():
                v['delayPolicyRef'] = "/stubo/api/v2/delay-policy/objects/%s" % k
                delay_list.append(v)
        else:
            # Returning empty dict
            delays = {}
            delay_list.append(delays)
    else:
        delays['delayPolicyRef'] = "/stubo/api/v2/delay-policy/objects/%s" % name
        delay_list.append(delays)

    response['data'] = delay_list
    return response, status_code

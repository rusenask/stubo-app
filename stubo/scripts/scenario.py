"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import json
from argparse import ArgumentParser
import stubo.service.api as api
from stubo.testing import DummyRequestHandler
from stubo.utils import init_mongo, start_redis


def list_stubs():
    parser = ArgumentParser(
        description="List scenario stubs"
    )
    parser.add_argument('--host', dest='host', default='localhost',
                        help="hostname")
    parser.add_argument('-s', '--scenario', dest='scenario',
                        help="scenario name")

    args = parser.parse_args()
    return list_stubs_core(args.host, args.scenario)


def list_stubs_core(host, scenario):
    init_mongo()
    response = api.list_stubs(DummyRequestHandler(), scenario, host)
    if response:
        result = json.dumps(response, sort_keys=True, indent=4,
                            separators=(',', ': '))
    else:
        result = 'no stubs found'
    return result


def stub_count():
    parser = ArgumentParser(
        description="Scenario stub count"
    )
    parser.add_argument('--host', dest='host', default='localhost',
                        help="hostname")
    parser.add_argument('-s', '--scenario', dest='scenario',
                        help="scenario name")
    args = parser.parse_args()
    init_mongo()
    print api.stub_count(args.host, args.scenario)


def export_stubs():
    parser = ArgumentParser(
        description="Export scenario stubs"
    )
    parser.add_argument('-s', '--scenario', dest='scenario',
                        help="scenario name")
    parser.add_argument('-p', '--static-path-dir', dest='static_path',
                        default=None, help="Path to static dir to export files. If not "
                                           "specified the output will placed in a tmp dir.")

    args = parser.parse_args()
    scenario = args.scenario
    if args.static_path:
        request = DummyRequestHandler(static_path=args.static_path)
    else:
        request = DummyRequestHandler()
    init_mongo()
    subordinate, main = start_redis({})
    response = api.export_stubs(request, scenario)
    print json.dumps(response, sort_keys=True, indent=4, separators=(',', ': '))

"""
    stubo
    ~~~~~
    
    Stub-O-Matic - Enable automated testing by maining system dependencies. 
    Use when reality is simply not good enough.
     
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import os
import sys

version = "0.6.6"
version_info = tuple(version.split('.'))


def stubo_path():
    # Find folder that this module is contained in
    module = sys.modules[__name__]
    return os.path.dirname(os.path.abspath(module.__file__))


def static_path(*args):
    return os.path.join(stubo_path(), 'static', *args)

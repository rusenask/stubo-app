"""  
    :copyright: (c) 2015 by OpenCredo.
    :license: GPLv3, see LICENSE for more details.
"""
import logging
import json

log = logging.getLogger(__name__)

redis_server = None
redis_main_server = None


def get_redis_subordinate():
    return redis_server


def get_redis_main():
    return redis_main_server


class QueueIterator(object):
    def __init__(self, queue, start=0):
        self.q = queue
        self.iter = iter(range(start, len(self.q)))

    def __iter__(self):
        return self

    def next(self):
        """
        Retrieve the next message in the queue.
        """
        i = self.iter.next()
        return self.q.get_item(i)


class Queue(object):
    def __init__(self, name, server=None):
        self.name = name
        self.server = server or redis_server

    def size(self):
        return sum([len(self.server.lindex(self.name, m)) for m in range(0, len(self))])

    def put_raw(self, data):
        self.server.rpush(self.name, data)

    def get_raw(self):
        return self.server.lpop(self.name)

    def put(self, msg):
        json_msg = json.dumps(msg)
        self.server.rpush(self.name, json_msg)

    def get(self, timeout=None):
        if timeout:
            result = self.server.blpop(self.name, timeout)
            if result:
                result = result[1]
        else:
            result = self.server.lpop(self.name)
        if result:
            result = json.loads(result)
        return result

    def get_item(self, index):
        msg = self.server.lindex(self.name, index)
        if msg:
            msg = json.loads(msg)
        return msg

    def __len__(self):
        return self.server.llen(self.name)

    def __iter__(self):
        return QueueIterator(self)

    def delete(self):
        return self.server.delete(self.name)


class String(object):
    def __init__(self, server=None, ttl=None):
        self.server = server or redis_server
        self.ttl = ttl or 24 * 7 * 3600  # 7 days  

    def set_raw(self, key, msg):
        self.server.setex(key, msg, self.ttl)

    def get_raw(self, key):
        return self.server.get(key)

    def get(self, key):
        try:
            return json.loads(self.get_raw(key))
        except TypeError:
            return None

    def set(self, key, msg):
        self.set_raw(key, json.dumps(msg))

    def delete(self, key):
        return self.server.delete(key)


class Hash(object):
    def __init__(self, server=None):
        self.server = server or redis_server

    def get(self, name, key):
        try:
            return json.loads(self.get_raw(name, key))
        except TypeError:
            return None

    def set(self, name, key, msg):
        return self.set_raw(name, key, json.dumps(msg))

    def set_raw(self, name, key, msg):
        return self.server.hset(name, key, msg)

    def incr(self, name, key, amount=1):
        return self.server.hincrby(name, key, amount=amount)

    def get_raw(self, name, key):
        return self.server.hget(name, key)

    def get_all_raw(self, name):
        return self.server.hgetall(name)

    def get_all(self, name):
        return dict((k, json.loads(v)) for k, v in self.server.hgetall(
            name).iteritems())

    def keys(self, name):
        return self.server.hkeys(name)

    def values(self, name):
        return [json.loads(x) for x in self.server.hvals(name)]

    def delete(self, name, *keys):
        """
        delete the hash key
        """
        return self.server.hdel(name, *keys)

    def remove(self, name):
        """
        delete the hash
        """
        return self.server.delete(name)

    def exists(self, name, key):
        return self.server.hexists(name, key)


def get_queue(q=None):
    q = q or Queue
    return q

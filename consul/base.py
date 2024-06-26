import abc
import base64
import collections
import enum
import json
import logging
import os
import re
import threading
import time
import warnings

import six
from six.moves import urllib

log = logging.getLogger(__name__)


class ConsulException(Exception):
    pass


class ACLDisabled(ConsulException):
    pass


class ACLPermissionDenied(ConsulException):
    pass


class NotFound(ConsulException):
    pass


class Timeout(ConsulException):
    pass


class BadRequest(ConsulException):
    pass


class ClientError(ConsulException):
    """Encapsulates 4xx Http error code"""
    pass


#
# Convenience to define checks

class Check(object):
    """
    There are three different kinds of checks: script, http and ttl
    """

    @classmethod
    def script(klass, args, interval):
        """
        Run the script *args* every *interval* (e.g. "10s") to peform health
        check
        """
        if isinstance(args, six.string_types) \
                or isinstance(args, six.binary_type):
            warnings.warn(
                "Check.script should take a list of arg", DeprecationWarning)
            args = ["sh", "-c", args]
        return {'args': args, 'interval': interval}

    @classmethod
    def http(klass, url, interval, timeout=None, deregister=None, header=None,
             tls_skip_verify=None):
        """
        Perform a HTTP GET against *url* every *interval* (e.g. "10s") to
        perform health check with an optional *timeout* and optional
        *deregister* after which a failing service will be automatically
        deregistered. Optional parameter *header* specifies headers sent in
        HTTP request. *header* parameter is in form of map of lists of
        strings, e.g. {"x-foo": ["bar", "baz"]}. Optional parameter
        *tls_skip_verify* allow to skip TLS certificate verification.
        """
        ret = {'http': url, 'interval': interval}
        if timeout:
            ret['timeout'] = timeout
        if deregister:
            ret['DeregisterCriticalServiceAfter'] = deregister
        if header:
            ret['header'] = header
        if tls_skip_verify:
            ret['TLSSkipVerify'] = tls_skip_verify
        return ret

    @classmethod
    def tcp(klass, host, port, interval, timeout=None, deregister=None):
        """
        Attempt to establish a tcp connection to the specified *host* and
        *port* at a specified *interval* with optional *timeout* and optional
        *deregister* after which a failing service will be automatically
        deregistered.
        """
        ret = {
            'tcp': '{host:s}:{port:d}'.format(host=host, port=port),
            'interval': interval
        }
        if timeout:
            ret['timeout'] = timeout
        if deregister:
            ret['DeregisterCriticalServiceAfter'] = deregister
        return ret

    @classmethod
    def ttl(klass, ttl):
        """
        Set check to be marked as critical after *ttl* (e.g. "10s") unless the
        check is periodically marked as passing.
        """
        return {'ttl': ttl}

    @classmethod
    def docker(klass, container_id, shell, script, interval, deregister=None):
        """
        Invoke *script* packaged within a running docker container with
        *container_id* at a specified *interval* on the configured
        *shell* using the Docker Exec API.  Optional *register* after which a
        failing service will be automatically deregistered.
        """
        ret = {
            'docker_container_id': container_id,
            'shell': shell,
            'script': script,
            'interval': interval
        }
        if deregister:
            ret['DeregisterCriticalServiceAfter'] = deregister
        return ret

    @classmethod
    def grpc(klass, grpc, interval, deregister=None):
        """
        grpc (string: "") - Specifies a gRPC check's endpoint that
        supports the standard gRPC health checking protocol.
        The state of the check will be updated at the given
        Interval by probing the configured endpoint. Add the
        service identifier after the gRPC check's endpoint in the
        following format to check for a specific service instead of
        the whole gRPC server /:service_identifier.
        """
        ret = {
            'GRPC': grpc,
            'Interval': interval
        }
        if deregister:
            ret['DeregisterCriticalServiceAfter'] = deregister
        return ret

    @classmethod
    def _compat(
            self,
            script=None,
            interval=None,
            ttl=None,
            http=None,
            timeout=None,
            deregister=None):

        if not script and not http and not ttl:
            return {}

        log.warning(
            'DEPRECATED: use consul.Check.script/http/ttl to specify check')

        ret = {'check': {}}

        if script:
            assert interval and not (ttl or http)
            ret['check'] = {'script': script, 'ttl': interval}
        if ttl:
            assert not (interval or script or http)
            ret['check'] = {'ttl': ttl}
        if http:
            assert interval and not (script or ttl)
            ret['check'] = {'http': http, 'interval': interval}
        if timeout:
            assert http
            ret['check']['timeout'] = timeout

        # if deregister:
        #     ret['check']['DeregisterCriticalServiceAfter'] = deregister

        return ret


Response = collections.namedtuple(
    'Response', ['code', 'headers', 'body', 'content'])


#
# Conveniences to create consistent callback handlers for endpoints

class CB(object):
    @classmethod
    def _status(klass, response, allow_404=True):
        # status checking
        if 400 <= response.code < 500:
            if response.code == 400:
                raise BadRequest('%d %s' % (response.code, response.body))
            elif response.code == 401:
                raise ACLDisabled(response.body)
            elif response.code == 403:
                raise ACLPermissionDenied(response.body)
            elif response.code == 404:
                if not allow_404:
                    raise NotFound(response.body)
            else:
                raise ClientError("%d %s" % (response.code, response.body))
        elif 500 <= response.code < 600:
            raise ConsulException("%d %s" % (response.code, response.body))

    @classmethod
    def bool(klass):
        # returns True on successful response
        def cb(response):
            CB._status(response)
            return response.code == 200

        return cb

    @classmethod
    def json(
            klass,
            map=None,
            allow_404=True,
            one=False,
            decode=False,
            is_id=False,
            index=False):
        """
        *map* is a function to apply to the final result.

        *allow_404* if set, None will be returned on 404, instead of raising
        NotFound.

        *index* if set, a tuple of index, data will be returned.

        *one* returns only the first item of the list of items. empty lists are
        coerced to None.

        *decode* if specified this key will be base64 decoded.

        *is_id* only the 'ID' field of the json object will be returned.
        """

        def cb(response):
            CB._status(response, allow_404=allow_404)
            if response.code == 404:
                return response.headers.get('X-Consul-Index'), None

            data = json.loads(response.body)

            if decode:
                for item in data:
                    if item.get(decode) is not None:
                        item[decode] = base64.b64decode(item[decode])
            if is_id:
                data = data['ID']
            if one:
                if not data:
                    data = None
                if data is not None:
                    data = data[0]
            if map:
                data = map(data)
            if index:
                return response.headers['X-Consul-Index'], data
            return data

        return cb

    @classmethod
    def binary(klass):
        """
        This method simply returns response body, usefull for snapshot
        """

        def cb(response):
            CB._status(response)
            return response.content

        return cb


#
# Convenience to define weight

class Weight(object):
    """
    There object for set weights parameters like this
    {'passing': 100, 'warning': 100}
    """

    @classmethod
    def weights(cls, passing, warning):
        return {'passing': passing, 'warning': warning}


class ConsistencyMode(enum.Enum):
    """
    Most of the read query endpoints support multiple levels of consistency.
    Since no policy will suit all clients' needs,
    these consistency modes allow the user to have the ultimate say in how
    to balance the trade-offs inherent in a distributed system.
    The three read modes are:

    *DEFAULT* - If not specified, the default is strongly consistent in almost all cases.
    However, there is a small window in which a new leader may be elected during which
    he old leader may service stale values. The trade-off is fast reads but potentially stale values.
    The condition resulting in stale reads is hard to trigger, and most clients should
    not need to worry about this case. Also, note that this race condition only applies to reads, not writes.

    *CONSISTENT* - This mode is strongly consistent without caveats.
    It requires that a leader verify with a quorum of peers that it is still leader.
    This introduces an additional round-trip to all server nodes. The trade-off is increased
    latency due to an extra round trip. Most clients should not use
    this unless they cannot tolerate a stale read.

    *STALE* - This mode allows any server to service the read regardless of whether it is the leader.
    This means reads can be arbitrarily stale; however, results are generally
    consistent to within 50 milliseconds of the leader. The trade-off is very fast and scalable reads
    with a higher likelihood of stale values. Since this mode allows reads without a leader, a cluster
    that is unavailable will still be able to respond to queries.
    """

    DEFAULT = 'default'
    CONSISTENT = 'consistent'
    STALE = 'stale'


class ConsulCacheBase(metaclass=abc.ABCMeta):
    """
    Base consul cache implements, that support blocking query.

    *cache* is a dict that consist cache values by key.

    *callbacks* is a list of methods, that will be invoked when cache updated

    *watch_seconds* is the maximum duration for the blocking request.

    *index* is the current Consul index, suitable for making subsequent
    calls to wait for changes since this query was last run.
    """

    def __init__(self, watch_seconds: str, backoff_delay_seconds: int, caller: str):
        self.cache = dict()
        self.callbacks = []
        self.caller = caller
        self.watch_seconds = watch_seconds
        self.backoff_delay_seconds = backoff_delay_seconds
        self.index = None
        self._running = True
        self._cache_thread = threading.Thread(
            target=self._update_cache,
            name='update_consul_cache_thread',
            daemon=True)

    def start(self):
        self._cache_thread.start()

    def stop(self):
        self._running = False

    def add_listener(self, callback, trigger_current=False):
        self.callbacks.append(callback)
        log.debug(f'Registered callback: {self.callbacks}')
        if trigger_current:
            for key, value in self.cache.items():
                callback(key, value)

    @abc.abstractmethod
    def _update_cache(cls):
        pass


class HealthCache(ConsulCacheBase):
    """
    Consul health service cache.

    *service* is a service name for getting healths info.

    *passing* specifies that the server should return only nodes
    with all checks in the passing state. This can be used to avoid
    additional filtering on the client side.
    """

    def __init__(self,
                 health_client,
                 watch_seconds: str,
                 backoff_delay_seconds: int,
                 service: str,
                 passing: bool,
                 dc: str,
                 caller: str):
        super().__init__(watch_seconds, backoff_delay_seconds, caller)
        self.service = service
        self.health_client = health_client
        self.passing = passing
        self.dc = dc.lower()
        self.index, service_health = health_client.service(
            service=self.service,
            passing=self.passing,
            dc=self.dc,
            caller=self.caller
        )
        self.cache = {self.service: service_health}

    def _update_cache(self):
        while self._running:
            try:
                params = {
                    'service': self.service,
                    'passing': self.passing,
                    'index': self.index,
                    'wait': self.watch_seconds,
                    'dc': self.dc,
                    'caller': self.caller
                }
                log.debug(f'Param for health query: {params}')
                self.index, values = self.health_client.service(**params)
                old_cache = self.cache
                self.cache = {self.service: values}
                if self.callbacks and self._running:
                    for key, old_value in old_cache.items():
                        new_value = self.cache.get(key, None)
                        for callback in self.callbacks:
                            callback(key, new_value)
            except Exception as e:
                log.error(f'Some problem with update consul cache: {e}. Will retry in {self.backoff_delay_seconds}s')
                time.sleep(self.backoff_delay_seconds)


class KVCache(ConsulCacheBase):
    """
    Consul key-value cache.

    *path* is a key for getting value

    *consistency_mode* sets the consistency mode to use by default for all reads
    that support the consistency option. It's still possible to override
    this by passing explicitly for a given request. *consistency* can be
    either 'default', 'consistent' or 'stale'.

    *total_timeout* is a ttl of HTTP session. Should be more than *watch_seconds*

    *cache_initial_warmup_timeout* is a ttl of HTTP session for initialize cache.
    May be None, will use *total_timeout* insted
    """

    def __init__(self,
                 kv_client,
                 watch_seconds: str,
                 backoff_delay_seconds: int,
                 path: str,
                 total_timeout: int,
                 recurse: bool,
                 consistency_mode: ConsistencyMode,
                 caller: str,
                 cache_initial_warmup_timeout=None):
        super().__init__(watch_seconds, backoff_delay_seconds, caller)
        self.kv_client = kv_client
        self.path = path
        self.recurse = recurse
        self.consistency_mode = consistency_mode.value
        self.total_timeout = total_timeout
        self.cache_initial_warmup_timeout = cache_initial_warmup_timeout
        self.index, kv = kv_client.get(
            key=path,
            recurse=recurse,
            total_timeout=self._get_warmup_timeout(),
            caller=self.caller
        )
        self.cache = {self.path: kv}

    def _get_warmup_timeout(self):
        if self.cache_initial_warmup_timeout:
            return self.cache_initial_warmup_timeout
        return self.total_timeout

    def get_value(self):
        return self.cache.get(self.path, None)

    def _update_cache(self):
        while self._running:
            try:
                params = {
                    'key': self.path,
                    'index': self.index,
                    'wait': self.watch_seconds,
                    'total_timeout': self.total_timeout,
                    'consistency': self.consistency_mode,
                    'recurse': self.recurse,
                    'caller': self.caller
                }
                log.debug(f'Param for kv query: {params}')
                self.index, values = self.kv_client.get(**params)
                old_cache = self.cache
                self.cache = {self.path: values}
                if self.callbacks and self._running:
                    for key, new_value in self.cache.items():
                        old_value = old_cache.get(key, None)
                        if old_value != new_value:
                            log.debug(f'Value was changed for key={key}. old: {old_value} new: {new_value}')
                            for callback in self.callbacks:
                                callback(key, new_value)
            except Exception as e:
                log.error(f'Some problem with update consul cache: {e}. Will retry in {self.backoff_delay_seconds}s')
                time.sleep(self.backoff_delay_seconds)


class HTTPClient(six.with_metaclass(abc.ABCMeta, object)):
    def __init__(self, host='127.0.0.1', port=8500, scheme='http',
                 verify=True, cert=None, timeout=None):
        self.host = host
        self.port = port
        self.scheme = scheme
        self.verify = verify
        self.base_uri = '%s://%s:%s' % (self.scheme, self.host, self.port)
        self.cert = cert
        self.timeout = timeout

    def uri(self, path, params=None):
        uri = self.base_uri + urllib.parse.quote(path, safe='/:')
        if params:
            uri = '%s?%s' % (uri, urllib.parse.urlencode(params))
        return uri

    @abc.abstractmethod
    def get(self, callback, path, params=None, headers=None, total_timeout=None):
        raise NotImplementedError

    @abc.abstractmethod
    def put(self, callback, path, params=None, data='', headers=None):
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, callback, path, params=None, data='', headers=None):
        raise NotImplementedError

    @abc.abstractmethod
    def post(self, callback, path, params=None, data='', headers=None):
        raise NotImplementedError


class Consul(object):
    def __init__(
            self,
            host='127.0.0.1',
            port=8500,
            token=None,
            scheme='http',
            consistency='default',
            dc=None,
            verify=True,
            cert=None,
            **kwargs):
        """
        *token* is an optional `ACL token`_. If supplied it will be used by
        default for all requests made with this client session. It's still
        possible to override this token by passing a token explicitly for a
        request.

        *consistency* sets the consistency mode to use by default for all reads
        that support the consistency option. It's still possible to override
        this by passing explicitly for a given request. *consistency* can be
        either 'default', 'consistent' or 'stale'.

        *dc* is the datacenter that this agent will communicate with.
        By default the datacenter of the host is used.

        *verify* is whether to verify the SSL certificate for HTTPS requests

        *cert* client side certificates for HTTPS requests
        """

        # TODO: Status

        if os.getenv('CONSUL_HTTP_ADDR'):
            try:
                host, port = os.getenv('CONSUL_HTTP_ADDR').split(':')
                scheme = 'http'
            except ValueError:
                try:
                    scheme, host, port = \
                        os.getenv('CONSUL_HTTP_ADDR').split(':')
                    host = host.lstrip('//')
                except ValueError:
                    raise ConsulException('CONSUL_HTTP_ADDR (%s) invalid, '
                                          'does not match <host>:<port> or '
                                          '<protocol>:<host>:<port>'
                                          % os.getenv('CONSUL_HTTP_ADDR'))
        use_ssl = os.getenv('CONSUL_HTTP_SSL')
        if use_ssl == 'true':
            scheme = 'https'
        if os.getenv('CONSUL_HTTP_SSL_VERIFY') is not None:
            verify = os.getenv('CONSUL_HTTP_SSL_VERIFY') == 'true'

        self.acl = Consul.ACL(self)
        self.agent = Consul.Agent(self)
        self.catalog = Consul.Catalog(self)
        self.config = Consul.Config(self)
        self.connect = Consul.Connect(self)
        assert consistency in ('default', 'consistent', 'stale'), \
            'consistency must be either default, consistent or state'
        self.consistency = consistency
        self.coordinate = Consul.Coordinate(self)
        self.dc = dc
        self.discovery_chain = Consul.DiscoveryChain(self)
        self.event = Consul.Event(self)
        self.health = Consul.Health(self)
        self.http = self.http_connect(host,
                                      port,
                                      scheme,
                                      verify,
                                      cert,
                                      **kwargs)
        self.kv = Consul.KV(self)
        self.operator = Consul.Operator(self)
        self.query = Consul.Query(self)
        self.scheme = scheme
        self.session = Consul.Session(self)
        self.snapshot = Consul.Snapshot(self)
        self.status = Consul.Status(self)
        self.token = os.getenv('CONSUL_HTTP_TOKEN', token)
        self.txn = Consul.Txn(self)

    class ACL(object):
        def __init__(self, agent):
            self.agent = agent
            self.tokens = Consul.ACL.Tokens(agent)
            self.legacy_tokens = Consul.ACL.LegacyTokens(agent)
            self.policy = Consul.ACL.Policy(agent)
            self.roles = Consul.ACL.Roles(agent)
            self.auth_method = Consul.ACL.AuthMethod(agent)
            self.binding_rule = Consul.ACL.BindingRule(agent)

        def self(self, token=None):
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(
                CB.json(), path='/v1/acl/token/self', headers=headers)

        def list(self, token=None):
            """
            Lists all the active ACL tokens. This is a privileged endpoint, and
            requires a management token. *token* will override this client's
            default token.  An *ACLPermissionDenied* exception will be raised
            if a management token is not used.
            """
            warnings.warn('Consul 1.4.0 deprecated',
                          DeprecationWarning)
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(
                CB.json(), path='/v1/acl/list', headers=headers)

        def info(self, acl_id, token=None):
            """
            Returns the token information for *acl_id*.
            """
            warnings.warn('Consul 1.4.0 deprecated',
                          DeprecationWarning)
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(
                CB.json(one=True),
                path='/v1/acl/info/%s' % acl_id,
                headers=headers)

        def create(self,
                   name=None,
                   type='client',
                   rules=None,
                   acl_id=None,
                   token=None):
            """
            Creates a new ACL token. This is a privileged endpoint, and
            requires a management token. *token* will override this client's
            default token.  An *ACLPermissionDenied* exception will be raised
            if a management token is not used.

            *name* is an optional name for this token.

            *type* is either 'management' or 'client'. A management token is
            effectively like a root user, and has the ability to perform any
            action including creating, modifying, and deleting ACLs. A client
            token can only perform actions as permitted by *rules*.

            *rules* is an optional `HCL`_ string for this `ACL Token`_ Rule
            Specification.

            Rules look like this::

                # Default all keys to read-only
                key "" {
                  policy = "read"
                }
                key "foo/" {
                  policy = "write"
                }
                key "foo/private/" {
                  # Deny access to the private dir
                  policy = "deny"
                }

            Returns the string *acl_id* for the new token.
            """
            warnings.warn('Consul 1.4.0 deprecated',
                          DeprecationWarning)
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token

            payload = {}
            if name:
                payload['Name'] = name
            if type:
                assert type in ('client', 'management'), \
                    'type must be client or management'
                payload['Type'] = type
            if rules:
                assert isinstance(rules, str), \
                    'Only HCL or JSON encoded strings supported for the moment'
                payload['Rules'] = rules
            if acl_id:
                payload['ID'] = acl_id

            if payload:
                data = json.dumps(payload)
            else:
                data = ''

            return self.agent.http.put(
                CB.json(is_id=True),
                path='/v1/acl/create',
                headers=headers,
                data=data)

        def update(self, acl_id, name=None, type=None, rules=None, token=None):
            """
            Updates the ACL token *acl_id*. This is a privileged endpoint, and
            requires a management token. *token* will override this client's
            default token. An *ACLPermissionDenied* exception will be raised if
            a management token is not used.

            *name* is an optional name for this token.

            *type* is either 'management' or 'client'. A management token is
            effectively like a root user, and has the ability to perform any
            action including creating, modifying, and deleting ACLs. A client
            token can only perform actions as permitted by *rules*.

            *rules* is an optional `HCL`_ string for this `ACL Token`_ Rule
            Specification.

            Returns the string *acl_id* of this token on success.
            """
            warnings.warn('Consul 1.4.0 deprecated',
                          DeprecationWarning)
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token

            payload = {'ID': acl_id}
            if name:
                payload['Name'] = name
            if type:
                assert type in ('client', 'management'), \
                    'type must be client or management'
                payload['Type'] = type
            if rules:
                assert isinstance(rules, str), \
                    'Only HCL or JSON encoded strings supported for the moment'
                payload['Rules'] = rules

            data = json.dumps(payload)

            return self.agent.http.put(
                CB.json(is_id=True),
                path='/v1/acl/update',
                headers=headers,
                data=data)

        def clone(self, acl_id, token=None):
            """
            Clones the ACL token *acl_id*. This is a privileged endpoint, and
            requires a management token. *token* will override this client's
            default token. An *ACLPermissionDenied* exception will be raised if
            a management token is not used.

            Returns the string of the newly created *acl_id*.
            """
            warnings.warn('Consul 1.4.0 deprecated',
                          DeprecationWarning)
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(
                CB.json(is_id=True),
                path='/v1/acl/clone/%s' % acl_id,
                headers=headers)

        def destroy(self, acl_id, token=None):
            """
            Destroys the ACL token *acl_id*. This is a privileged endpoint, and
            requires a management token. *token* will override this client's
            default token. An *ACLPermissionDenied* exception will be raised if
            a management token is not used.

            Returns *True* on success.
            """
            warnings.warn('Consul 1.4.0 deprecated',
                          DeprecationWarning)
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(
                CB.json(),
                path='/v1/acl/destroy/%s' % acl_id,
                headers=headers)

        def bootstrap(self, token=None):
            """
            This endpoint does a special one-time bootstrap of the ACL system,
            making the first management token if the acl.tokens.master
            configuration entry is not specified in the Consul server
            configuration and if the cluster has not been bootstrapped
            previously. This is available in Consul 0.9.1 and later, and
            requires all Consul servers to be upgraded in order to operate.
            :param token:
            :return:
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(CB.json(),
                                       path='/v1/acl/bootstrap',
                                       headers=headers)

        def replication(self, dc=None, token=None):
            """
            This endpoint returns the status of the ACL replication
            processes in the datacenter. This is intended to be used
            by operators or by automation checking to discover the
            health of ACL replication.
            :param dc:
            :header token:
            :return:
            """
            params = []
            headers = {}
            token = token or self.agent.token
            dc = dc or self.agent.dc
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            return self.agent.http.get(CB.json(),
                                       path='/v1/acl/replication',
                                       params=params,
                                       headers=headers)

        def create_translate(self, payload, token=None):
            """
            This endpoint translates the legacy rule syntax into the latest
            syntax. It is intended to be used by operators managing Consul's
            ACLs and performing legacy token to new policy migrations.
            *payload*

            agent "" {
                policy = "read"
            }

            :return:
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.post(CB.binary(),
                                        path='/v1/acl/rules/translate',
                                        headers=headers,
                                        data=payload)

        def get_translate(self, accessor_id, token=None):
            """
            This endpoint translates the legacy rules embedded within a legacy
            ACL into the latest syntax.
            :param accessor_id:
            :param token:
            :return:
            """
            path = '/v1/acl/rules/translate/%s' % accessor_id
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(CB.json(),
                                       path=path,
                                       headers=headers)

        def login(self, auth_method, bearer_token, meta=None, token=None):
            payload = {
                "AuthMethod": auth_method,
                "BearerToken": bearer_token,

            }
            if meta:
                payload['Meta'] = meta
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.post(CB.json(),
                                        path='/v1/acl/login',
                                        headers=headers,
                                        data=json.dumps(payload))

        def logout(self, token=None):
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.post(CB.json(),
                                        path='/v1/acl/logout',
                                        headers=headers)

        class Tokens(object):
            """
            The APIs are available in Consul versions 1.4.0 and later.
            """

            def __init__(self, agent=None):
                self.agent = agent

            def create(self, payload, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path='/v1/acl/token',
                                           headers=headers,
                                           data=json.dumps(payload))

            def get(self, accessor_id, token=None):
                path = '/v1/acl/token/%s' % accessor_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def self(self, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path='/v1/acl/token/self',
                                           headers=headers)

            def update(self, payload, accessor_id, token=None):
                path = '/v1/acl/token/%s' % accessor_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

            def clone(self,
                      description='',
                      token=None,
                      accessor_id=None):
                payload = {
                    "Description": description,
                }
                path = '/v1/acl/token/%s/clone' % accessor_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

            def delete(self, accessor_id, token=None):
                path = '/v1/acl/token/%s' % accessor_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers)

            def list(
                    self, policy=None, role=None, authmethod=None, token=None):
                params = []
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if policy:
                    params.append(('policy', policy))
                if role:
                    params.append(('role', role))
                if authmethod:
                    params.append(('authmethod', authmethod))
                return self.agent.http.get(CB.json(),
                                           path='/v1/acl/tokens',
                                           params=params,
                                           headers=headers)

        class LegacyTokens(object):
            def __init__(self, agent=None):
                warnings.warn(
                    'Consul 1.4.0 deprecates the legacy ACL.',
                    DeprecationWarning)
                self.agent = agent

            def list(self, token=None):
                warnings.warn('Consul 1.4.0 deprecated',
                              DeprecationWarning)
                params = []
                token = token or self.agent.token
                if token:
                    params.append(('token', token))
                return self.agent.http.get(
                    CB.json(), path='/v1/acl/list', params=params)

            def info(self, acl_id, token=None):
                """
                Returns the token information for *acl_id*.
                """
                warnings.warn('Consul 1.4.0 deprecated',
                              DeprecationWarning)
                params = []
                token = token or self.agent.token
                if token:
                    params.append(('token', token))
                return self.agent.http.get(CB.json(one=True),
                                           path='/v1/acl/info/%s' % acl_id,
                                           params=params)

            def create(self,
                       name=None,
                       type='client',
                       rules=None,
                       acl_id=None,
                       token=None):
                warnings.warn('Consul 1.4.0 deprecated',
                              DeprecationWarning)
                params = []
                token = token or self.agent.token
                if token:
                    params.append(('token', token))

                payload = {}
                if name:
                    payload['Name'] = name
                if type:
                    assert type in ('client', 'management'), \
                        'type must be client or management'
                    payload['Type'] = type
                if rules:
                    assert isinstance(rules, str), \
                        'Only HCL or JSON encoded strings' \
                        ' supported for the moment'
                    payload['Rules'] = rules
                if acl_id:
                    payload['ID'] = acl_id

                if payload:
                    data = json.dumps(payload)
                else:
                    data = ''

                return self.agent.http.put(
                    CB.json(is_id=True),
                    path='/v1/acl/create',
                    params=params,
                    data=data)

            def update(self, acl_id, name=None,
                       type=None, rules=None, token=None):
                warnings.warn('Consul 1.4.0 deprecated',
                              DeprecationWarning)
                params = []
                token = token or self.agent.token
                if token:
                    params.append(('token', token))

                payload = {'ID': acl_id}
                if name:
                    payload['Name'] = name
                if type:
                    assert type in ('client', 'management'), \
                        'type must be client or management'
                    payload['Type'] = type
                if rules:
                    assert isinstance(rules, str), \
                        'Only HCL or JSON encoded strings' \
                        ' supported for the moment'
                    payload['Rules'] = rules

                data = json.dumps(payload)

                return self.agent.http.put(
                    CB.json(is_id=True),
                    path='/v1/acl/update',
                    params=params,
                    data=data)

            def clone(self, acl_id, token=None):
                warnings.warn('Consul 1.4.0 deprecated',
                              DeprecationWarning)
                params = []
                token = token or self.agent.token
                if token:
                    params.append(('token', token))
                return self.agent.http.put(
                    CB.json(is_id=True),
                    path='/v1/acl/clone/%s' % acl_id,
                    params=params)

            def destroy(self, acl_id, token=None):
                """
                Returns *True* on success.
                """
                warnings.warn('Consul 1.4.0 deprecated',
                              DeprecationWarning)
                params = []
                token = token or self.agent.token
                if token:
                    params.append(('token', token))
                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/acl/destroy/%s' % acl_id,
                    params=params)

        class Policy(object):
            def __init__(self, agent=None):
                self.agent = agent

            def create(self, name, description=None,
                       rules=None, datacenters=None, token=None):

                payload = {"Name": name}
                if description:
                    payload['Description'] = description
                if rules:
                    payload['Rules'] = rules
                if datacenters:
                    payload['Datacenters'] = datacenters

                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.put(CB.json(),
                                           path='/v1/acl/policy',
                                           headers=headers,
                                           data=json.dumps(payload))

            def get(self, policy_id=None, name=None, token=None):
                path = '/v1/acl/policy/%s' % (policy_id or 'name/' + name)
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def update(self, policy_id, name, description=None,
                       rules=None, datacenters=None, token=None):

                payload = {"Name": name}
                if description:
                    payload['Description'] = description
                if rules:
                    payload['Rules'] = rules
                if datacenters:
                    payload['Datacenters'] = datacenters

                path = '/v1/acl/policy/%s' % policy_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

            def delete(self, policy_id, token=None):
                path = '/v1/acl/policy/%s' % policy_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers)

            def list(self, token=None):
                path = '/v1/acl/policies'
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

        class Roles(object):
            def __init__(self, agent=None):
                self.agent = agent

            def create(self, payload, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path='/v1/acl/role',
                                           headers=headers,
                                           data=json.dumps(payload))

            def get(self, role_id, token=None):
                path = '/v1/acl/role/%s' % role_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def get_by_name(self, role_name, token=None):
                path = '/v1/acl/role/name/%s' % role_name
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def update(self, payload, role_id, token=None):
                path = '/v1/acl/role/%s' % role_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

            def delete(self, role_id, token=None):
                path = '/v1/acl/role/%s' % role_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers)

            def list(
                    self, policy=None, token=None):
                """
                This endpoint lists all the ACL roles.
                    :param *policy*: (string: "") - Filters the
                    role list to those roles that are linked with
                    the specific policy ID.
                    :header X-Consul-Token:
                    :return:
                """

                path = '/v1/acl/roles'
                params = []
                if policy:
                    params.append(('policy', policy))
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.get(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers)

        class AuthMethod(object):
            def __init__(self, agent=None):
                self.agent = agent

            def create(self, payload, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path='/v1/acl/auth-method',
                                           headers=headers,
                                           data=json.dumps(payload))

            def get(self, auth_method_name, token=None):
                path = '/v1/acl/auth-method/%s' % auth_method_name
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def update(self, payload, name, token=None):
                path = '/v1/acl/auth-method/%s' % name
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

            def delete(self, name, token=None):
                path = '/v1/acl/auth-method/%s' % name
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers)

            def list(
                    self, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path='/v1/acl/auth-methods',
                                           headers=headers)

        class BindingRule(object):
            def __init__(self, agent=None):
                self.agent = agent

            def create(self, payload, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path='/v1/acl/binding-rule',
                                           headers=headers,
                                           data=json.dumps(payload))

            def get(self, binding_rule_id, token=None):
                path = '/v1/acl/binding-rule/%s' % binding_rule_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def update(self, payload, binding_rule_id, token=None):
                path = '/v1/acl/binding-rule/%s' % binding_rule_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

            def delete(self, binding_rule_id, token=None):
                path = '/v1/acl/binding-rule/%s' % binding_rule_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers)

            def list(
                    self, token=None):
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path='/v1/acl/binding-rules',
                                           headers=headers)

    class Agent(object):
        """
        The Agent endpoints are used to interact with a local Consul agent.
        Usually, services and checks are registered with an agent, which then
        takes on the burden of registering with the Catalog and performing
        anti-entropy to recover from outages.
        """

        def __init__(self, agent):
            self.agent = agent
            self.service = Consul.Agent.Service(agent)
            self.check = Consul.Agent.Check(agent)
            self.connect = Consul.Agent.Connect(agent)

        def self(self, token=None):
            """
            Returns configuration of the local agent and member information.
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(CB.json(),
                                       path='/v1/agent/self', headers=headers)

        def services(self, token=None):
            """
            Returns all the services that are registered with the local agent.
            These services were either provided through configuration files, or
            added dynamically using the HTTP API. It is important to note that
            the services known by the agent may be different than those
            reported by the Catalog. This is usually due to changes being made
            while there is no leader elected. The agent performs active
            anti-entropy, so in most situations everything will be in sync
            within a few seconds.
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(CB.json(),
                                       path='/v1/agent/services',
                                       headers=headers)

        def checks(self, token=None):
            """
            Returns all the checks that are registered with the local agent.
            These checks were either provided through configuration files, or
            added dynamically using the HTTP API. Similar to services,
            the checks known by the agent may be different than those
            reported by the Catalog. This is usually due to changes being made
            while there is no leader elected. The agent performs active
            anti-entropy, so in most situations everything will be in sync
            within a few seconds.
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(CB.json(),
                                       path='/v1/agent/checks',
                                       headers=headers)

        def members(self, wan=False, token=None):
            """
            Returns all the members that this agent currently sees. This may
            vary by agent, use the nodes api of Catalog to retrieve a cluster
            wide consistent view of members.

            For agents running in server mode, setting *wan* to *True* returns
            the list of WAN members instead of the LAN members which is
            default.
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if wan:
                params.append(('wan', 1))
            return self.agent.http.get(
                CB.json(), path='/v1/agent/members',
                params=params, headers=headers)

        def maintenance(self, enable, reason=None, token=None):
            """
            The node maintenance endpoint can place the agent into
            "maintenance mode".

            *enable* is either 'true' or 'false'. 'true' enables maintenance
            mode, 'false' disables maintenance mode.

            *reason* is an optional string. This is simply to aid human
            operators.
            """

            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            params.append(('enable', enable))
            if reason:
                params.append(('reason', reason))

            return self.agent.http.put(
                CB.bool(), path='/v1/agent/maintenance',
                params=params, headers=headers)

        def join(self, address, wan=False, token=None):
            """
            This endpoint instructs the agent to attempt to connect to a
            given address.

            *address* is the ip to connect to.

            *wan* is either 'true' or 'false'. For agents running in server
            mode, 'true' causes the agent to attempt to join using the WAN
            pool. Default is 'false'.
            """

            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if wan:
                params.append(('wan', 1))

            return self.agent.http.put(
                CB.bool(), path='/v1/agent/join/%s' % address,
                params=params, headers=headers)

        def force_leave(self, node, token=None):
            """
            This endpoint instructs the agent to force a node into the left
            state. If a node fails unexpectedly, then it will be in a failed
            state. Once in the failed state, Consul will attempt to reconnect,
            and the services and checks belonging to that node will not be
            cleaned up. Forcing a node into the left state allows its old
            entries to be removed.

            *node* is the node to change state for.
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(
                CB.bool(),
                path='/v1/agent/force-leave/%s' % node, headers=headers)

        class Service(object):
            def __init__(self, agent):
                self.agent = agent

            def register(
                    self,
                    name,
                    service_id=None,
                    address=None,
                    port=None,
                    tags=None,
                    check=None,
                    token=None,
                    meta=None,
                    weights=None,
                    # *deprecated* use check parameter
                    script=None,
                    interval=None,
                    ttl=None,
                    http=None,
                    timeout=None,
                    caller=None,
                    enable_tag_override=False):
                """
                Add a new service to the local agent. There is more
                documentation on services
                `here <http://www.consul.io/docs/agent/services.html>`_.

                *name* is the name of the service.

                If the optional *service_id* is not provided it is set to
                *name*. You cannot have duplicate *service_id* entries per
                agent, so it may be necessary to provide one.

                *address* will default to the address of the agent if not
                provided.

                An optional health *check* can be created for this service is
                one of `Check.script`_, `Check.http`_, `Check.tcp`_,
                `Check.ttl`_ or `Check.docker`_.

                *token* is an optional `ACL token`_ to apply to this request.
                Note this call will return successful even if the token doesn't
                have permissions to register this service.

                *meta* specifies arbitrary KV metadata linked to the service
                formatted as {k1:v1, k2:v2}.

                *weights* specifies weights for the service.
                If this field is not provided weights
                will default to {"Passing": 1, "Warning": 1}.

                *script*, *interval*, *ttl*, *http*, and *timeout* arguments
                are deprecated. use *check* instead.

                *caller* is a name of caller service.

                *enable_tag_override* is an optional bool that enable you
                to modify a service tags from servers(consul agent role server)
                Default is set to False.
                This option is only for >=v0.6.0 version on both agent and
                servers.
                for more information
                https://www.consul.io/docs/agent/services.html
                """
                params = []
                payload = {}

                payload['name'] = name
                if enable_tag_override:
                    payload['enabletagoverride'] = enable_tag_override
                if service_id:
                    payload['id'] = service_id
                if address:
                    payload['address'] = address
                if port:
                    payload['port'] = port
                if tags:
                    payload['tags'] = tags
                if meta:
                    payload['meta'] = meta
                if check:
                    payload['check'] = check
                if weights:
                    payload['weights'] = weights

                if caller:
                    params.append(('caller', caller))

                else:
                    payload.update(Check._compat(
                        script=script,
                        interval=interval,
                        ttl=ttl,
                        http=http,
                        timeout=timeout))

                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/service/register',
                    headers=headers,
                    params=params,
                    data=json.dumps(payload))

            def deregister(self, service_id, caller=None, token=None):
                """
                Used to remove a service from the local agent. The agent will
                take care of deregistering the service with the Catalog. If
                there is an associated check, that is also deregistered.
                """

                params = []
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if caller:
                    params.append(('caller', caller))
                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/service/deregister/%s' % service_id,
                    headers=headers,
                    params=params
                )

            def maintenance(self, service_id, enable, reason=None, token=None):
                """
                The service maintenance endpoint allows placing a given service
                into "maintenance mode".

                *service_id* is the id of the service that is to be targeted
                for maintenance.

                *enable* is either 'true' or 'false'. 'true' enables
                maintenance mode, 'false' disables maintenance mode.

                *reason* is an optional string. This is simply to aid human
                operators.
                """

                params = [('enable', enable)]
                headers = {}

                token = token or self.agent.token
                if reason:
                    params.append(('reason', reason))
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/service/maintenance/{}'.format(service_id),
                    headers=headers,
                    params=params)

        class Check(object):
            def __init__(self, agent):
                self.agent = agent

            def register(
                    self,
                    name,
                    check=None,
                    check_id=None,
                    notes=None,
                    service_id=None,
                    token=None,
                    # *deprecated* use check parameter
                    script=None,
                    interval=None,
                    ttl=None,
                    http=None,
                    timeout=None):
                """
                Register a new check with the local agent. More documentation
                on checks can be found `here
                <http://www.consul.io/docs/agent/checks.html>`_.

                *name* is the name of the check.

                *check* is one of `Check.script`_, `Check.http`_, `Check.tcp`_
                `Check.ttl`_ or `Check.docker`_ and is required.

                If the optional *check_id* is not provided it is set to *name*.
                *check_id* must be unique for this agent.

                *notes* is not used by Consul, and is meant to be human
                readable.

                Optionally, a *service_id* can be specified to associate a
                registered check with an existing service.

                *token* is an optional `ACL token`_ to apply to this request.
                Note this call will return successful even if the token doesn't
                have permissions to register this check.

                *script*, *interval*, *ttl*, *http*, and *timeout* arguments
                are deprecated. use *check* instead.

                Returns *True* on success.
                """
                payload = {'name': name}

                assert check or script or ttl or http, \
                    'check is required'

                if check:
                    payload.update(check)

                else:
                    payload.update(Check._compat(
                        script=script,
                        interval=interval,
                        ttl=ttl,
                        http=http,
                        timeout=timeout)['check'])

                if check_id:
                    payload['id'] = check_id
                if notes:
                    payload['notes'] = notes
                if service_id:
                    payload['serviceid'] = service_id

                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/check/register',
                    headers=headers,
                    data=json.dumps(payload))

            def deregister(self, check_id, token=None):
                """
                Remove a check from the local agent.
                """
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/check/deregister/%s' % check_id,
                    headers=headers,
                )

            def ttl_pass(self, check_id, notes=None, token=None):
                """
                Mark a ttl based check as passing. Optional notes can be
                attached to describe the status of the check.
                """
                params = []
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if notes:
                    params.append(('note', notes))

                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/check/pass/%s' % check_id,
                    params=params,
                    headers=headers)

            def ttl_fail(self, check_id, notes=None, token=None):
                """
                Mark a ttl based check as failing. Optional notes can be
                attached to describe why check is failing. The status of the
                check will be set to critical and the ttl clock will be reset.
                """
                params = []
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if notes:
                    params.append(('note', notes))

                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/check/fail/%s' % check_id,
                    params=params,
                    headers=headers)

            def ttl_warn(self, check_id, notes=None, token=None):
                """
                Mark a ttl based check with warning. Optional notes can be
                attached to describe the warning. The status of the
                check will be set to warn and the ttl clock will be reset.
                """
                params = []
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if notes:
                    params.append(('note', notes))

                return self.agent.http.put(
                    CB.bool(),
                    path='/v1/agent/check/warn/%s' % check_id,
                    params=params,
                    headers=headers)

        class Connect(object):
            def __init__(self, agent):
                self.agent = agent

            def authorize(self,
                          target,
                          client_cert_uri,
                          client_cert_serial,
                          token=None):
                payload = {
                    "Target": target,
                    "ClientCertURI": client_cert_uri,
                    "ClientCertSerial": client_cert_serial
                }
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.post(
                    CB.json(),
                    path='/v1/agent/connect/authorize',
                    headers=headers,
                    data=json.dumps(payload))

            def root_certificates(self, token=None):
                """
                :param token:
                :return: returns the trusted certificate authority (CA)
                root certificates.
                """
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.get(
                    CB.json(),
                    path='/v1/agent/connect/ca/roots',
                    headers=headers)

            def leaf_certificates(self, service, token=None):
                """
                :param token:
                :return: returns the leaf certificate representing
                a single service.
                """
                path = '/agent/connect/ca/leaf/%s' % service
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.get(
                    CB.json(),
                    path=path,
                    headers=headers)

    class Catalog(object):
        def __init__(self, agent):
            self.agent = agent

        def register(self,
                     node,
                     address,
                     service=None,
                     skipnodeupdate=False,
                     check=None,
                     dc=None,
                     token=None,
                     node_meta=None):
            """
            A low level mechanism for directly registering or updating entries
            in the catalog. It is usually recommended to use
            agent.service.register and agent.check.register, as they are
            simpler and perform anti-entropy.

            *node* is the name of the node to register.

            *address* is the ip of the node.

            *service* is an optional service to register. if supplied this is a
            dict::

                {
                    "Service": "redis",
                    "ID": "redis1",
                    "Tags": [
                        "master",
                        "v1"
                    ],
                    "Port": 8000
                }

            where

                *Service* is required and is the name of the service

                *ID* is optional, and will be set to *Service* if not provided.
                Note *ID* must be unique for the given *node*.

                *Tags* and *Port* are optional.

            *skipnodeupdate* is an optional boolean which, when set to True,
            skips updating the node's information during registration. By
            default, it is set to False. For information on this parameter, see
            `here <https://www.consul.io/api-docs/catalog#skipnodeupdate`_.

            *check* is an optional check to register. if supplied this is a
            dict::

                {
                    "Node": "foobar",
                    "CheckID": "service:redis1",
                    "Name": "Redis health check",
                    "Notes": "Script based health check",
                    "Status": "passing",
                    "ServiceID": "redis1"
                }

            *dc* is the datacenter of the node and defaults to this agents
            datacenter.

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.

            This manipulates the health check entry, but does not setup a
            script or TTL to actually update the status. The full documentation
            is `here <https://consul.io/docs/agent/http.html#catalog>`_.

            Returns *True* on success.
            """
            data = {'node': node, 'address': address}
            params = []
            headers = {}
            dc = dc or self.agent.dc
            if dc:
                data['datacenter'] = dc
            if service:
                data['service'] = service
            if skipnodeupdate:
                data['skipnodeupdate'] = True
            if check:
                data['check'] = check
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if node_meta:
                nm = {}
                for nodemeta_name, nodemeta_value in node_meta.items():
                    nm[nodemeta_name] = nodemeta_value
                data['NodeMeta'] = nm
            return self.agent.http.put(
                CB.bool(),
                path='/v1/catalog/register',
                data=json.dumps(data),
                params=params,
                headers=headers)

        def deregister(self,
                       node,
                       service_id=None,
                       check_id=None,
                       dc=None,
                       token=None):
            """
            A low level mechanism for directly removing entries in the catalog.
            It is usually recommended to use the agent APIs, as they are
            simpler and perform anti-entropy.

            *node* and *dc* specify which node on which datacenter to remove.
            If *service_id* and *check_id* are not provided, all associated
            services and checks are deleted. Otherwise only one of *service_id*
            and *check_id* should be provided and only that service or check
            will be removed.

            *token* is an optional `ACL token`_ to apply to this request.

            Returns *True* on success.
            """
            assert not (service_id and check_id)
            data = {'node': node}
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                data['datacenter'] = dc
            if service_id:
                data['serviceid'] = service_id
            if check_id:
                data['checkid'] = check_id
            if token:
                headers['X-Consul-Token'] = token

            return self.agent.http.put(
                CB.bool(),
                path='/v1/catalog/deregister',
                params=params,
                headers=headers,
                data=json.dumps(data))

        def datacenters(self):
            """
            Returns all the datacenters that are known by the Consul server.
            """
            return self.agent.http.get(
                CB.json(), path='/v1/catalog/datacenters')

        def nodes(
                self,
                index=None,
                wait=None,
                consistency=None,
                dc=None,
                near=None,
                token=None,
                node_meta=None):
            """
            Returns a tuple of (*index*, *nodes*) of all nodes known
            about in the *dc* datacenter. *dc* defaults to the current
            datacenter of this agent.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *near* is a node name to sort the resulting list in ascending
            order based on the estimated round trip time from that node

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.

            The response looks like this::

                (index, [
                    {
                        "Node": "baz",
                        "Address": "10.1.10.11"
                    },
                    {
                        "Node": "foobar",
                        "Address": "10.1.10.12"
                    }
                ])
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            if near:
                params.append(('near', near))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            if node_meta:
                for nodemeta_name, nodemeta_value in node_meta.items():
                    params.append(('node-meta', '{0}:{1}'.
                                   format(nodemeta_name, nodemeta_value)))
            return self.agent.http.get(
                CB.json(index=True), path='/v1/catalog/nodes',
                params=params, headers=headers)

        def services(self,
                     index=None,
                     wait=None,
                     consistency=None,
                     dc=None,
                     token=None,
                     node_meta=None):
            """
            Returns a tuple of (*index*, *services*) of all services known
            about in the *dc* datacenter. *dc* defaults to the current
            datacenter of this agent.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.

            The response looks like this::

                (index, {
                    "consul": [],
                    "redis": [],
                    "postgresql": [
                        "master",
                        "slave"
                    ]
                })

            The main keys are the service names and the list provides all the
            known tags for a given service.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            if node_meta:
                for nodemeta_name, nodemeta_value in node_meta.items():
                    params.append(('node-meta', '{0}:{1}'.
                                   format(nodemeta_name, nodemeta_value)))
            return self.agent.http.get(
                CB.json(index=True), path='/v1/catalog/services',
                params=params, headers=headers)

        def node(self,
                 node,
                 index=None,
                 wait=None,
                 consistency=None,
                 dc=None,
                 token=None):
            """
            Returns a tuple of (*index*, *services*) of all services provided
            by *node*.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.

            *dc* is the datacenter of the node and defaults to this agents
            datacenter.

            *token* is an optional `ACL token`_ to apply to this request.

            The response looks like this::

                (index, {
                    "Node": {
                        "Node": "foobar",
                        "Address": "10.1.10.12"
                    },
                    "Services": {
                        "consul": {
                            "ID": "consul",
                            "Service": "consul",
                            "Tags": null,
                            "Port": 8300
                        },
                        "redis": {
                            "ID": "redis",
                            "Service": "redis",
                            "Tags": [
                                "v1"
                            ],
                            "Port": 8000
                        }
                    }
                })
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/catalog/node/%s' % node,
                params=params,
                headers=headers)

        def service(
                self,
                service,
                index=None,
                wait=None,
                tag=None,
                consistency=None,
                dc=None,
                near=None,
                token=None,
                node_meta=None):
            """
            Returns a tuple of (*index*, *nodes*) of the nodes providing
            *service* in the *dc* datacenter. *dc* defaults to the current
            datacenter of this agent.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            If *tag* is provided, the list of nodes returned will be filtered
            by that tag.

            *near* is a node name to sort the resulting list in ascending
            order based on the estimated round trip time from that node

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.

            The response looks like this::

                (index, [
                    {
                        "Node": "foobar",
                        "Address": "10.1.10.12",
                        "ServiceID": "redis",
                        "ServiceName": "redis",
                        "ServiceTags": null,
                        "ServicePort": 8000
                    }
                ])
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            if tag:
                params.append(('tag', tag))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            if near:
                params.append(('near', near))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            if node_meta:
                for nodemeta_name, nodemeta_value in node_meta.items():
                    params.append(('node-meta', '{0}:{1}'.
                                   format(nodemeta_name, nodemeta_value)))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/catalog/service/%s' % service,
                params=params,
                headers=headers)

    class Config(object):
        """
        The /config endpoints create, update, delete and query central
        configurationentries registered with Consul. See the agent
        configuration for moreinformation on how to enable this
        functionality for centrally configuring services and
        configuration entries docs for a description of the
        configuration entries content.
        """

        def __init__(self, agent):
            self.agent = agent

        def put(self, data, dc=None, token=None, cas=None):
            """
            This endpoint creates or updates the given config entry.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.

            *cas* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            if cas:
                params.append(('cas', cas))

            if data:
                data = json.dumps(data)
            else:
                data = ''
            return self.agent.http.put(CB.json(),
                                       path='/v1/config',
                                       params=params,
                                       headers=headers,
                                       data=data)

        def get(self, kind, name, dc=None, token=None):
            """
            This endpoint returns a specific config entry.

            *dc* (string: "") - Specifies the datacenter to query. This will
            default to the datacenter of the agent being queried. This is
            specified as part of the URL as a query parameter

            *kind* (string: <required>) - Specifies the kind of the entry to
             read. This is specified as part of the URL.

            *name* (string: <required>) - Specifies the name of the entry to
             read. This is specified as part of the URL
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token
            path = '/v1/config/%s/%s' % (kind, name)

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token

            return self.agent.http.get(CB.json(),
                                       path=path,
                                       params=params,
                                       headers=headers)

        def list(self, kind, dc=None, token=None):
            """
            This endpoint returns all config entries of the given kind.

            *dc* (string: "") - Specifies the datacenter to query. This will
            default to the datacenter of the agent being queried. This is
            specified as part of the URL as a query parameter

            *kind* (string: <required>) - Specifies the kind of the entry to
             read. This is specified as part of the URL.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token
            path = '/v1/config/%s' % kind

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token

            return self.agent.http.get(CB.json(),
                                       path=path,
                                       params=params,
                                       headers=headers)

        def delete(self, kind, name, dc=None, token=None):
            """
            This endpoint delete the given config entry.


            *dc* (string: "") - Specifies the datacenter to query. This will
            default to the datacenter of the agent being queried. This is
            specified as part of the URL as a query parameter

            *kind* (string: <required>) - Specifies the kind of the entry to
             read. This is specified as part of the URL.

            *name* (string: <required>) - Specifies the name of the entry to
             read. This is specified as part of the URL
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token
            path = '/v1/config/%s/%s' % (kind, name)

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token

            return self.agent.http.delete(CB.bool(),
                                          path=path,
                                          params=params,
                                          headers=headers)

    class Connect(object):
        """
        The */connect* endpoints provide access to Connect-related
        operations for  intentions and the certificate authority.

        There are also Connect-related endpoints in the Agent and Catalog APIs.
        For example, the API for requesting a TLS certificate for a service is
        part of the agent APIs. And the catalog API has an endpoint for finding
        all Connect-capable services in the catalog..
        """

        def __init__(self, agent):
            self.agent = agent
            self.certificates = Consul.Connect.Certificates(agent)
            self.intentions = Consul.Connect.Intentions(agent)

        class Certificates:
            """
            This endpoint returns the current list of trusted CA root
            certificates in the cluster.
            """

            def __init__(self, agent):
                self.agent = agent

            def list(self, token=None):
                path = '/v1/connect/ca/roots'
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.get(CB.json(),
                                           path=path, headers=headers)

            def current(self, token=None):
                """
                This endpoint returns the current CA configuration.
                """
                path = '/v1/connect/ca/configuration'
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token

                return self.agent.http.get(CB.json(),
                                           path=path, headers=headers)

            def put(self, config, provider, token=None):
                """
                This endpoint updates the configuration for the CA.
                If this results in a new root certificate being used,
                the Root Rotation process will be triggered.

                *Provider*
                *Config*
                dict::

                {
                    "LeafCertTTL": "72h",
                    "PrivateKey": "-----BEGIN RSA PRIVATE KEY-----...",
                    "RootCert": "-----BEGIN CERTIFICATE-----...",
                    "RotationPeriod": "2160h"
                 }
                :return:
                """
                path = '/v1/connect/ca/configuration'
                payload = {
                    "Provider": provider,
                    "Config": config
                }
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.bool(),
                                           path=path,
                                           headers=headers,
                                           data=json.dumps(payload))

        class Intentions:
            """
            This endpoint returns the current list of trusted CA root
            certificates in the cluster.
            """

            def __init__(self, agent):
                self.agent = agent

            def create(self,
                       source_name,
                       destination_name,
                       source_type,
                       action,
                       description=None,
                       meta=None,
                       token=None):
                """
                :param source_name:
                :param destination_name:
                :param source_type:
                :param action:
                :param description:
                :param meta:
                :param token:
                :return: intentions id
                """
                path = '/v1/connect/intentions'
                payload = {
                    "SourceName": source_name,
                    "DestinationName": destination_name,
                    "SourceType": source_type,
                    "Action": action
                }
                if description:
                    payload['Description'] = description
                if meta:
                    payload['Meta'] = meta
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.post(CB.json(),
                                            path=path,
                                            headers=headers,
                                            data=json.dumps(payload))

            def get(self, intention_id, token=None):
                path = '/v1/connect/intentions/%s' % intention_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def list(self, token=None):
                path = '/v1/connect/intentions'
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           headers=headers)

            def put(self, intention_id,
                    token=None,
                    source_name=None,
                    destination_name=None,
                    source_type=None,
                    action=None,
                    description=None,
                    meta=None):
                """
                :param intention_id:
                :param token:
                :param source_name:
                :param destination_name:
                :param source_type:
                :param action:
                :param description:
                :param meta:
                :return:
                """
                path = '/v1/connect/intentions/%s' % intention_id
                payload = {}
                if source_name:
                    payload['SourceName'] = source_name
                if destination_name:
                    payload['DestinationName'] = destination_name
                if source_type:
                    payload['SourceType'] = source_type
                if action:
                    payload['Action'] = action
                if description:
                    payload['Description'] = description
                if meta:
                    payload['Meta'] = meta
                headers = {}
                token = token or self.agent.token
                if payload:
                    data = json.dumps(payload)
                else:
                    data = ''
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.put(CB.bool(),
                                           path=path,
                                           headers=headers,
                                           data=data)

            def delete(self, intention_id, token=None):
                path = '/v1/connect/intentions/%s' % intention_id
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers)

            def check(self, source, destination, token=None):
                path = '/v1/connect/intentions/check'
                params = []
                headers = {}
                token = token or self.agent.token
                params.append(('source', source))
                params.append(('destination', destination))
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers)

            def list_match(self, by, name, token=None):
                path = '/v1/connect/intentions/match'
                params = []
                headers = {}
                token = token or self.agent.token
                params.append(('by', by))
                params.append(('name', name))
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers)

    class Coordinate(object):
        def __init__(self, agent):
            self.agent = agent

        def datacenters(self):
            """
            Returns the WAN network coordinates for all Consul servers,
            organized by DCs.
            """
            return self.agent.http.get(CB.json(),
                                       path='/v1/coordinate/datacenters')

        def nodes(self, dc=None, index=None, wait=None, consistency=None):
            """
            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.
            """
            params = []
            if dc:
                params.append(('dc', dc))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/coordinate/nodes', params=params)

    class DiscoveryChain(object):
        def __init__(self, agent):
            warnings.warn('1.6.0+: The discovery '
                          'chain API is available '
                          'in Consul versions 1.6.0 '
                          'and newer.', DeprecationWarning)
            self.agent = agent

        """
        This is a low-level API primarily targeted at developers
        building external Connect proxy integrations. Future
        high-level proxy integration APIs may obviate the need
        for this API over time.
        # todo DiscoveryChain
        """

    class Event(object):
        """
        The event command provides a mechanism to fire a custom user event to
        an entire datacenter. These events are opaque to Consul, but they can
        be used to build scripting infrastructure to do automated deploys,
        restart services, or perform any other orchestration action.

        Unlike most Consul data, which is replicated using consensus, event
        data is purely peer-to-peer over gossip.

        This means it is not persisted and does not have a total ordering. In
        practice, this means you cannot rely on the order of message delivery.
        An advantage however is that events can still be used even in the
        absence of server nodes or during an outage."""

        def __init__(self, agent):
            self.agent = agent

        def fire(
                self,
                name,
                body="",
                node=None,
                service=None,
                tag=None,
                token=None):
            """
            Sends an event to Consul's gossip protocol.

            *name* is the Consul-opaque name of the event. This can be filtered
            on in calls to list, below

            *body* is the Consul-opaque body to be delivered with the event.
             From the Consul documentation:
                The underlying gossip also sets limits on the size of a user
                event message. It is hard to give an exact number, as it
                depends on various parameters of the event, but the payload
                should be kept very small (< 100 bytes). Specifying too large
                of an event will return an error.

            *node*, *service*, and *tag* are regular expressions which remote
            agents will filter against to determine if they should store the
            event

            *token* is an optional `ACL token`_ to apply to this request. If
            the token's policy is not allowed to fire an event of this *name*
            an *ACLPermissionDenied* exception will be raised.
            """
            assert not name.startswith('/'), \
                'keys should not start with a forward slash'
            params = []
            headers = {}
            if node is not None:
                params.append(('node', node))
            if service is not None:
                params.append(('service', service))
            if tag is not None:
                params.append(('tag', tag))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token

            return self.agent.http.put(
                CB.json(),
                path='/v1/event/fire/%s' % name,
                params=params, headers=headers, data=body)

        def list(
                self,
                name=None,
                index=None,
                wait=None, token=None):
            """
            Returns a tuple of (*index*, *events*)
                Note: Since Consul's event protocol uses gossip, there is no
                ordering, and instead index maps to the newest event that
                matches the query.

            *name* is the type of events to list, if None, lists all available.

            *index* is the current event Consul index, suitable for making
            subsequent calls to wait for changes since this query was last run.
            Check https://consul.io/docs/agent/http/event.html#event_list for
            more infos about indexes on events.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. This parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            Consul agents only buffer the most recent entries. The current
            buffer size is 256, but this value could change in the future.

            Each *event* looks like this::

                {
                      {
                        "ID": "b54fe110-7af5-cafc-d1fb-afc8ba432b1c",
                        "Name": "deploy",
                        "Payload": "1609030",
                        "NodeFilter": "",
                        "ServiceFilter": "",
                        "TagFilter": "",
                        "Version": 1,
                        "LTime": 19
                      },
                }
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if name is not None:
                params.append(('name', name))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            return self.agent.http.get(
                CB.json(index=True, decode=True),
                path='/v1/event/list', params=params, headers=headers)

    class Health(object):
        # TODO: All of the health endpoints support all consistency modes
        def __init__(self, agent):
            self.agent = agent

        def service(self,
                    service,
                    index=None,
                    wait=None,
                    passing=None,
                    tag=None,
                    dc=None,
                    near=None,
                    token=None,
                    node_meta=None,
                    caller=None):
            """
            Returns a tuple of (*index*, *nodes*)

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *nodes* are the nodes providing the given service.

            Calling with *passing* set to True will filter results to only
            those nodes whose checks are currently passing.

            Calling with *tag* will filter the results by tag.

            *dc* is the datacenter of the node and defaults to this agents
            datacenter.

            *near* is a node name to sort the resulting list in ascending
            order based on the estimated round trip time from that node

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.

            *caller* is a name of caller service.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            if passing:
                params.append(('passing', '1'))
            if tag is not None:
                params.append(('tag', tag))
            if dc:
                params.append(('dc', dc))
            if near:
                params.append(('near', near))
            if token:
                headers['X-Consul-Token'] = token
            if node_meta:
                for nodemeta_name, nodemeta_value in node_meta.items():
                    params.append(('node-meta', '{0}:{1}'.
                                   format(nodemeta_name, nodemeta_value)))
            if caller:
                params.append(('caller', caller))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/health/service/%s' % service,
                params=params, headers=headers)

        def checks(
                self,
                service,
                index=None,
                wait=None,
                dc=None,
                near=None,
                token=None,
                node_meta=None):
            """
            Returns a tuple of (*index*, *checks*) with *checks* being the
            checks associated with the service.

            *service* is the name of the service being checked.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *dc* is the datacenter of the node and defaults to this agents
            datacenter.

            *near* is a node name to sort the resulting list in ascending
            order based on the estimated round trip time from that node

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.
            """
            params = []
            headers = {}
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            if near:
                params.append(('near', near))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if node_meta:
                for nodemeta_name, nodemeta_value in node_meta.items():
                    params.append(('node-meta', '{0}:{1}'.
                                   format(nodemeta_name, nodemeta_value)))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/health/checks/%s' % service,
                params=params, headers=headers)

        def state(self,
                  name,
                  index=None,
                  wait=None,
                  dc=None,
                  near=None,
                  token=None,
                  node_meta=None):
            """
            Returns a tuple of (*index*, *nodes*)

            *name* is a supported state. From the Consul docs:

                The supported states are any, unknown, passing, warning, or
                critical. The any state is a wildcard that can be used to
                return all checks.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *dc* is the datacenter of the node and defaults to this agents
            datacenter.

            *near* is a node name to sort the resulting list in ascending
            order based on the estimated round trip time from that node

            *token* is an optional `ACL token`_ to apply to this request.

            *node_meta* is an optional meta data used for filtering, a
            dictionary formatted as {k1:v1, k2:v2}.

            *nodes* are the nodes providing the given service.
            """
            assert name in ['any', 'unknown', 'passing', 'warning', 'critical']
            params = []
            headers = {}
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if near:
                params.append(('near', near))
            if token:
                headers['X-Consul-Token'] = token
            if node_meta:
                for nodemeta_name, nodemeta_value in node_meta.items():
                    params.append(('node-meta', '{0}:{1}'.
                                   format(nodemeta_name, nodemeta_value)))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/health/state/%s' % name,
                params=params, headers=headers)

        def node(self, node, index=None, wait=None, dc=None, token=None):
            """
            Returns a tuple of (*index*, *checks*)

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *dc* is the datacenter of the node and defaults to this agents
            datacenter.

            *token* is an optional `ACL token`_ to apply to this request.

            *nodes* are the nodes providing the given service.
            """
            params = []
            headers = {}
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token

            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/health/node/%s' % node,
                params=params, headers=headers)

    class KV(object):
        """
        The KV endpoint is used to expose a simple key/value store. This can be
        used to store service configurations or other meta data in a simple
        way.
        """

        def __init__(self, agent):
            self.agent = agent

        def get(
                self,
                key,
                index=None,
                recurse=False,
                wait=None,
                token=None,
                consistency=None,
                keys=False,
                separator=None,
                dc=None,
                total_timeout=None,
                caller=None):
            """
            Returns a tuple of (*index*, *value[s]*)

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *token* is an optional `ACL token`_ to apply to this request.

            *keys* is a boolean which, if True, says to return a flat list of
            keys without values or other metadata. *separator* can be used
            with *keys* to list keys only up to a given separator character.

            *dc* is the optional datacenter that you wish to communicate with.
            If None is provided, defaults to the agent's datacenter.

            The *value* returned is for the specified key, or if *recurse* is
            True a list of *values* for all keys with the given prefix is
            returned.

            Each *value* looks like this::

                {
                    "CreateIndex": 100,
                    "ModifyIndex": 200,
                    "LockIndex": 200,
                    "Key": "foo",
                    "Flags": 0,
                    "Value": "bar",
                    "Session": "adf4238a-882b-9ddc-4a9d-5b6758e4159e"
                }

            *caller* is a name of caller service.

            Note, if the requested key does not exists *(index, None)* is
            returned. It's then possible to long poll on the index for when the
            key is created.
            """
            assert not key.startswith('/'), \
                'keys should not start with a forward slash'
            params = []
            headers = {}
            if index:
                params.append(('index', index))
                if wait:
                    assert total_timeout, \
                        'total_timeout should be setted'
                    assert not self._convert_wait_to_seconds(wait) >= total_timeout, \
                        f'wait: {wait} should be less than total_timeout: {total_timeout}s'
                    params.append(('wait', wait))
            if recurse:
                params.append(('recurse', '1'))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            dc = dc or self.agent.dc
            if dc:
                params.append(('dc', dc))
            if keys:
                params.append(('keys', True))
            if separator:
                params.append(('separator', separator))
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            if caller:
                params.append(('caller', caller))

            one = False
            decode = False

            if not keys:
                decode = 'Value'
            if not recurse and not keys:
                one = True
            return self.agent.http.get(
                CB.json(index=True, decode=decode, one=one,
                        map=lambda x: x if x else None),
                path='/v1/kv/%s' % key,
                params=params, headers=headers, total_timeout=total_timeout)

        def _convert_wait_to_seconds(self, wait):
            unit_to_seconds_multiplier = {
                'ms': 0.001,
                's': 1,
                'm': 60
            }
            wait_digit = int(re.search(r'\d+', wait).group())
            multiplier = unit_to_seconds_multiplier[re.search(r'ms|s|m', wait).group()]

            return wait_digit * multiplier

        def put(
                self,
                key,
                value,
                cas=None,
                flags=None,
                acquire=None,
                release=None,
                token=None,
                dc=None):
            """
            Sets *key* to the given *value*.

            *value* can either be None (useful for marking a key as a
            directory) or any string type, including binary data (e.g. a
            msgpack'd data structure)

            The optional *cas* parameter is used to turn the PUT into a
            Check-And-Set operation. This is very useful as it allows clients
            to build more complex syncronization primitives on top. If the
            index is 0, then Consul will only put the key if it does not
            already exist. If the index is non-zero, then the key is only set
            if the index matches the ModifyIndex of that key.

            An optional *flags* can be set. This can be used to specify an
            unsigned value between 0 and 2^64-1.

            *acquire* is an optional session_id. if supplied a lock acquisition
            will be attempted.

            *release* is an optional session_id. if supplied a lock release
            will be attempted.

            *token* is an optional `ACL token`_ to apply to this request. If
            the token's policy is not allowed to write to this key an
            *ACLPermissionDenied* exception will be raised.

            *dc* is the optional datacenter that you wish to communicate with.
            If None is provided, defaults to the agent's datacenter.

            The return value is simply either True or False. If False is
            returned, then the update has not taken place.
            """
            assert not key.startswith('/'), \
                'keys should not start with a forward slash'

            assert 'value should be None or a string / binary data', \
                value is None or \
                isinstance(value, (six.string_types, six.binary_type))

            params = []
            headers = {}
            token = token or self.agent.token
            dc = dc or self.agent.dc

            if cas is not None:
                params.append(('cas', cas))
            if flags is not None:
                params.append(('flags', flags))
            if acquire:
                params.append(('acquire', acquire))
            if release:
                params.append(('release', release))
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            return self.agent.http.put(
                CB.json(), path='/v1/kv/%s' % key,
                params=params, headers=headers, data=value)

        def delete(self, key, recurse=None, cas=None, token=None, dc=None):
            """
            Deletes a single key or if *recurse* is True, all keys sharing a
            prefix.

            *cas* is an optional flag is used to turn the DELETE into a
            Check-And-Set operation. This is very useful as a building block
            for more complex synchronization primitives. Unlike PUT, the index
            must be greater than 0 for Consul to take any action: a 0 index
            will not delete the key. If the index is non-zero, the key is only
            deleted if the index matches the ModifyIndex of that key.

            *token* is an optional `ACL token`_ to apply to this request. If
            the token's policy is not allowed to delete to this key an
            *ACLPermissionDenied* exception will be raised.

            *dc* is the optional datacenter that you wish to communicate with.
            If None is provided, defaults to the agent's datacenter.
            """
            assert not key.startswith('/'), \
                'keys should not start with a forward slash'

            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if recurse:
                params.append(('recurse', '1'))
            if cas is not None:
                params.append(('cas', cas))
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))

            return self.agent.http.delete(
                CB.json(), path='/v1/kv/%s' % key,
                params=params, headers=headers)

    class Operator(object):
        def __init__(self, agent):
            self.agent = agent
            self.autopilot = Consul.Operator.Autopilot(agent)
            self.keyring = Consul.Operator.Keyring(agent)
            self.raft = Consul.Operator.Raft(agent)

        def raft_config(self, token=None):
            """
            Returns raft configuration.
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(
                CB.json(),
                path='/v1/operator/raft/configuration',
                headers=headers)

        class Autopilot:
            """
            doing Autopilot
            """

            def __init__(self, agent=None):
                self.agent = agent

            def configuration(self, stale=None, dc=None, token=None):
                path = '/v1/operator/autopilot/configuration'
                params = []
                headers = {}
                dc = dc or self.agent.dc
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if dc:
                    params.append(('dc', dc))
                if stale:
                    params.append(('stale', stale))
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers)

            def update(self, payload, cas=None, dc=None, token=None):
                path = '/v1/operator/autopilot/configuration'
                params = []
                headers = {}
                dc = dc or self.agent.dc
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if dc:
                    params.append(('dc', dc))
                if cas:
                    params.append(('cas', cas))
                return self.agent.http.put(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers,
                                           data=json.dumps(payload))

            def health(self, dc=None, token=None):
                path = '/v1/operator/autopilot/health'
                params = []
                headers = {}
                token = token or self.agent.token
                dc = dc or self.agent.dc
                if token:
                    headers['X-Consul-Token'] = token
                if dc:
                    params.append(('dc', dc))
                return self.agent.http.get(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers)

        class Keyring:

            def __init__(self, agent=None):
                self.agent = agent

            def create(self, key, relay_factor=None, token=None):
                path = '/v1/operator/keyring'
                params = []
                headers = {}
                payload = {'Key': key}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if relay_factor:
                    params.append(('relay-factor', relay_factor))
                return self.agent.http.post(CB.bool(),
                                            path=path,
                                            params=params,
                                            headers=headers,
                                            data=json.dumps(payload))

            def update(self, key, relay_factor=None, token=None):
                path = '/v1/operator/keyring'
                params = []
                headers = {}
                payload = {'Key': key}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if relay_factor:
                    params.append(('relay-factor', relay_factor))
                return self.agent.http.put(CB.bool(),
                                           path=path,
                                           params=params,
                                           headers=headers,
                                           data=json.dumps(payload))

            def delete(self, key, token=None):
                path = '/v1/operator/keyring'
                headers = {}
                payload = {'Key': key}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              headers=headers,
                                              data=json.dumps(payload))

            def list(self, relay_factor=None, local_only=None, token=None):
                params = []
                headers = {}
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if relay_factor:
                    params.append(('relay-factor', relay_factor))
                if local_only:
                    params.append(('local-only', local_only))
                return self.agent.http.get(CB.json(),
                                           path='/v1/operator/keyring',
                                           params=params,
                                           headers=headers)

        class Raft:
            """
            Raft
            """

            def __init__(self, agent=None):
                self.agent = agent

            def configuration(self, dc=None, stale=None, token=None):
                path = '/v1/operator/raft/configuration'
                params = []
                headers = {}
                dc = dc or self.agent.dc
                token = token or self.agent.token
                if token:
                    headers['X-Consul-Token'] = token
                if dc:
                    params.append(('dc', dc))
                if stale:
                    params.append(('stale', stale))

                return self.agent.http.get(CB.json(),
                                           path=path,
                                           params=params,
                                           headers=headers)

            def delete(self, raft_id=None, address=None, dc=None, token=None):
                """
                This endpoint removes the Consul server with given address from
                the Raft configuration.

                There are rare cases where a peer may be left behind in the
                Raft configuration even though the server is no longer
                present and known to the cluster. This endpoint can be used
                to remove the failed server so that it is no longer affects
                the Raft quorum.

                If ACLs are enabled, the client will need to supply an ACL
                Token with operator write privileges.
                """
                path = '/v1/operator/raft/peer'
                params = []
                headers = {}
                token = token or self.agent.token
                dc = dc or self.agent.dc
                assert (raft_id or address) and not \
                    (raft_id and address), 'raft_id or address there' \
                                           ' just and must be one'

                if raft_id:
                    params.append(('id', raft_id))
                else:
                    params.append(('address', address))
                if token:
                    headers['X-Consul-Token'] = token
                if dc:
                    params.append(('dc', dc))
                return self.agent.http.delete(CB.bool(),
                                              path=path,
                                              params=params,
                                              headers=headers)

    class Query(object):
        def __init__(self, agent):
            self.agent = agent

        def list(self, dc=None, token=None):
            """
            Lists all the active  queries. This is a privileged endpoint,
            therefore you will only be able to get the prepared queries
            which the token supplied has read privileges to.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.

            *token* is an optional `ACL token`_ to apply to this request.
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))

            return self.agent.http.get(CB.json(), path='/v1/query',
                                       params=params, headers=headers)

        def _query_data(self, service=None,
                        name=None,
                        session=None,
                        token=None,
                        nearestn=None,
                        datacenters=None,
                        onlypassing=None,
                        tags=None,
                        ttl=None,
                        regexp=None,
                        near=None,
                        nodemeta=None,
                        servicemeta=None):
            """
            {
    "Service": "redis",
    "Failover": {
      "NearestN": 3,
      "Datacenters": ["dc1", "dc2"]
    },
    "Near": "node1",
    "OnlyPassing": false,
    "Tags": ["primary", "!experimental"],
    "NodeMeta": {"instance_type": "m3.large"},
    "ServiceMeta": {"environment": "production"}
  },
            """
            service_body = dict([
                (k, v) for k, v in {
                    'Service': service,
                    'onlypassing': onlypassing,
                    'tags': tags,
                    'near': near,
                    'nodemeta': nodemeta,
                    'servicemeta': servicemeta,
                    'failover': dict([
                        (k, v) for k, v in {
                            'nearestn': nearestn,
                            'datacenters': datacenters
                        }.items() if v is not None
                    ])
                }.items() if v is not None
            ])

            data = dict([
                (k, v) for k, v in {
                    'name': name,
                    'session': session,
                    'token': token or self.agent.token,
                    'dns': {
                        'ttl': ttl
                    } if ttl is not None else None,
                    'template': dict([
                        (k, v) for k, v in {
                            'type': 'name_prefix_match',
                            'regexp': regexp
                        }.items() if v is not None
                    ]),
                    'service': service_body
                }.items() if v is not None
            ])
            return json.dumps(data)

        def create(self, service,
                   name=None,
                   dc=None,
                   session=None,
                   token=None,
                   nearestn=None,
                   datacenters=None,
                   onlypassing=None,
                   tags=None,
                   ttl=None,
                   regexp=None,
                   near=None,
                   nodemeta=None,
                   servicemeta=None):
            """
            Creates a new query. This is a privileged endpoint, and
            requires a management token for a certain query name.*token* will
            override this client's default token.

            *service* is mandatory for new query. represent service name to
            query.

            *name* is an optional name for this query.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.

            *token* is an optional `ACL token`_ to apply to this request.

            *nearestn* if set to a value greater than zero, then the query will
            be forwarded to up to NearestN other datacenters based on their
            estimated network round trip time using Network Coordinates from
            the WAN gossip pool.

            *datacenters* is a fixed list of remote datacenters to forward the
            query to if there are no healthy nodes in the local datacenter.

            *onlypassing* controls the behavior of the query's health check
            filtering.

            *tags* is a list of service tags to filter the query results.

            *ttl*  is a duration string that can use "s" as a suffix for
            seconds. It controls how the TTL is set when query results are
            served over DNS.

            *regexp* is optional for template this option is only supported
            in Consul 0.6.4 or later. The only option for type is
            name_prefix_match so if you want a query template with no regexp
            enter an empty string.

            For more information about query
            https://www.consul.io/docs/agent/http/query.html
            """
            path = '/v1/query'
            params = [] if dc is None else [('dc', dc)]
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            data = self._query_data(
                service, name, session, token, nearestn, datacenters,
                onlypassing, tags, ttl, regexp, near, nodemeta,
                servicemeta
            )
            return self.agent.http.post(
                CB.json(), path, params=params, headers=headers, data=data)

        def update(self, query_id,
                   service=None,
                   name=None,
                   dc=None,
                   session=None,
                   token=None,
                   nearestn=None,
                   datacenters=None,
                   onlypassing=None,
                   tags=None,
                   ttl=None,
                   regexp=None,
                   near=None,
                   nodemeta=None,
                   servicemeta=None):
            """
            This endpoint will update a certain query

            *query_id* is the query id for update

            all the other setting remains the same as the query create method
            """
            path = '/v1/query/%s' % query_id
            params = []
            headers = {}
            if dc:
                params.append(('dc', dc))
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            data = self._query_data(
                service, name, session, token, nearestn, datacenters,
                onlypassing, tags, ttl, regexp, near, nodemeta,
                servicemeta
            )
            return self.agent.http.put(
                CB.bool(), path, params=params, headers=headers, data=data)

        def get(self,
                query_id,
                token=None,
                dc=None):
            """
            This endpoint will return information about a certain query

            *query_id* the query id to retrieve information about

            *token* is an optional `ACL token`_ to apply to this request.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            return self.agent.http.get(
                CB.json(),
                path='/v1/query/%s' % query_id,
                params=params,
                headers=headers)

        def delete(self, query_id, token=None, dc=None):
            """
            This endpoint will delete certain query

            *query_id* the query id delete

            *token* is an optional `ACL token`_ to apply to this request.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            return self.agent.http.delete(
                CB.bool(), path='/v1/query/%s' % query_id,
                params=params, headers=headers)

        def execute(self,
                    query,
                    token=None,
                    dc=None,
                    near=None,
                    limit=None):
            """
            This endpoint will execute certain query

            *query* name or query id to execute

            *token* is an optional `ACL token`_ to apply to this request.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.

            *near* is a node name to sort the resulting list in ascending
            order based on the estimated round trip time from that node

            *limit* is used to limit the size of the list to the given number
            of nodes. This is applied after any sorting or shuffling.
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            if near:
                params.append(('near', near))
            if limit:
                params.append(('limit', limit))
            return self.agent.http.get(
                CB.json(), path='/v1/query/%s/execute' % query,
                params=params, headers=headers)

        def explain(self,
                    query,
                    token=None,
                    dc=None):
            """
            This endpoint shows a fully-rendered query for a given name

            *query* name to explain. This cannot be query id.

            *token* is an optional `ACL token`_ to apply to this request.

            *dc* is the datacenter that this agent will communicate with. By
            default the datacenter of the host is used.
            """
            params = []
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            return self.agent.http.get(
                CB.json(),
                path='/v1/query/%s/explain' % query,
                params=params,
                headers=headers)

    class Session(object):
        def __init__(self, agent):
            self.agent = agent

        def create(
                self,
                name=None,
                node=None,
                checks=None,
                lock_delay=15,
                behavior='release',
                ttl=None,
                dc=None,
                token=None):
            """
            Creates a new session. There is more documentation for sessions
            `here <https://consul.io/docs/internals/sessions.html>`_.

            *name* is an optional human readable name for the session.

            *node* is the node to create the session on. if not provided the
            current agent's node will be used.

            *checks* is a list of checks to associate with the session. if not
            provided it defaults to the *serfHealth* check. It is highly
            recommended that, if you override this list, you include the
            default *serfHealth*.

            *lock_delay* is an integer of seconds.

            *behavior* can be set to either 'release' or 'delete'. This
            controls the behavior when a session is invalidated. By default,
            this is 'release', causing any locks that are held to be released.
            Changing this to 'delete' causes any locks that are held to be
            deleted. 'delete' is useful for creating ephemeral key/value
            entries.

            when *ttl* is provided, the session is invalidated if it is not
            renewed before the TTL expires.  If specified, it is an integer of
            seconds.  Currently it must be between 10 and 86400 seconds.

            By default the session will be created in the current datacenter
            but an optional *dc* can be provided.

            Returns the string *session_id* for the session.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            data = {}
            if name:
                data['name'] = name
            if node:
                data['node'] = node
            if checks is not None:
                data['checks'] = checks
            if lock_delay != 15:
                data['lockdelay'] = '%ss' % lock_delay
            assert behavior in ('release', 'delete'), \
                'behavior must be release or delete'
            if behavior != 'release':
                data['behavior'] = behavior
            if ttl:
                assert 10 <= ttl <= 86400
                data['ttl'] = '%ss' % ttl
            if data:
                data = json.dumps(data)
            else:
                data = ''

            return self.agent.http.put(
                CB.json(is_id=True),
                path='/v1/session/create',
                params=params,
                headers=headers,
                data=data)

        def destroy(self, session_id, dc=None, token=None):
            """
            Destroys the session *session_id*

            Returns *True* on success.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(
                CB.bool(),
                path='/v1/session/destroy/%s' % session_id,
                params=params,
                headers=headers)

        def list(self,
                 index=None,
                 wait=None,
                 consistency=None,
                 dc=None,
                 token=None):
            """
            Returns a tuple of (*index*, *sessions*) of all active sessions in
            the *dc* datacenter. *dc* defaults to the current datacenter of
            this agent.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.

            The response looks like this::

                (index, [
                    {
                        "LockDelay": 1.5e+10,
                        "Checks": [
                            "serfHealth"
                        ],
                        "Node": "foobar",
                        "ID": "adf4238a-882b-9ddc-4a9d-5b6758e4159e",
                        "CreateIndex": 1086449
                    },
                  pass
               ])
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            return self.agent.http.get(
                CB.json(index=True), path='/v1/session/list',
                params=params, headers=headers)

        def node(self,
                 node,
                 index=None,
                 wait=None,
                 consistency=None,
                 dc=None,
                 token=None):
            """
            Returns a tuple of (*index*, *sessions*) as per *session.list*, but
            filters the sessions returned to only those active for *node*.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            return self.agent.http.get(
                CB.json(index=True),
                path='/v1/session/node/%s' % node,
                params=params, headers=headers)

        def info(self,
                 session_id,
                 index=None,
                 wait=None,
                 consistency=None,
                 dc=None,
                 token=None):
            """
            Returns a tuple of (*index*, *session*) for the session
            *session_id* in the *dc* datacenter. *dc* defaults to the current
            datacenter of this agent.

            *index* is the current Consul index, suitable for making subsequent
            calls to wait for changes since this query was last run.

            *wait* the maximum duration to wait (e.g. '10s') to retrieve
            a given index. this parameter is only applied if *index* is also
            specified. the wait time by default is 5 minutes.

            *consistency* can be either 'default', 'consistent' or 'stale'. if
            not specified *consistency* will the consistency level this client
            was configured with.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            if index:
                params.append(('index', index))
                if wait:
                    params.append(('wait', wait))
            consistency = consistency or self.agent.consistency
            if consistency in ('consistent', 'stale'):
                params.append((consistency, '1'))
            return self.agent.http.get(
                CB.json(index=True, one=True),
                path='/v1/session/info/%s' % session_id,
                params=params, headers=headers)

        def renew(self, session_id, dc=None, token=None):
            """
            This is used with sessions that have a TTL, and it extends the
            expiration by the TTL.

            *dc* is the optional datacenter that you wish to communicate with.
            If None is provided, defaults to the agent's datacenter.

            Returns the session.
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            if dc:
                params.append(('dc', dc))
            return self.agent.http.put(
                CB.json(one=True, allow_404=False),
                path='/v1/session/renew/%s' % session_id,
                params=params, headers=headers)

    class Snapshot(object):
        def __init__(self, agent):
            self.agent = agent

        def get(self, dc=None, stale=None, token=None):
            """
            *dc* Specifies the datacenter to query. This will default
             to the datacenter of the agent being queried.
            *stale* Specifies that any follower may reply. By default
             requests are forwarded to the leade
            Returns gzipped snapshot of current consul cluster
            """
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if stale:
                params.append(('stale', stale))
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.get(
                CB.binary(), path='/v1/snapshot',
                params=params, headers=headers)

        def put(self, data_binary, dc=None, token=None):
            params = []
            headers = {}
            dc = dc or self.agent.dc
            token = token or self.agent.token

            if dc:
                params.append(('dc', dc))
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(
                CB.binary(), path='/v1/snapshot',
                params=params, headers=headers,
                data=data_binary)

        def save(self, file_path):
            """
            Backup snapshot in a file
            """
            backup_file = open(file_path, 'w+b')
            backup_file.write(self.get())
            backup_file.close()
            return True

        def restore(self, file_path):
            """
            Restore from snapshot file
            """
            backup_file = open(file_path, 'rb')
            data_binary = backup_file.read()
            self.put(data_binary)
            backup_file.close()
            return True

    class Status(object):
        """
        The Status endpoints are used to get information about the status
         of the Consul cluster.
        """

        def __init__(self, agent):
            self.agent = agent

        def leader(self):
            """
            This endpoint is used to get the Raft leader for the datacenter
            in which the agent is running.
            """
            return self.agent.http.get(CB.json(), '/v1/status/leader')

        def peers(self):
            """
            This endpoint retrieves the Raft peers for the datacenter in which
            the the agent is running.
            """
            return self.agent.http.get(CB.json(), path='/v1/status/peers')

    class Txn(object):
        """
        The Transactions endpoints manage updates or fetches of multiple keys
        inside a single, atomic transaction.
        """

        def __init__(self, agent):
            self.agent = agent

        def put(self, payload, token=None):
            """
            Create a transaction by submitting a list of operations to apply to
            the KV store inside of a transaction. If any operation fails, the
            transaction is rolled back and none of the changes are applied.

            *payload* is a list of operations where each operation is a `dict`
            with a single key value pair, with the key specifying operation the
            type. An example payload of operation type "KV" is
            dict::

                {
                    "KV": {
                      "Verb": "<verb>",
                      "Key": "<key>",
                      "Value": "<Base64-encoded blob of data>",
                      "Flags": 0,
                      "Index": 0,
                      "Session": "<session id>"
                    }
                }
            """
            headers = {}
            token = token or self.agent.token
            if token:
                headers['X-Consul-Token'] = token
            return self.agent.http.put(CB.json(), path="/v1/txn",
                                       headers=headers,
                                       data=json.dumps(payload))

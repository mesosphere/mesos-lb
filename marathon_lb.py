#!/usr/bin/env python3

"""# marathon-lb
### Overview
The marathon-lb is a service discovery and load balancing tool
for Marathon based on HAProxy. It reads the Marathon task information
and dynamically generates HAProxy configuration details.

To gather the task information, marathon-lb needs to know where
to find Marathon. The service configuration details are stored in labels.

Every service port in Marathon can be configured independently.

### Configuration
Service configuration lives in Marathon via labels.
Marathon-lb just needs to know where to find Marathon.
To run in listening mode you must also specify the address + port at
which marathon-lb can be reached by Marathon.

### Command Line Usage
"""

from operator import attrgetter
from shutil import move
from tempfile import mkstemp
from wsgiref.simple_server import make_server
from six.moves.urllib import parse
from itertools import cycle
from common import *
from config import *

import argparse
import json
import logging
import os
import os.path
import stat
import re
import requests
import shlex
import subprocess
import sys
import socket
import time
import dateutil.parser
import threading
import traceback
import random
import hashlib

logger = logging.getLogger('marathon_lb')


class MarathonBackend(object):

    def __init__(self, host, ip, port, draining):
        self.host = host
        """
        The host that is running this task.
        """

        self.ip = ip
        """
        The IP address used to access the task.  For tasks using IP-per-task,
        this is the actual IP address of the task; otherwise, it is the IP
        address resolved from the hostname.
        """

        self.port = port
        """
        The port used to access a particular service on a task.  For tasks
        using IP-per-task, this is the actual port exposed by the task;
        otherwise, it is the port exposed on the host.
        """

        self.draining = draining
        """
        Whether we should be draining access to this task in the LB.
        """

    def __hash__(self):
        return hash((self.host, self.port))

    def __repr__(self):
        return "MarathonBackend(%r, %r, %r)" % (self.host, self.ip, self.port)


class MarathonService(object):

    def __init__(self, appId, servicePort, healthCheck):
        self.appId = appId
        self.servicePort = servicePort
        self.backends = set()
        self.hostname = None
        self.proxypath = None
        self.revproxypath = None
        self.redirpath = None
        self.haproxy_groups = frozenset()
        self.path = None
        self.sticky = False
        self.redirectHttpToHttps = False
        self.useHsts = False
        self.sslCert = None
        self.bindOptions = None
        self.bindAddr = '*'
        self.groups = frozenset()
        self.mode = 'tcp'
        self.balance = 'roundrobin'
        self.healthCheck = healthCheck
        self.labels = {}
        if healthCheck:
            if healthCheck['protocol'] == 'HTTP':
                self.mode = 'http'

    def add_backend(self, host, ip, port, draining):
        self.backends.add(MarathonBackend(host, ip, port, draining))

    def __hash__(self):
        return hash(self.servicePort)

    def __eq__(self, other):
        return self.servicePort == other.servicePort

    def __repr__(self):
        return "MarathonService(%r, %r)" % (self.appId, self.servicePort)


class MarathonApp(object):

    def __init__(self, marathon, appId, app):
        self.app = app
        self.groups = frozenset()
        self.appId = appId

        # port -> MarathonService
        self.services = dict()

    def __hash__(self):
        return hash(self.appId)

    def __eq__(self, other):
        return self.appId == other.appId


class Marathon(object):

    def __init__(self, hosts, health_check, auth):
        # TODO(cmaloney): Support getting master list from zookeeper
        self.__hosts = hosts
        self.__health_check = health_check
        self.__auth = auth
        self.__cycle_hosts = cycle(self.__hosts)

    def api_req_raw(self, method, path, auth, body=None, **kwargs):
        for host in self.__hosts:
            path_str = os.path.join(host, 'v2')

            for path_elem in path:
                path_str = path_str + "/" + path_elem
            response = requests.request(
                method,
                path_str,
                auth=auth,
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                **kwargs
            )

            logger.debug("%s %s", method, response.url)
            if response.status_code == 200:
                break
        if 'message' in response.json():
            response.reason = "%s (%s)" % (
                response.reason,
                response.json()['message'])
        response.raise_for_status()
        return response

    def api_req(self, method, path, **kwargs):
        return self.api_req_raw(method, path, self.__auth, **kwargs).json()

    def create(self, app_json):
        return self.api_req('POST', ['apps'], app_json)

    def get_app(self, appid):
        logger.info('fetching app %s', appid)
        return self.api_req('GET', ['apps', appid])["app"]

    # Lists all running apps.
    def list(self):
        logger.info('fetching apps')
        return self.api_req('GET', ['apps'],
                            params={'embed': 'apps.tasks'})["apps"]

    def health_check(self):
        return self.__health_check

    def tasks(self):
        logger.info('fetching tasks')
        return self.api_req('GET', ['tasks'])["tasks"]

    def add_subscriber(self, callbackUrl):
        return self.api_req(
                'POST',
                ['eventSubscriptions'],
                params={'callbackUrl': callbackUrl})

    def remove_subscriber(self, callbackUrl):
        return self.api_req(
                'DELETE',
                ['eventSubscriptions'],
                params={'callbackUrl': callbackUrl})

    def get_event_stream(self):
        url = self.host+"/v2/events"
        logger.info(
            "SSE Active, trying fetch events from {0}".format(url))

        headers = {
            'Cache-Control': 'no-cache',
            'Accept': 'text/event-stream'
        }

        resp = requests.get(url, stream=True,
                            headers=headers, auth=self.__auth)

        class Event(object):
            def __init__(self, data):
                self.data = data

        for line in resp.iter_lines():
            if line.strip() != '':
                for real_event_data in re.split(r'\r\n',
                                                line.decode('utf-8')):
                    if real_event_data[:6] == "data: ":
                        event = Event(data=real_event_data[6:])
                        yield event

    @property
    def host(self):
        return next(self.__cycle_hosts)


def has_group(groups, app_groups):
    # All groups / wildcard match
    if '*' in groups:
        return True

    # empty group only
    if len(groups) == 0 and len(app_groups) == 0:
        raise Exception("No groups specified")

    # Contains matching groups
    if (len(frozenset(app_groups) & groups)):
        return True

    return False

ip_cache = dict()


def resolve_ip(host):
    cached_ip = ip_cache.get(host, None)
    if cached_ip:
        return cached_ip
    else:
        try:
            logger.debug("trying to resolve ip address for host %s", host)
            ip = socket.gethostbyname(host)
            ip_cache[host] = ip
            return ip
        except socket.gaierror:
            return None


def config(apps, groups, bind_http_https, ssl_certs, templater):
    logger.info("generating config")
    config = templater.haproxy_head
    groups = frozenset(groups)
    _ssl_certs = ssl_certs or "/etc/ssl/mesosphere.com.pem"
    _ssl_certs = _ssl_certs.split(",")

    if bind_http_https:
        http_frontends = templater.haproxy_http_frontend_head
        https_frontends = templater.haproxy_https_frontend_head.format(
            sslCerts=" ".join(map(lambda cert: "crt " + cert, _ssl_certs))
        )

    frontends = str()
    backends = str()
    http_appid_frontends = templater.haproxy_http_frontend_appid_head
    apps_with_http_appid_backend = []

    for app in sorted(apps, key=attrgetter('appId', 'servicePort')):
        # App only applies if we have it's group
        # Check if there is a haproxy group associated with service group
        # if not fallback to original HAPROXY group.
        # This is added for backward compatability with HAPROXY_GROUP
        if app.haproxy_groups:
            if not has_group(groups, app.haproxy_groups):
                continue
        else:
            if not has_group(groups, app.groups):
                continue

        logger.debug("configuring app %s", app.appId)
        backend = app.appId[1:].replace('/', '_') + '_' + str(app.servicePort)

        logger.debug("frontend at %s:%d with backend %s",
                     app.bindAddr, app.servicePort, backend)

        # if the app has a hostname set force mode to http
        # otherwise recent versions of haproxy refuse to start
        if app.hostname:
            app.mode = 'http'

        frontend_head = templater.haproxy_frontend_head(app)
        frontends += frontend_head.format(
            bindAddr=app.bindAddr,
            backend=backend,
            servicePort=app.servicePort,
            mode=app.mode,
            sslCert=' ssl crt ' + app.sslCert if app.sslCert else '',
            bindOptions=' ' + app.bindOptions if app.bindOptions else ''
        )

        backend_head = templater.haproxy_backend_head(app)
        backends += backend_head.format(
            backend=backend,
            balance=app.balance,
            mode=app.mode
        )

        # if a hostname is set we add the app to the vhost section
        # of our haproxy config
        # TODO(lloesche): Check if the hostname is already defined by another
        # service
        if bind_http_https and app.hostname:
            p_fe, s_fe = generateHttpVhostAcl(templater, app, backend)
            http_frontends += p_fe
            https_frontends += s_fe

        # if app mode is http, we add the app to the second http frontend
        # selecting apps by http header X-Marathon-App-Id
        if app.mode == 'http' and \
                app.appId not in apps_with_http_appid_backend:
            logger.debug("adding virtual host for app with id %s", app.appId)
            # remember appids to prevent multiple entries for the same app
            apps_with_http_appid_backend += [app.appId]
            cleanedUpAppId = re.sub(r'[^a-zA-Z0-9\-]', '_', app.appId)

            http_appid_frontend_acl = templater \
                .haproxy_http_frontend_appid_acl(app)
            http_appid_frontends += http_appid_frontend_acl.format(
                cleanedUpAppId=cleanedUpAppId,
                hostname=app.hostname,
                appId=app.appId,
                backend=backend
            )

        if app.mode == 'http':
            if app.useHsts:
                backends += templater.haproxy_backend_hsts_options(app)
            backends += templater.haproxy_backend_http_options(app)
            backend_http_backend_proxypass = templater \
                .haproxy_http_backend_proxypass(app)
            if app.proxypath:
                backends += backend_http_backend_proxypass.format(
                    hostname=app.hostname,
                    proxypath=app.proxypath
                )
            backend_http_backend_revproxy = templater \
                .haproxy_http_backend_revproxy(app)
            if app.revproxypath:
                backends += backend_http_backend_revproxy.format(
                    hostname=app.hostname,
                    rootpath=app.revproxypath
                )
            backend_http_backend_redir = templater \
                .haproxy_http_backend_redir(app)
            if app.redirpath:
                backends += backend_http_backend_redir.format(
                    hostname=app.hostname,
                    redirpath=app.redirpath
                )

        if app.healthCheck:
            health_check_options = None
            if app.mode == 'tcp' or app.healthCheck['protocol'] == 'TCP':
                health_check_options = templater \
                    .haproxy_backend_tcp_healthcheck_options(app)
            elif app.mode == 'http':
                health_check_options = templater \
                    .haproxy_backend_http_healthcheck_options(app)
            if health_check_options:
                healthCheckPort = app.healthCheck.get('port')
                backends += health_check_options.format(
                    healthCheck=app.healthCheck,
                    healthCheckPortIndex=app.healthCheck.get('portIndex'),
                    healthCheckPort=healthCheckPort,
                    healthCheckProtocol=app.healthCheck['protocol'],
                    healthCheckPath=app.healthCheck.get('path', '/'),
                    healthCheckTimeoutSeconds=app.healthCheck[
                        'timeoutSeconds'],
                    healthCheckIntervalSeconds=app.healthCheck[
                        'intervalSeconds'],
                    healthCheckIgnoreHttp1xx=app.healthCheck['ignoreHttp1xx'],
                    healthCheckGracePeriodSeconds=app.healthCheck[
                        'gracePeriodSeconds'],
                    healthCheckMaxConsecutiveFailures=app.healthCheck[
                        'maxConsecutiveFailures'],
                    healthCheckFalls=app.healthCheck[
                        'maxConsecutiveFailures'] + 1,
                    healthCheckPortOptions=' port ' +
                    str(healthCheckPort) if healthCheckPort else ''
                )

        if app.sticky:
            logger.debug("turning on sticky sessions")
            backends += templater.haproxy_backend_sticky_options(app)

        frontend_backend_glue = templater.haproxy_frontend_backend_glue(app)
        frontends += frontend_backend_glue.format(backend=backend)

        key_func = attrgetter('host', 'port')
        for backendServer in sorted(app.backends, key=key_func):
            logger.debug(
                "backend server %s:%d on %s",
                backendServer.ip,
                backendServer.port,
                backendServer.host)
            serverName = re.sub(
                r'[^a-zA-Z0-9\-]', '_',
                (backendServer.host + '_' +
                 backendServer.ip + '_' +
                 str(backendServer.port)))
            shortHashedServerName = hashlib.sha1(serverName.encode()) \
                .hexdigest()[:10]

            healthCheckOptions = None
            if app.healthCheck:
                server_health_check_options = None
                if app.mode == 'tcp' or app.healthCheck['protocol'] == 'TCP':
                    server_health_check_options = templater \
                        .haproxy_backend_server_tcp_healthcheck_options(app)
                elif app.mode == 'http':
                    server_health_check_options = templater \
                        .haproxy_backend_server_http_healthcheck_options(app)
                if server_health_check_options:
                    healthCheckPort = app.healthCheck.get('port')
                    healthCheckOptions = server_health_check_options.format(
                        healthCheck=app.healthCheck,
                        healthCheckPortIndex=app.healthCheck.get('portIndex'),
                        healthCheckPort=healthCheckPort,
                        healthCheckProtocol=app.healthCheck['protocol'],
                        healthCheckPath=app.healthCheck.get('path', '/'),
                        healthCheckTimeoutSeconds=app.healthCheck[
                            'timeoutSeconds'],
                        healthCheckIntervalSeconds=app.healthCheck[
                            'intervalSeconds'],
                        healthCheckIgnoreHttp1xx=app.healthCheck[
                            'ignoreHttp1xx'],
                        healthCheckGracePeriodSeconds=app.healthCheck[
                            'gracePeriodSeconds'],
                        healthCheckMaxConsecutiveFailures=app.healthCheck[
                            'maxConsecutiveFailures'],
                        healthCheckFalls=app.healthCheck[
                            'maxConsecutiveFailures'] + 1,
                        healthCheckPortOptions=' port ' +
                        str(healthCheckPort) if healthCheckPort else ''
                    )
            backend_server_options = templater \
                .haproxy_backend_server_options(app)
            backends += backend_server_options.format(
                host=backendServer.host,
                host_ipv4=backendServer.ip,
                port=backendServer.port,
                serverName=serverName,
                cookieOptions=' check cookie ' +
                shortHashedServerName if app.sticky else '',
                healthCheckOptions=healthCheckOptions
                if healthCheckOptions else '',
                otherOptions=' disabled' if backendServer.draining else ''
            )

    if bind_http_https:
        config += http_frontends
    config += http_appid_frontends
    if bind_http_https:
        config += https_frontends
    config += frontends
    config += backends

    return config


def get_haproxy_pids():
    try:
        return subprocess.check_output(
            "pidof haproxy",
            stderr=subprocess.STDOUT,
            shell=True)
    except subprocess.CalledProcessError as ex:
        return ''


def reloadConfig():
    reloadCommand = []
    if args.command:
        reloadCommand = shlex.split(args.command)
    else:
        logger.debug("No reload command provided, trying to find out how to" +
                     " reload the configuration")
        if os.path.isfile('/etc/init/haproxy.conf'):
            logger.debug("we seem to be running on an Upstart based system")
            reloadCommand = ['reload', 'haproxy']
        elif (os.path.isfile('/usr/lib/systemd/system/haproxy.service') or
              os.path.isfile('/etc/systemd/system/haproxy.service')):
            logger.debug("we seem to be running on systemd based system")
            reloadCommand = ['systemctl', 'reload', 'haproxy']
        elif os.path.isfile('/etc/init.d/haproxy'):
            logger.debug("we seem to be running on a sysvinit based system")
            reloadCommand = ['/etc/init.d/haproxy', 'reload']
        else:
            # if no haproxy exists (maybe running in a container)
            logger.debug("no haproxy detected. won't reload.")
            reloadCommand = None

    if reloadCommand:
        logger.info("reloading using %s", " ".join(reloadCommand))
        try:
            start_time = time.time()
            pids = get_haproxy_pids()
            subprocess.check_call(reloadCommand, close_fds=True)
            # Wait until the reload actually occurs
            while pids == get_haproxy_pids():
                time.sleep(0.1)
            logger.debug("reload finished, took %s seconds",
                         time.time() - start_time)
        except OSError as ex:
            logger.error("unable to reload config using command %s",
                         " ".join(reloadCommand))
            logger.error("OSError: %s", ex)
        except subprocess.CalledProcessError as ex:
            logger.error("unable to reload config using command %s",
                         " ".join(reloadCommand))
            logger.error("reload returned non-zero: %s", ex)


def generateHttpVhostAcl(templater, app, backend):
    # If the hostname contains the delimiter ',', then the marathon app is
    # requesting multiple hostname matches for the same backend, and we need
    # to use alternate templates from the default one-acl/one-use_backend.
    staging_http_frontends = ""
    staging_https_frontends = ""

    if "," in app.hostname:
        logger.debug(
            "vhost label specifies multiple hosts: %s", app.hostname)
        vhosts = app.hostname.split(',')
        acl_name = re.sub(r'[^a-zA-Z0-9\-]', '_', vhosts[0]) + \
            '_' + app.appId[1:].replace('/', '_')

        if app.path:
            # Set the path ACL if it exists
            logger.debug("adding path acl, path=%s", app.path)
            http_frontend_acl = \
                templater.haproxy_http_frontend_acl_only_with_path(app)
            staging_http_frontends += http_frontend_acl.format(
                path=app.path,
                backend=backend
            )
            https_frontend_acl = \
                templater.haproxy_https_frontend_acl_only_with_path(app)
            staging_https_frontends += https_frontend_acl.format(
                path=app.path,
                backend=backend
            )

        for vhost_hostname in vhosts:
            logger.debug("processing vhost %s", vhost_hostname)
            http_frontend_acl = templater.haproxy_http_frontend_acl_only(app)
            staging_http_frontends += http_frontend_acl.format(
                cleanedUpHostname=acl_name,
                hostname=vhost_hostname
            )

            # Tack on the SSL ACL as well
            if app.path:
                https_frontend_acl = \
                    templater.haproxy_https_frontend_acl_with_path(app)
                staging_https_frontends += https_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=vhost_hostname,
                    appId=app.appId,
                    backend=backend
                )
            else:
                https_frontend_acl = templater.haproxy_https_frontend_acl(app)
                staging_https_frontends += https_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=vhost_hostname,
                    appId=app.appId,
                    backend=backend
                )

        # We've added the http acl lines, now route them to the same backend
        if app.redirectHttpToHttps:
            logger.debug("writing rule to redirect http to https traffic")
            if app.path:
                haproxy_backend_redirect_http_to_https = \
                    templater.\
                    haproxy_backend_redirect_http_to_https_with_path(app)
                frontend = haproxy_backend_redirect_http_to_https.format(
                    bindAddr=app.bindAddr,
                    cleanedUpHostname=acl_name,
                    backend=backend
                )
                staging_http_frontends += frontend
            else:
                haproxy_backend_redirect_http_to_https = \
                    templater.haproxy_backend_redirect_http_to_https(app)
                frontend = haproxy_backend_redirect_http_to_https.format(
                    bindAddr=app.bindAddr,
                    cleanedUpHostname=acl_name
                )
                staging_http_frontends += frontend
        elif app.path:
            http_frontend_route = \
                templater.haproxy_http_frontend_routing_only_with_path(app)
            staging_http_frontends += http_frontend_route.format(
                cleanedUpHostname=acl_name,
                backend=backend
            )
        else:
            http_frontend_route = \
                templater.haproxy_http_frontend_routing_only(app)
            staging_http_frontends += http_frontend_route.format(
                cleanedUpHostname=acl_name,
                backend=backend
            )

    else:
        # A single hostname in the VHOST label
        logger.debug(
            "adding virtual host for app with hostname %s", app.hostname)
        acl_name = re.sub(r'[^a-zA-Z0-9\-]', '_', app.hostname) + \
            '_' + app.appId[1:].replace('/', '_')

        if app.path:
            if app.redirectHttpToHttps:
                http_frontend_acl = \
                    templater.haproxy_http_frontend_acl_only(app)
                staging_http_frontends += http_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=app.hostname
                )
                http_frontend_acl = \
                    templater.haproxy_http_frontend_acl_only_with_path(app)
                staging_http_frontends += http_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=app.hostname,
                    path=app.path,
                    backend=backend
                )
                haproxy_backend_redirect_http_to_https = \
                    templater.\
                    haproxy_backend_redirect_http_to_https_with_path(app)
                frontend = haproxy_backend_redirect_http_to_https.format(
                    bindAddr=app.bindAddr,
                    cleanedUpHostname=acl_name,
                    backend=backend
                )
                staging_http_frontends += frontend
            else:
                http_frontend_acl = \
                    templater.haproxy_http_frontend_acl_with_path(app)
                staging_http_frontends += http_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=app.hostname,
                    path=app.path,
                    appId=app.appId,
                    backend=backend
                )
            https_frontend_acl = \
                templater.haproxy_https_frontend_acl_only_with_path(app)
            staging_https_frontends += https_frontend_acl.format(
                path=app.path,
                backend=backend
            )
            https_frontend_acl = \
                templater.haproxy_https_frontend_acl_with_path(app)
            staging_https_frontends += https_frontend_acl.format(
                cleanedUpHostname=acl_name,
                hostname=app.hostname,
                appId=app.appId,
                backend=backend
            )
        else:
            if app.redirectHttpToHttps:
                http_frontend_acl = \
                    templater.haproxy_http_frontend_acl_only(app)
                staging_http_frontends += http_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=app.hostname
                )
                haproxy_backend_redirect_http_to_https = \
                    templater.\
                    haproxy_backend_redirect_http_to_https(app)
                frontend = haproxy_backend_redirect_http_to_https.format(
                    bindAddr=app.bindAddr,
                    cleanedUpHostname=acl_name
                )
                staging_http_frontends += frontend
            else:
                http_frontend_acl = templater.haproxy_http_frontend_acl(app)
                staging_http_frontends += http_frontend_acl.format(
                    cleanedUpHostname=acl_name,
                    hostname=app.hostname,
                    appId=app.appId,
                    backend=backend
                )
            https_frontend_acl = templater.haproxy_https_frontend_acl(app)
            staging_https_frontends += https_frontend_acl.format(
                cleanedUpHostname=acl_name,
                hostname=app.hostname,
                appId=app.appId,
                backend=backend
            )

    return (staging_http_frontends, staging_https_frontends)


def writeConfigAndValidate(config, config_file):
    # Test run, print to stdout and exit
    if args.dry:
        print(config)
        sys.exit()
    # Write config to a temporary location
    fd, haproxyTempConfigFile = mkstemp()
    logger.debug("writing config to temp file %s", haproxyTempConfigFile)
    with os.fdopen(fd, 'w') as haproxyTempConfig:
        haproxyTempConfig.write(config)

    # Ensure new config is created with the same
    # permissions the old file had or use defaults
    # if config file doesn't exist yet
    perms = 0o644
    if os.path.isfile(config_file):
        perms = stat.S_IMODE(os.lstat(config_file).st_mode)
    os.chmod(haproxyTempConfigFile, perms)

    # If skip validation flag is provided, don't check.
    if args.skip_validation:
        logger.debug("skipping validation. moving temp file %s to %s",
                     haproxyTempConfigFile,
                     config_file)
        move(haproxyTempConfigFile, config_file)
        return True

    # Check that config is valid
    cmd = ['haproxy', '-f', haproxyTempConfigFile, '-c']
    logger.debug("checking config with command: " + str(cmd))
    returncode = subprocess.call(args=cmd)
    if returncode == 0:
        # Move into place
        logger.debug("moving temp file %s to %s",
                     haproxyTempConfigFile,
                     config_file)
        move(haproxyTempConfigFile, config_file)
        return True
    else:
        logger.error("haproxy returned non-zero when checking config")
        return False


def compareWriteAndReloadConfig(config, config_file):
    # See if the last config on disk matches this, and if so don't reload
    # haproxy
    runningConfig = str()
    try:
        logger.debug("reading running config from %s", config_file)
        with open(config_file, "r") as f:
            runningConfig = f.read()
    except IOError:
        logger.warning("couldn't open config file for reading")

    if runningConfig != config:
        logger.info(
            "running config is different from generated config - reloading")
        if writeConfigAndValidate(config, config_file):
            reloadConfig()
        else:
            logger.warning("skipping reload: config not valid")


def get_health_check(app, portIndex):
    for check in app['healthChecks']:
        if check.get('port'):
            return check
        if check.get('portIndex') == portIndex:
            return check
    return None


def is_ip_per_task(app):
    """
    Return whether the application is using IP-per-task.
    :param app:  The application to check.
    :return:  True if using IP per task, False otherwise.
    """
    return app.get('ipAddress') is not None


def get_task_ip_and_ports(app, task):
    """
    Return the IP address and list of ports used to access a task.  For a
    task using IP-per-task, this is the IP address of the task, and the ports
    exposed by the task services.  Otherwise, this is the IP address of the
    host and the ports exposed by the host.
    :param app: The application owning the task.
    :param task: The task.
    :return: Tuple of (ip address, [ports]).  Returns (None, None) if no IP
    address could be resolved or found for the task.
    """
    # If the app ipAddress field is present and not None then this app is using
    # IP per task.  The ipAddress may be an empty dictionary though, in which
    # case there are no discovery ports.  At the moment, Mesos only supports a
    # single IP address, so just take the first IP in the list.
    if is_ip_per_task(app):
        logger.debug("Using IP per container")
        task_ip_addresses = task.get('ipAddresses')
        if not task_ip_addresses:
            logger.warning("Task %s does not yet have an ip address allocated",
                           task['id'])
            return None, None
        task_ip = task_ip_addresses[0]['ipAddress']

        discovery = app['ipAddress'].get('discovery', {})
        task_ports = [int(port['number'])
                      for port in discovery.get('ports', [])]
    else:
        logger.debug("Using host port mapping")
        task_ports = task.get('ports', [])
        task_ip = resolve_ip(task['host'])
        if not task_ip:
            logger.warning("Could not resolve ip for host %s, ignoring",
                           task['host'])
            return None, None

    logger.debug("Returning: %r, %r", task_ip, task_ports)
    return task_ip, task_ports


class ServicePortAssigner(object):
    """
    Helper class to assign service ports.

    Ordinarily Marathon should assign the service ports, but Marathon issue
    https://github.com/mesosphere/marathon/issues/3636 means the service ports
    are not returned for applications using IP-per-task.  We work around that
    here by assigning ports arbitrary ports when required.  We only assign
    ports if the command line options --min-serv-port-ip-per-task and
    --max-serv-port-ip-per-task are both specified.

    Note that auto-assigning ports is only really useful when using vhost,
    otherwise your application needs to know the port (which we don't expose).
    """
    def __init__(self):
        self.can_assign = False
        self.min_port = None
        self.max_port = None
        self.next_port = None
        self.ports_by_app = {}
        self.old_ports_by_app = {}

    def set_ports(self, min_port, max_port):
        self.can_assign = min_port and max_port
        self.min_port = min_port
        self.max_port = max_port
        self.next_port = min_port

    def mark(self):
        self.old_ports_by_app = self.ports_by_app
        self.ports_by_app = {}

    def sweep(self):
        self.old_ports_by_app = {}

    def _assign_new_service_port(self):
        assert self.can_assign
        wrapped = False
        ports = self.ports_by_app.values()
        old_ports = self.old_ports_by_app.values()
        while True:
            port = self.next_port
            self.next_port += 1
            if self.next_port > self.max_port:
                self.next_port = self.min_port
                assert not wrapped, "Service ports are exhausted"
                wrapped = True
            if port not in ports and port not in old_ports:
                break

        logger.debug("Assigned new port: %d", port)
        return port

    def _get_service_port(self, app, task_port):
        key = (app['id'], task_port)
        port = (self.ports_by_app.get(key) or
                self.old_ports_by_app.get(key) or
                self._assign_new_service_port())
        self.ports_by_app[key] = port
        return port

    def get_service_ports(self, app):
        ports = app['ports']
        if not ports and is_ip_per_task(app) and self.can_assign:
            logger.warning("Auto assigning service port for "
                           "IP-per-container task")
            task = app['tasks'][0]
            _, task_ports = get_task_ip_and_ports(app, task)
            ports = [self._get_service_port(app, task_port)
                     for task_port in task_ports]
        logger.debug("Service ports: %r", ports)
        return ports

SERVICE_PORT_ASSIGNER = ServicePortAssigner()


def get_apps(marathon):
    apps = marathon.list()
    logger.debug("got apps %s", [app["id"] for app in apps])

    marathon_apps = []
    # This process requires 2 passes: the first is to gather apps belonging
    # to a deployment group.
    processed_apps = []
    deployment_groups = {}
    for app in apps:
        deployment_group = None
        if 'HAPROXY_DEPLOYMENT_GROUP' in app['labels']:
            deployment_group = app['labels']['HAPROXY_DEPLOYMENT_GROUP']
            # mutate the app id to match deployment group
            if deployment_group[0] != '/':
                deployment_group = '/' + deployment_group
            app['id'] = deployment_group
        else:
            processed_apps.append(app)
            continue
        if deployment_group in deployment_groups:
            # merge the groups, with the oldest taking precedence
            prev = deployment_groups[deployment_group]
            cur = app

            # TODO(brenden): do something more intelligent when the label is
            # missing.
            if 'HAPROXY_DEPLOYMENT_STARTED_AT' in prev['labels']:
                prev_date = dateutil.parser.parse(
                    prev['labels']['HAPROXY_DEPLOYMENT_STARTED_AT'])
            else:
                prev_date = ''
            if 'HAPROXY_DEPLOYMENT_STARTED_AT' in cur['labels']:
                cur_date = dateutil.parser.parse(
                    cur['labels']['HAPROXY_DEPLOYMENT_STARTED_AT'])
            else:
                cur_date = ''

            old = new = None
            if prev_date < cur_date:
                old = prev
                new = cur
            else:
                new = prev
                old = cur

            target_instances = \
                int(new['labels']['HAPROXY_DEPLOYMENT_TARGET_INSTANCES'])

            # Mark N tasks from old app as draining, where N is the
            # number of instances in the new app.  Sort the old tasks so that
            # order is deterministic (i.e. so that we always drain the same
            # tasks).
            old_tasks = sorted(old['tasks'], key=lambda task: task['id'])

            healthy_new_instances = 0
            if len(app['healthChecks']) > 0:
                for task in new['tasks']:
                    if 'healthCheckResults' not in task:
                        continue
                    alive = True
                    for result in task['healthCheckResults']:
                        if not result['alive']:
                            alive = False
                    if alive:
                        healthy_new_instances += 1
            else:
                healthy_new_instances = new['instances']

            maximum_drainable = \
                max(0, (healthy_new_instances + old['instances']) -
                    target_instances)

            for i in range(0, min(len(old_tasks),
                                  healthy_new_instances,
                                  maximum_drainable)):
                old_tasks[i]['draining'] = True

            # merge tasks from new app into old app
            merged = old
            old_tasks.extend(new['tasks'])
            merged['tasks'] = old_tasks

            deployment_groups[deployment_group] = merged
        else:
            deployment_groups[deployment_group] = app

    processed_apps.extend(deployment_groups.values())

    # Mark existing auto-assigned service ports for deletion.  If they are
    # re-used, they will be "un-marked".  Any marked service ports will
    # be deleted after we have processed all the apps.
    SERVICE_PORT_ASSIGNER.mark()

    for app in processed_apps:
        appId = app['id']
        if appId[1:] == os.environ.get("FRAMEWORK_NAME"):
            continue

        marathon_app = MarathonApp(marathon, appId, app)

        if 'HAPROXY_GROUP' in marathon_app.app['labels']:
            marathon_app.groups = \
                marathon_app.app['labels']['HAPROXY_GROUP'].split(',')
        marathon_apps.append(marathon_app)

        service_ports = SERVICE_PORT_ASSIGNER.get_service_ports(app)
        for i in range(len(service_ports)):
            servicePort = service_ports[i]
            service = MarathonService(
                        appId, servicePort, get_health_check(app, i))

            for key_unformatted in label_keys:
                key = key_unformatted.format(i)
                if key in marathon_app.app['labels']:
                    func = label_keys[key_unformatted]
                    func(service,
                         key_unformatted,
                         marathon_app.app['labels'][key])

            marathon_app.services[servicePort] = service

        for task in app['tasks']:
            # Marathon 0.7.6 bug workaround
            if not task['host']:
                logger.warning("Ignoring Marathon task without host " +
                               task['id'])
                continue

            if marathon.health_check() and 'healthChecks' in app and \
               len(app['healthChecks']) > 0:
                if 'healthCheckResults' not in task:
                    continue
                alive = True
                for result in task['healthCheckResults']:
                    if not result['alive']:
                        alive = False
                if not alive:
                    continue

            task_ip, task_ports = get_task_ip_and_ports(app, task)
            if not task_ip:
                logger.warning("Task has no resolvable IP address - skip")
                continue

            draining = task.get('draining', False)

            # if different versions of app have different number of ports,
            # try to match as many ports as possible
            for task_port, service_port in zip(task_ports, service_ports):
                service = marathon_app.services.get(service_port, None)
                if service:
                    service.groups = marathon_app.groups
                    service.add_backend(task['host'],
                                        task_ip,
                                        task_port,
                                        draining)

    # Sweep up services ports that are no longer required.
    SERVICE_PORT_ASSIGNER.sweep()

    # Convert into a list for easier consumption
    apps_list = []
    for marathon_app in marathon_apps:
        for service in list(marathon_app.services.values()):
            if service.backends:
                apps_list.append(service)

    return apps_list


def regenerate_config(apps, config_file, groups, bind_http_https,
                      ssl_certs, templater):
    compareWriteAndReloadConfig(config(apps, groups, bind_http_https,
                                ssl_certs, templater), config_file)


class MarathonEventProcessor(object):

    def __init__(self, marathon, config_file, groups,
                 bind_http_https, ssl_certs):
        self.__marathon = marathon
        # appId -> MarathonApp
        self.__apps = dict()
        self.__config_file = config_file
        self.__groups = groups
        self.__templater = ConfigTemplater()
        self.__bind_http_https = bind_http_https
        self.__ssl_certs = ssl_certs

        self.__condition = threading.Condition()
        self.__thread = threading.Thread(target=self.do_reset)
        self.__pending_reset = False
        self.__stop = False
        self.__thread.start()

        # Fetch the base data
        self.reset_from_tasks()

    def do_reset(self):
        with self.__condition:
            logger.info('starting event processor thread')
            while True:
                self.__condition.acquire()
                if self.__stop:
                    logger.info('stopping event processor thread')
                    return
                if not self.__pending_reset:
                    if not self.__condition.wait(300):
                        logger.info('condition wait expired')
                self.__pending_reset = False
                self.__condition.release()

                try:
                    start_time = time.time()

                    self.__apps = get_apps(self.__marathon)
                    regenerate_config(self.__apps,
                                      self.__config_file,
                                      self.__groups,
                                      self.__bind_http_https,
                                      self.__ssl_certs,
                                      self.__templater)

                    logger.debug("updating tasks finished, took %s seconds",
                                 time.time() - start_time)
                except requests.exceptions.ConnectionError as e:
                    logger.error("Connection error({0}): {1}".format(
                        e.errno, e.strerror))
                except:
                    logger.exception("Unexpected error!")

    def stop(self):
        self.__condition.acquire()
        self.__stop = True
        self.__condition.notify()
        self.__condition.release()

    def reset_from_tasks(self):
        self.__condition.acquire()
        self.__pending_reset = True
        self.__condition.notify()
        self.__condition.release()

    def handle_event(self, event):
        if event['eventType'] == 'status_update_event' or \
                event['eventType'] == 'health_status_changed_event' or \
                event['eventType'] == 'api_post_event':
            self.reset_from_tasks()


def get_arg_parser():
    parser = argparse.ArgumentParser(
        description="Marathon HAProxy Load Balancer",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--longhelp",
                        help="Print out configuration details",
                        action="store_true"
                        )
    parser.add_argument("--marathon", "-m",
                        nargs="+",
                        help="[required] Marathon endpoint, eg. -m " +
                             "http://marathon1:8080 -m http://marathon2:8080"
                        )
    parser.add_argument("--listening", "-l",
                        help="The address this script listens on for " +
                        "marathon events (e.g., http://0.0.0.0:8080)"
                        )
    parser.add_argument("--callback-url", "-u",
                        help="The HTTP address that Marathon can call this " +
                             "script back at (http://lb1:8080)"
                        )
    parser.add_argument("--haproxy-config",
                        help="Location of haproxy configuration",
                        default="/etc/haproxy/haproxy.cfg"
                        )
    parser.add_argument("--group",
                        help="[required] Only generate config for apps which"
                        " list the specified names. Use '*' to match all"
                        " groups",
                        action="append",
                        default=list())
    parser.add_argument("--command", "-c",
                        help="If set, run this command to reload haproxy.",
                        default=None)
    parser.add_argument("--sse", "-s",
                        help="Use Server Sent Events instead of HTTP "
                        "Callbacks",
                        action="store_true")
    parser.add_argument("--health-check", "-H",
                        help="If set, respect Marathon's health check "
                        "statuses before adding the app instance into "
                        "the backend pool.",
                        action="store_true")
    parser.add_argument("--dont-bind-http-https",
                        help="Don't bind to HTTP and HTTPS frontends.",
                        action="store_true")
    parser.add_argument("--ssl-certs",
                        help="List of SSL certificates separated by comma"
                             "for frontend marathon_https_in"
                             "Ex: /etc/ssl/site1.co.pem,/etc/ssl/site2.co.pem",
                        default="/etc/ssl/mesosphere.com.pem")
    parser.add_argument("--skip-validation",
                        help="Skip haproxy config file validation",
                        action="store_true")
    parser.add_argument("--dry", "-d",
                        help="Only print configuration to console",
                        action="store_true")
    parser.add_argument("--min-serv-port-ip-per-task",
                        help="Minimum port number to use when auto-assigning "
                             "service ports for IP-per-task applications")
    parser.add_argument("--max-serv-port-ip-per-task",
                        help="Maximum port number to use when auto-assigning "
                             "service ports for IP-per-task applications")
    parser = set_logging_args(parser)
    parser = set_marathon_auth_args(parser)
    return parser


def run_server(marathon, listen_addr, callback_url, config_file, groups,
               bind_http_https, ssl_certs):
    processor = MarathonEventProcessor(marathon,
                                       config_file,
                                       groups,
                                       bind_http_https,
                                       ssl_certs)
    try:
        marathon.add_subscriber(callback_url)

        # TODO(cmaloney): Switch to a sane http server
        # TODO(cmaloney): Good exception catching, etc
        def wsgi_app(env, start_response):
            length = int(env['CONTENT_LENGTH'])
            data = env['wsgi.input'].read(length)
            processor.handle_event(json.loads(data.decode('utf-8')))
            # TODO(cmaloney): Make this have a simple useful webui for
            # debugging / monitoring
            start_response('200 OK', [('Content-Type', 'text/html')])

            return ["Got it\n".encode('utf-8')]

        listen_uri = parse.urlparse(listen_addr)
        httpd = make_server(listen_uri.hostname, listen_uri.port, wsgi_app)
        httpd.serve_forever()
    finally:
        processor.stop()


def clear_callbacks(marathon, callback_url):
    logger.info("Cleanup, removing subscription to {0}".format(callback_url))
    marathon.remove_subscriber(callback_url)


def process_sse_events(marathon, config_file, groups,
                       bind_http_https, ssl_certs):
    processor = MarathonEventProcessor(marathon,
                                       config_file,
                                       groups,
                                       bind_http_https,
                                       ssl_certs)
    try:
        events = marathon.get_event_stream()
        for event in events:
            try:
                # logger.info("received event: {0}".format(event))
                # marathon might also send empty messages as keepalive...
                if (event.data.strip() != ''):
                    # marathon sometimes sends more than one json per event
                    # e.g. {}\r\n{}\r\n\r\n
                    for real_event_data in re.split(r'\r\n', event.data):
                        data = json.loads(real_event_data)
                        logger.info(
                            "received event of type {0}"
                            .format(data['eventType']))
                        processor.handle_event(data)
                else:
                    logger.info("skipping empty message")
            except:
                print(event.data)
                print("Unexpected error:", sys.exc_info()[0])
                traceback.print_stack()
                raise
    finally:
        processor.stop()


if __name__ == '__main__':
    # Process arguments
    arg_parser = get_arg_parser()
    args = arg_parser.parse_args()

    # Print the long help text if flag is set
    if args.longhelp:
        print(__doc__)
        print('```')
        arg_parser.print_help()
        print('```')
        print(ConfigTemplater().get_descriptions())
        sys.exit()
    # otherwise make sure that a Marathon URL was specified
    else:
        if args.marathon is None:
            arg_parser.error('argument --marathon/-m is required')
        if args.sse and args.listening:
            arg_parser.error(
                'cannot use --listening and --sse at the same time')
        if bool(args.min_serv_port_ip_per_task) != \
           bool(args.max_serv_port_ip_per_task):
            arg_parser.error(
                'either specific both --min-serv-port-ip-per-task '
                'and --max-serv-port-ip-per-task or neither')
        if len(args.group) == 0:
            arg_parser.error('argument --group is required: please' +
                             'specify at least one group name')

    # Configure the service port assigner if min/max ports have been specified.
    if args.min_serv_port_ip_per_task and args.max_serv_port_ip_per_task:
        SERVICE_PORT_ASSIGNER.set_ports(int(args.min_serv_port_ip_per_task),
                                        int(args.max_serv_port_ip_per_task))

    # Set request retries
    s = requests.Session()
    a = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount('http://', a)

    # Setup logging
    setup_logging(logger, args.syslog_socket, args.log_format)

    # Marathon API connector
    marathon = Marathon(args.marathon,
                        args.health_check,
                        get_marathon_auth_params(args))

    # If in listening mode, spawn a webserver waiting for events. Otherwise
    # just write the config.
    if args.listening:
        callback_url = args.callback_url or args.listening
        try:
            run_server(marathon, args.listening, callback_url,
                       args.haproxy_config, args.group,
                       not args.dont_bind_http_https, args.ssl_certs)
        finally:
            clear_callbacks(marathon, callback_url)
    elif args.sse:
        backoff = 3
        while True:
            stream_started = time.time()
            try:
                process_sse_events(marathon,
                                   args.haproxy_config,
                                   args.group,
                                   not args.dont_bind_http_https,
                                   args.ssl_certs)
            except:
                logger.exception("Caught exception")
                backoff = backoff * 1.5
                if backoff > 300:
                    backoff = 300
                logger.error("Reconnecting in {}s...", backoff)
            # Reset the backoff if it's been more than 10 minutes
            if time.time() - stream_started > 600:
                backoff = 3
            time.sleep(random.random() * backoff)
    else:
        # Generate base config
        regenerate_config(get_apps(marathon), args.haproxy_config, args.group,
                          not args.dont_bind_http_https,
                          args.ssl_certs, ConfigTemplater())

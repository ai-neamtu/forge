# Copyright 2017 datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Forge CLI.

Usage:
  forge setup
  forge pull [--config=<config>] [--filter=<pattern>]
  forge bake [--config=<config>]
  forge push [--config=<config>]
  forge yaml [--config=<config>]
  forge build [--config=<config>]
  forge deploy [--config=<config>] [--dry-run]
  forge create <prototype> <arguments> [-o,--output <target>]
  forge serve [--config=<config>]
  forge -h | --help
  forge --version

Options:
  --config=<config>     Forge config file location.
  --filter=<pattern>    Only operate on services matching <pattern>. [default: *]
  -h --help             Show this screen.
  --version             Show version.
"""

import eventlet
eventlet.sleep() # workaround for import cycle: https://github.com/eventlet/eventlet/issues/401
eventlet.monkey_patch()

import getpass
getpass.os = eventlet.patcher.original('os') # workaround for https://github.com/eventlet/eventlet/issues/340

import base64, fnmatch, requests, os, sys, time, urllib2, yaml
from blessed import Terminal
from docopt import docopt
from collections import OrderedDict
from jinja2 import Template

import util
from ._metadata import __version__
from .workstream import Workstream, Elidable, Secret, Command, WorkError
from .common import Service, Prototype, image, containers

class CLIError(Exception): pass

OMIT = object()

def safe(f, *args):
    try:
        return f(*args)
    except CLIError, e:
        return e
    except WorkError, e:
        return e

def async_map(fun, sequence):
    threads = []
    for item in sequence:
        threads.append(eventlet.spawn(safe, fun, item))
    errors = []
    for thread in threads:
        result = thread.wait()
        if isinstance(result, WorkError):
            errors.append(result)
        elif result is not OMIT:
            yield result
    if errors:
        raise CLIError("%s task(s) had errors" % len(errors))

def async_apply(fun, sequence):
    return async_map(lambda i: fun(*i), sequence)

def force(sequence):
    list(sequence)

def next_page(response):
    if "Link" in response.headers:
        links = requests.utils.parse_header_links(response.headers["Link"])
        for link in links:
            if link['rel'] == 'next':
                return link['url']
    return None

def inject_token(url, token):
    if not token: return url
    parts = url.split("://", 1)
    if len(parts) == 2:
        return Elidable("%s://" % parts[0], Secret(token), "@%s" % parts[1])
    else:
        return Elidable(Secret(token), "@%s" % parts[0])

SETUP_TEMPLATE = Template("""# Forge configuration
organization: twitface
workdir: work
docker-repo: {{docker}}
user: {{user}}
password: >
  {{password}}
""")

def file_contents(path):
    try:
        with open(os.path.expanduser(os.path.expandvars(path)), "read") as fd:
            return fd.read()
    except IOError, e:
        print "  %s" % e
        return None

class Baker(Workstream):

    def __init__(self):
        Workstream.__init__(self)
        self.terminal = Terminal()
        self.moved = 0
        self.spincount = 0
        self.pushed_cache = {}

    def spin(self):
        eventlet.spawn(self.do_spin)

    def do_spin(self):
        while True:
            time.sleep(0.1)
            self.render()
            self.spincount = self.spincount + 1

    def clear(self):
        del self.items[:]
        self.moved = 0

    def prompt(self, msg, default=None, loader=None, echo=True):
        prompt = "%s: " % msg if default is None else "%s[%s]: " % (msg, default)
        prompter = raw_input if echo else getpass.getpass

        while True:
            self.clear()
            value = prompter(prompt) or default
            if value is None: continue
            if loader is not None:
                loaded = loader(value)
                if loaded is None:
                    continue
            if loader:
                return value, loaded
            else:
                return value

    def setup(self):
        print self.terminal.bold("== Checking Kubernetes Setup ==")
        print

        try:
            self.call("kubectl", "version", "--short", verbose=True)
            self.call("kubectl", "get", "service", "kubernetes", "--namespace", "default", verbose=True)
        except WorkError, e:
            print
            raise CLIError(self.terminal.red("== Kubernetes Check Failed ==") +
                           "\n\nPlease make sure kubectl is installed/configured correctly.")

        registry = "registry.hub.docker.com"
        repo = None
        user = os.environ["USER"]
        password = None
        json_key = None

        test_image = "registry.hub.docker.com/library/alpine:latest"

        def validate():
            self.call("docker", "login", "-u", user, "-p", Secret(password), registry)
            self.call("docker", "pull", test_image)
            img = image(registry, repo, "forge_test", "dummy")
            self.call("docker", "tag", test_image, img)
            self.do_push(img)
            assert self.pushed("forge_test", "dummy", registry=registry, repo=repo, user=user, password=password)

        print
        print self.terminal.bold("== Setting up Docker ==")

        while True:
            print
            registry = self.prompt("Docker registry", registry)
            repo = self.prompt("Docker repo", repo)
            user = self.prompt("Docker user", user)
            if user == "_json_key":
                json_key, password = self.prompt("Path to json key", json_key, loader=file_contents)
            else:
                password = self.prompt("Docker password", echo=False)

            try:
                print
                validate()
                break
            except WorkError, e:
                print
                print self.terminal.red("-- please try again --")
                continue
            finally:
                self.clear()

        print

        config = SETUP_TEMPLATE.render(
            docker="%s/%s" % (registry, repo),
            user=user,
            password=base64.encodestring(password).replace("\n", "\n  ")
        )

        config_file = "forge.yaml"

        print self.terminal.bold("== Writing config to %s ==" % config_file)

        with open(config_file, "write") as fd:
            fd.write(config)

        print
        print config.strip()
        print

        print self.terminal.bold("== Done ==")

    def spinner(self):
        return "-\\|/"[self.spincount % 4]

    def render_tail(self, limit):
        unfinished = self.spinner()
        count = 0
        for item in reversed(self.items):
            if item.finished:
                if not item.visible: continue
                status = self.terminal.bold(item.finish_summary)
            else:
                status = unfinished
            summary = "%s[%s]: %s" % (item.__class__.__name__, status, item.start_summary)
            lines = [summary]
            if (item.verbose or (item.finished and not item.ok)) and item.output:
                for l in item.output.splitlines():
                    lines.append("  %s" % l)
            for l in reversed(lines):
                yield l
                count = count + 1
                if count >= limit:
                    return

    def render(self):
        screenful = list(self.render_tail(self.terminal.height))

        sys.stdout.write(self.terminal.move_up*self.moved)

        for idx, line in enumerate(reversed(screenful)):
            # XXX: should really wrap this properly somehow, but
            #      writing out more than the terminal width will mess up
            #      the movement logic
            delta = len(line) - self.terminal.length(line)
            sys.stdout.write(line[:self.terminal.width+delta])
            sys.stdout.write(self.terminal.clear_eol + self.terminal.move_down)
        sys.stdout.write(self.terminal.clear_eol)

        self.moved = len(screenful)

    def started(self, item):
        self.render()

    def updated(self, item, output):
        self.render()

    def finished(self, item):
        self.render()

    def dr(self, url, expected=(), user=None, password=None):
        user = user or self.user
        password = password or self.password

        response = self.get(url, auth=(user, password), expected=expected + (401,), visible=False)
        if response.status_code == 401:
            challenge = response.headers['Www-Authenticate']
            if challenge.startswith("Bearer "):
                challenge = challenge[7:]
            opts = urllib2.parse_keqv_list(urllib2.parse_http_list(challenge))
            token = self.get("{realm}?service={service}&scope={scope}".format(**opts),
                             auth=(user, password),
                             visible=False).json()['token']
            response = self.get(url, headers={'Authorization': 'Bearer %s' % token}, expected=expected)
        return response

    def gh(self, api, expected=None):
        headers = {'Authorization': 'token %s' % self.token} if self.token else None
        response = self.get("https://api.github.com/%s" % api, headers=headers, expected=expected)
        result = response.json()
        if response.ok:
            next_url = next_page(response)
            while next_url:
                response = self.get(next_url, headers=headers)
                result.extend(response.json())
                next_url = next_page(response)
        return result

    def git_pull(self, name, url):
        repodir = os.path.join(self.workdir, name)
        if not os.path.exists(repodir):
            os.makedirs(repodir)
            self.call("git", "init", cwd=repodir)
        self.call("git", "pull", inject_token(url, self.token), cwd=repodir)

    EXCLUDED = set([".git"])

    def is_git(self, path):
        if os.path.exists(os.path.join(path, ".git")):
            return True
        elif path not in ('', '/'):
            return self.is_git(os.path.dirname(path))
        else:
            return False

    def version(self, root):
        if self.is_git(root):
            result = self.call("git", "diff", "--quiet", ".", cwd=root, expected=(1,), visible=False)
            if result.code == 0:
                return "%s.git" % self.call("git", "rev-parse", "HEAD", cwd=root).output.strip()
        return "%s.ephemeral" % util.shadir(root)

    def scan(self):
        prototypes = OrderedDict()
        services = OrderedDict()

        def descend(path, parent):
            if not os.path.exists(path): return
            names = os.listdir(path)

            if "proto.yaml" in names:
                proto = Prototype(os.path.join(path, "proto.yaml"))
                if proto.name not in prototypes:
                    prototypes[proto.name] = proto
                return
            if "service.yaml" in names:
                version = self.version(path)
                svc = Service(version, os.path.join(path, "service.yaml"), [])
                if svc.name not in services:
                    services[svc.name] = svc
                parent = svc
            if "Dockerfile" in names and parent:
                parent.containers.append(os.path.relpath(os.path.join(path, "Dockerfile"), parent.root))

            for n in names:
                if n not in self.EXCLUDED and os.path.isdir(os.path.join(path, n)):
                    descend(os.path.join(path, n), parent)

        for root in (os.getcwd(), self.workdir):
            descend(root, None)

        return list(prototypes.values()), list(services.values())
    
    def baked(self, name, version):
        result = self.call("docker", "images", "-q", image(self.registry, self.repo, name, version))
        return result.output

    def pushed(self, name, version, registry=None, repo=None, user=None, password=None):
        registry = registry or self.registry
        repo = repo or self.repo
        user = user or self.user
        password = password or self.password

        img = image(registry, repo, name, version)
        if img in self.pushed_cache:
            return self.pushed_cache[img]

        response = self.dr("https://%s/v2/%s/%s/manifests/%s" % (registry, repo, name, version), expected=(404,),
                           user=user, password=password)
        result = response.json()
        if 'signatures' in result and 'fsLayers' in result:
            self.pushed_cache[img] = True
            return True
        elif 'errors' in result and result['errors']:
            if result['errors'][0]['code'] == 'MANIFEST_UNKNOWN':
                self.pushed_cache[img] = False
                return False
        raise CLIError(response.content)

    def pull(self):
        repos = self.gh("orgs/%s/repos" % self.org)
        filtered = [r for r in repos if fnmatch.fnmatch(r["full_name"], self.filter)]

        urls = []
        for repo in async_map(lambda r: self.gh("repos/%s" % r["full_name"], expected=(404,)),
                              filtered):
            if "id" in repo:
                urls.append((repo["full_name"], repo["clone_url"]))

        force(async_apply(self.git_pull, urls))

    def is_raw(self, name, version):
        return not (self.pushed(name, version) or self.baked(name, version))

    def bake(self, scanned = None):
        prototypes, services = scanned or self.scan()

        raw = async_apply(lambda svc, name, container:
                              (svc, name, container) if self.is_raw(name, svc.version) else OMIT,
                          containers(services))

        force(async_apply(lambda svc, name, container:
                              self.call("docker", "build", ".", "-t",
                                        image(self.registry, self.repo, name, svc.version),
                                        cwd=os.path.join(svc.root, os.path.dirname(container))),
                          raw))

    def do_push(self, img):
        self.pushed_cache.pop(img, None)
        return self.call("docker", "push", img)

    def push(self, scanned = None):
        prototypes, services = scanned or self.scan()

        local = list(async_apply(lambda svc, name, container:
                                     (svc, name, container) if (self.baked(name, svc.version) and
                                                                not self.pushed(name, svc.version)) else OMIT,
                                 containers(services)))

        if local: self.call("docker", "login", "-u", self.user, "-p", Secret(self.password), self.registry)
        force(async_apply(lambda svc, name, container:
                              self.do_push(image(self.registry, self.repo, name, svc.version)),
                          local))

    def render_yaml(self, svc):
        k8s_dir = os.path.join(self.workdir, "k8s", svc.name)
        svc.deployment(self.registry, self.repo, k8s_dir)
        return k8s_dir

    def resources(self, k8s_dir):
        return self.call("kubectl", "apply", "--dry-run", "-f", k8s_dir, "-o", "name").output.split()

    def apply_yaml(self, k8s_dir):
        cmd = "kubectl", "apply", "-f", k8s_dir
        if self.dry_run:
            cmd += "--dry-run",
        return self.call(*cmd, verbose=True)

    def yaml(self, scanned = None):
        prototypes, services = scanned or self.scan()

        owners = OrderedDict()
        conflicts = []
        k8s_dirs = []

        for svc, k8s_dir, resources in async_apply(lambda svc, k8s_dir: (svc, k8s_dir, self.resources(k8s_dir)),
                                                   async_map(lambda svc: (svc, self.render_yaml(svc)), services)):
            for resource in resources:
                if resource in owners:
                    conflicts.append((resource, owners[resource].name, svc.name))
                else:
                    owners[resource] = svc
            k8s_dirs.append(k8s_dir)

        if conflicts:
            messages = ", ".join("%s defined by %s and %s" % c for c in conflicts)
            raise CLIError("conflicts: %s" % messages)
        return k8s_dirs

    def build(self, scanned = None):
        scanned = scanned or self.scan()
        self.bake(scanned)
        self.push(scanned)
        return self.yaml(scanned)

    def deploy(self):
        k8s_dirs = self.build(self.scan())
        force(async_map(self.apply_yaml, k8s_dirs))

def get_config(args):
    if args["--config"] is not None:
        return args["--config"]

    if "FORGE_CONFIG" in os.environ:
        return os.environ["FORGE_CONFIG"]

    prev = None
    path = os.getcwd()
    while path != prev:
        prev = path
        candidate = os.path.join(path, "forge.yaml")
        if os.path.exists(candidate):
            return candidate
        path = os.path.dirname(path)
    return None

def get_workdir(conf, base):
    workdir = conf.get("workdir") or base
    if not workdir.startswith("/"):
        workdir = os.path.join(base, workdir)
    return workdir

def get_repo(conf):
    url = conf.get("docker-repo")
    if url is None:
        raise CLIError("docker-repo must be configured")
    if "/" not in url:
        raise CLIError("docker-repo must be in the form <registry-url>/<name>")
    registry, repo = url.split("/", 1)
    return registry, repo

def get_password(conf):
    pw = conf.get("password")
    if not pw:
        raise CLIError("docker password must be configured")
    return base64.decodestring(pw)

def create(baker, args):
    proto = args["<prototype>"]
    arguments_file = args["<arguments>"]
    target = args["<target>"] or os.path.splitext(os.path.basename(arguments_file))[0]
    prototypes, services = baker.scan()
    selected = [p for p in prototypes if p.name == proto]
    assert len(selected) <= 1
    if not selected:
        raise CLIError("no such prototype: %s" % proto)
    prototype = selected[0]

    try:
        with open(arguments_file, "read") as fd:
            # XXX: the Loader=blah messes up the OrderedDict stuff
            arguments = yaml.load(fd, Loader=yaml.loader.BaseLoader)
    except IOError, e:
        raise CLIError(e)

    errors = prototype.validate(arguments)
    if errors:
        raise CLIError("\n".join(errors))

    prototype.instantiate(target, arguments)

def main(args):
    baker = Baker()

    if args["setup"]: return baker.setup()

    conf_file = get_config(args)
    if not conf_file:
        raise CLIError("unable to find forge.yaml, try running `forge setup`")

    with open(conf_file, "read") as fd:
        conf = yaml.load(fd)

    baker.workdir = get_workdir(conf, os.path.dirname(os.path.abspath(conf_file)))
    baker.registry, baker.repo = get_repo(conf)

    try:
        baker.org = conf["organization"]
        baker.user = conf["user"]
    except KeyError, e:
        raise CLIError("missing config property: %s" % e)

    baker.token = conf.get("token")
    baker.password = get_password(conf)

    baker.filter = args["--filter"]
    baker.dry_run = args["--dry-run"]

    if not args["serve"]:
        baker.spin()

    if args["pull"]: baker.pull()
    if args["bake"]: baker.bake()
    if args["push"]: baker.push()
    if args["yaml"]: baker.yaml()
    if args["build"]: baker.build()
    if args["deploy"]: baker.deploy()
    if args["create"]: create(baker, args)
    if args["serve"]:
        from .server import serve
        serve(baker)

def call_main():
    util.setup_yaml()
    args = docopt(__doc__, version="Forge %s" % __version__)
    try:
        exit(main(args))
    except CLIError, e:
        exit(e)
    except KeyboardInterrupt, e:
        exit(e)

if __name__ == "__main__":
    call_main()
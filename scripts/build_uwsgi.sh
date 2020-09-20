#!/bin/sh -e
# Build a custom uWSGI with all plugins required.

PLUGINS="cgi corerouter python router_cache router_rewrite router_uwsgi ugreen"

# Contains patch for CGI chunked request handling, needed for git-http-backend
[ -d uwsgi ] || git clone --depth 1 --single-branch --no-tags https://github.com/unbit/uwsgi
cd uwsgi
python3 uwsgiconfig.py --build core
for plugin in $PLUGINS; do
	python3 uwsgiconfig.py --plugin plugins/$plugin core
done

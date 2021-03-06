#!/bin/sh -e
# Build a custom uWSGI with all plugins required.
# The patch for CGI chunked request handling, needed for git-http-backend, wasn't
# released yet, hence uWSGI needs to be built from master.

PLUGINS="cgi corerouter python router_cache router_rewrite router_uwsgi ugreen"

if [ -d uwsgi ]; then
	echo "WARNING: uwsgi directory exists, building without cloning" >&2
else
	git clone --depth 1 --no-tags https://github.com/unbit/uwsgi
fi

cd uwsgi
python3 uwsgiconfig.py --build core
for plugin in $PLUGINS; do
	python3 uwsgiconfig.py --plugin plugins/$plugin core
done

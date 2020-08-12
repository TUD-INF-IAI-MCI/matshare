#!/bin/sh
# The docker main command.

set -e

echo '
*********************
* This is MatShare. *
*********************
'

if [ ! -z "$MS_DATABASE_HOST" ] && [ ! -z "$MS_DATABASE_PORT" ]; then
    echo "Waiting for database on ${MS_DATABASE_HOST}:${MS_DATABASE_PORT} ..."
    while ! nc -z "$MS_DATABASE_HOST" "$MS_DATABASE_PORT"; do sleep 1; done;
    echo
fi

export DJANGO_SETTINGS_MODULE=matshare.settings

./manage.py migrate
echo

./initialize_matshare.py
echo

# Make sure uWSGI's spooler directory exists and run the server
mkdir -p spooler
exec /opt/uwsgi/uwsgi --plugin-dir=/opt/uwsgi --emperor=uwsgi_configs --vassal-set=plugin-dir=/opt/uwsgi --vassal-set=chdir=..

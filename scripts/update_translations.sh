#!/bin/sh
# Scan the codebase for new translation strings and update the *.po files in
# matshare/locale.

# Space-separated list of locales to update translation strings for
LOCALES="de"
# Additional flags can be passed via this environment variable from outside
MAKEMESSAGES_OPTS="--no-wrap --no-obsolete --ignore ci_templates $MAKEMESSAGES_OPTS"

set -e

mkdir -p matshare/locale
for locale in $LOCALES; do
	for domain in django djangojs; do
		./manage.py makemessages --domain="$domain" --locale="$locale" $MAKEMESSAGES_OPTS
	done
done

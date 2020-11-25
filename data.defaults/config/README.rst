MatShare Configuration
======================

MatShare is configured using environment variables. To make the whole process as
convenient as possible, configuration files are just bash scripts that are sourced
before starting MatShare (or one of the management commands).

All files in this directory ending in ``.conf`` are loaded in alphabetical order.
The available settings can be found in ``99-defaults.conf``. This file **must**
be kept as-is and loaded last, as it sets the appropriate defaults for settings you
omitted in your own file(s).

So in order to configure MatShare, create something like ``50-local.conf`` with only
the settings whose defaults aren't appropriate for you and the defaults file will
take care of the rest.

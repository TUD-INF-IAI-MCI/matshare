#!/bin/sh
# Collect static assets from ./node_modules and put them into ./static.

set -e

# Collect own assets
yarn run sass
mkdir -p static/matshare
mv css static/matshare

# Collect third-party libraries
mkdir -p static/fontawesome
mv node_modules/@fortawesome/fontawesome-free/css static/fontawesome
mv node_modules/@fortawesome/fontawesome-free/webfonts static/fontawesome
mv node_modules/bootstrap/dist static/bootstrap
mv node_modules/jquery/dist static/jquery

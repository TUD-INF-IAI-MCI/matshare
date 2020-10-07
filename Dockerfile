# Collect assets using yarn
FROM node:15-alpine3.12 AS yarn_static

WORKDIR /tmp
COPY package.json .
RUN yarn
COPY scripts/collect_assets.sh .
COPY scss scss
RUN ./collect_assets.sh


# This becomes the uwsgi image
FROM debian:buster

ENV \
	POETRY_VERSION=1.0.10 \
    # This gives the virtualenvs a deterministic path
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    # Make the poetry executable available
    PATH="/root/.poetry/bin:$PATH"

# Install debian packages and build uWSGI first to cache them in docker layer
WORKDIR /tmp
COPY scripts/build_uwsgi.sh ./
RUN \
    apt update && \
    apt install --no-install-recommends -y \
        curl gettext git netcat-openbsd pandoc \
        python3 python3-pip python3-venv \
        # gladtex dependencies
        dvipng preview-latex-style texlive-fonts-recommended texlive-latex-recommended \
        # django-auth-ldap build dependencies
        python3-dev libldap2-dev libsasl2-dev \
        # uWSGI build dependencies
        gcc libc-dev libpcre3-dev make python3-dev && \
    # Poetry needs to be installed that way
    curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python3 && \
    ./build_uwsgi.sh && \
    mv uwsgi /opt && \
    apt purge -y libc-dev libpcre3-dev make && \
    apt autoremove --purge -y && \
    rm -rf * ~/.cache/* /var/cache/apt/* /var/lib/apt/lists/*

WORKDIR /opt/matshare

# Now install MatShare's dependencies only to cache them in docker layer
COPY poetry.lock pyproject.toml ./
RUN \
    # The latest pygit2 wheels require a recent pip
    poetry run pip install -U pip setuptools wheel && \
    poetry install --no-dev --no-root && \
    rm -rf ~/.cache/* /tmp/*

# Copy config files and scripts
COPY manage.py ./
COPY scripts/initialize_matshare.py scripts/run_uwsgi.sh ./
COPY uwsgi_configs uwsgi_configs

# Finally install MatShare itself, we only need to rebuild from here when code changes
COPY matshare matshare
RUN \
    # This creates a system-wide install of matshare, linked to /opt/matshare/matshare
    poetry install --no-dev && \
    rm -rf ~/.cache/* /tmp/*

# Collect static files using django and mix with those fetched via yarn
RUN \
    export MS_DEBUG=1 && \
    poetry run ./manage.py collectstatic && \
    # Compile gettext translation catalogs
    mkdir -p matshare/locale && \
    poetry run ./manage.py compilemessages --ignore .venv
COPY --from=yarn_static /tmp/static static

CMD ["poetry", "run", "./run_uwsgi.sh"]

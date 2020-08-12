MatShare
========

This is the material distribution system developed by the Working Group Services
Disability and Studies at Dresden University of Technology to provide visually
impaired students with the material they need to attend courses.


Documentation
-------------

For the time being, there is no documentation apart from the various annotations in
the code base itself and explanatory on-site help texts. I know that this situation
is far from being perfect, but that's how things are right now. Proper documentation
is to be written as a future work.


Deployment
----------

MatShare is designed to be deployed using Docker. This allows for reproducible
deployments and reliable upgrades with all self-contained dependencies.

The application stack consists of three containers communicating with each other:

*  uWSGI as application server, which serves both the MatShare Python application
   and git repositories via git-http-backend
*  postgres as DBMS
*  nginx as reverse proxy

The nginx server is configured in the file ``nginx.conf``. The provided settings are
necessary for all of MatShare's features to work correctly. Pay particular attention
to the included comments should you decide to extend the basic configuration, for
instance to have nginx do TLS termination.

Configuration of MatShare itself is done by setting several environment
variables, all of which are listed and documented in the bundled
``docker-compose.yaml``. The recommended way of customizing the settings is to create a
``docker-compose.override.yaml`` file and override the required settings therein. That
way you don't mess with the default values and make future upgrades run smoothly. A
sample for development purposes can be found in ``docker-compose.override-dev.yaml``.

Another point for you to customize is corporate identity in the HTML templates. In
the directory ``matshare/ci_templates/matshare_ci``, you find a README file explaining
how to do it.

When you're all set, bring the whole stack up by running::

    docker-compose up -d --build

At first start, a superuser account will be created with the information you
configured via the ``MS_ADMIN_*`` variables. Check the container's output to verify
it all went well::

    docker-compose logs uwsgi


Upgrading
---------

In general, all pending database migrations are ran when starting the Docker containers
with updated images. To be sure, check ``CHANGELOG.md`` before you upgrade. Eventual
caveats or additional steps to be taken for existing installations will be listed
there.

The general upgrade path is then:

1. Backup the database contents and all directories in case anything goes wrong.

2. Update the code base.

3. Rebuild images and recreate the containers::

       docker-compose pull db
       docker-compose build --pull
       docker-compose up -d

4. Verify everything looks good::

       docker-compose logs uwsgi


Development
-----------

MatShare uses `Poetry <https://python-poetry.org/>`_ for dependency
management. Therefore you need to install it and then set up the development
environment by running this from the repository's root::

    poetry install --no-root

Poetry will install all dependencies to a virtual environment to keep your system
clean.


Pre-Commit Tasks
~~~~~~~~~~~~~~~~

Some steps need to be taken before committing changes. These commands have to be
ran from the repository's root.

1. Generate proper database migrations::

       poetry run ./manage.py makemigrations

2. The code base is formatted using black, so make sure to run it::

       poetry run black .

3. The translation files need to be updated after making code changes::

       poetry run ./scripts/update_translations.sh

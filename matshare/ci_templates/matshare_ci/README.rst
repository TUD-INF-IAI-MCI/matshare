Corporate Identity Templates
============================

In this directory, you can place custom template snippets that are then included at
various places around the MatShare web interface and e-mails.

All overridable templates can be found in ``../../templates/matshare_ci``. If you
want to customize one of them, copy it over to this directory and modify it according
to your needs.

The templates use the `Django Template Language
<https://docs.djangoproject.com/en/stable/ref/templates/language/>`_, which is,
thanks to its thorough documentation, easy to get started with and provides a lot
of functionality.

``*.html`` files in this directory are excluded by ``.gitignore`` and hence won't
be committed along with eventual development efforts. This also has the benefit that
your customized templates won't be overridden when upgrading MatShare.

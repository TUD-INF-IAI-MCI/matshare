"""
Root package of the MatShare project.
"""

import pkg_resources


__version__ = pkg_resources.get_distribution(__name__).version

# AppConfig to use when including "matshare" in INSTALLED_APPS
default_app_config = "matshare.apps.MatShareConfig"

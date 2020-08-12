from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _
import watson.search


class MatShareConfig(AppConfig):
    """
    Configuration for the matshare app.
    """

    name = "matshare"
    verbose_name = _("Material Management")

    def ready(self):
        """Register models with watson and hook up Django signals."""
        Course = self.get_model("Course")
        watson.search.register(Course, Course.WatsonSearchAdapter)

        from . import django_signals

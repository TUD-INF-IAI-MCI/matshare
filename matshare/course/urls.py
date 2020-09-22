from django.urls import path

from . import views


# Path of the URL that identifies a course human-friendly
SLUG_PATH = (
    "<slug:study_course_slug>/<slug:term_slug>/<slug:type_slug>/<slug:course_slug>"
)

urlpatterns = [
    # Course directory
    path("", views.DirectoryView.as_view(), name="course_directory"),
    # course detail pages
    path(f"{SLUG_PATH}/", views.OverviewView.as_view(), name="course_overview"),
    path(f"{SLUG_PATH}/sources/", views.SourcesView.as_view(), name="course_sources"),
    path(
        f"{SLUG_PATH}/sources/<path:path>",
        views.SourcesView.as_view(),
        name="course_sources",
    ),
    path(f"{SLUG_PATH}/git/", views.GitView.as_view(), name="course_git"),
    # Material downloading
    path(
        f"{SLUG_PATH}/material/download/",
        views.MaterialDownloadView.as_view(),
        name="course_material_download",
    ),
    # Online HTML material viewing
    path(
        f"{SLUG_PATH}/material/html/",
        views.MaterialHTMLView.as_view(),
        name="course_material_html",
    ),
    path(
        f"{SLUG_PATH}/material/html/<path:path>",
        views.MaterialHTMLView.as_view(),
        name="course_material_html",
    ),
    # Self-subscription and subscription settings
    path(
        f"{SLUG_PATH}/subscription/",
        views.SubscriptionView.as_view(),
        name="course_subscription",
    ),
]

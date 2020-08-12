from django.urls import path

from . import views


urlpatterns = [
    path("dashboard/", views.DashboardView.as_view(), name="user_dashboard"),
    path(
        "feed/<int:user_pk>/<str:feed_token>/editor-feed.xml",
        views.EditorFeedView.as_view(),
        name="user_editor_feed",
    ),
    path(
        "feed/<int:user_pk>/<str:feed_token>/student-feed.xml",
        views.StudentFeedView.as_view(),
        name="user_student_feed",
    ),
    path("settings/", views.SettingsView.as_view(), name="user_settings"),
]

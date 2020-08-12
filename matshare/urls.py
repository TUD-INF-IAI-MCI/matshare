from django.urls import include, path

from . import views
from .admin import admin_site
from .git import views as git_views


# Custom error views
handler404 = views.View404.as_view()

# Path of the URL that identifies a course human-friendly
SLUG_PATH = (
    "<slug:study_course_slug>/<slug:term_slug>/<slug:type_slug>/<slug:course_slug>"
)

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("courses/", include("matshare.course.urls")),
    path("user/", include("matshare.user.urls")),
    path(f"git/{SLUG_PATH}/", git_views.GitAuthView.as_view(), name="git_auth"),
    path(
        f"git/{SLUG_PATH}/<path:suffix>",
        git_views.GitAuthView.as_view(),
        name="git_auth",
    ),
    path(
        f"git-push-notify/<int:course_pk>/",
        git_views.GitPushNotifyView.as_view(),
        name="git_push_notify",
    ),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path(
        "easy-access/<str:token>/",
        views.EasyAccessActivationView.as_view(),
        name="easy_access_activation",
    ),
    path(
        "password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="password_reset_request",
    ),
    path(
        "password-reset/sent/",
        views.PasswordResetRequestSentView.as_view(),
        name="password_reset_request_sent",
    ),
    path(
        "password-reset/confirm/<int:pk>/<str:token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("set-language/", views.set_language, name="set_language"),
    path("cookie-consent/", views.CookieConsentView.as_view(), name="cookie_consent"),
    path("legal/", views.LegalNoticeView.as_view(), name="legal_notice"),
    path("i18n.js", views.JavaScriptCatalog.as_view(), name="i18n_js"),
    # Custom admin site
    path("admin/", admin_site.urls),
]

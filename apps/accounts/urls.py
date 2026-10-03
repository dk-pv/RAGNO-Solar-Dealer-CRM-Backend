from django.urls import path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import DepartmentViewSet, LoginView, MeView, RoleViewSet, UserViewSet

router = SimpleRouter()
router.register('users', UserViewSet)
router.register('roles', RoleViewSet)
router.register('departments', DepartmentViewSet)

urlpatterns = [
    path('auth/login/', LoginView.as_view(), name='auth-login'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='auth-refresh'),
    path('auth/me/', MeView.as_view(), name='auth-me'),
    *router.urls,
]

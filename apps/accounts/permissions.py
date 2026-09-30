from rest_framework.permissions import BasePermission, DjangoModelPermissions

from .models import Role


class IsAdminRole(BasePermission):
    """Only CRM ADMIN users. This is the project-wide default, so a new endpoint stays closed until opened explicitly."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == Role.ADMIN)


class ModelPermissions(DjangoModelPermissions):
    """Django model permissions per HTTP method. Unlike DRF's default, reads require the `view` permission."""

    perms_map = {
        **DjangoModelPermissions.perms_map,
        'GET': ['%(app_label)s.view_%(model_name)s'],
        'HEAD': ['%(app_label)s.view_%(model_name)s'],
    }

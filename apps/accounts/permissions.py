from rest_framework.permissions import BasePermission, DjangoModelPermissions

from .models import Role

# Module access. Each module a role can be given is a named bundle of the Django permissions its API checks, so giving
# the STAFF role a module in Settings → Roles & Access grants exactly what the backend enforces. ADMIN has every module.
# Every permission listed must be a real, migrated permission.
MODULES = {
    # Dashboard, Work, Activities and Reports have no backend yet: their APIs must check these access permissions
    # (for example user.has_perm('accounts.access_work')) when they are built.
    'dashboard': {'label': 'Dashboard', 'permissions': ['accounts.access_dashboard']},
    'leads': {'label': 'Leads', 'permissions': ['leads.view_lead', 'leads.add_lead', 'leads.change_lead']},
    'work': {'label': 'Work', 'permissions': ['accounts.access_work']},
    'activities': {'label': 'Activities', 'permissions': ['accounts.access_activities']},
    'reports': {'label': 'Reports', 'permissions': ['accounts.access_reports']},
    # Read-only: user and department writes stay ADMIN-only whatever permissions a STAFF user holds.
    'settings': {'label': 'Settings', 'permissions': ['accounts.view_user', 'accounts.view_department']},
}


class IsAdminRole(BasePermission):
    """Only CRM ADMIN users. This is the project-wide default, so a new endpoint stays closed until opened explicitly."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role_id == Role.ADMIN)


class ModelPermissions(DjangoModelPermissions):
    """Django model permissions per HTTP method. Unlike DRF's default, reads require the `view` permission."""

    perms_map = {
        **DjangoModelPermissions.perms_map,
        'GET': ['%(app_label)s.view_%(model_name)s'],
        'HEAD': ['%(app_label)s.view_%(model_name)s'],
    }

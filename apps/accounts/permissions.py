from rest_framework.permissions import BasePermission, DjangoModelPermissions

from .models import Role

# Module access. Each module is a named bundle of the Django permissions its API checks. ADMIN has every module; the
# STAFF role can only be given Activities, in Settings → Roles & Access (RoleSerializer refuses the others).
# Every permission listed must be a real, migrated permission.
MODULES = {
    # Modules without models of their own use one access permission: the Work, Activities, Dashboard and Reports APIs
    # check accounts.access_work, access_activities, access_dashboard and access_reports.
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

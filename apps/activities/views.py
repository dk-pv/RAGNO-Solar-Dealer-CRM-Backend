from rest_framework import mixins, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.leads.models import Lead

from .models import Activity
from .serializers import ActivitySerializer


class FollowsLeadAccess(BasePermission):
    """Activities follow their lead: reading needs the Leads view permission, adding, editing and deleting need the
    change permission. Which leads count is the same as everywhere else: all for an admin, their own for staff."""

    def has_permission(self, request, view):
        return request.user.has_perm('leads.view_lead' if request.method in SAFE_METHODS else 'leads.change_lead')


class ActivityViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ActivitySerializer
    permission_classes = [FollowsLeadAccess]

    def get_queryset(self):
        leads = Lead.objects.visible_to(self.request.user)
        activities = Activity.objects.filter(lead__in=leads).select_related('created_by')
        if self.action == 'list':
            # Listed per lead, as the lead page shows them.
            lead = self.request.query_params.get('lead', '')
            if not lead.isdigit():
                raise ValidationError({'lead': 'Choose a lead.'})
            activities = activities.filter(lead_id=int(lead))
        return activities.order_by('-created_at', '-id')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

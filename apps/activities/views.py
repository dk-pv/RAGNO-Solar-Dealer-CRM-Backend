from django.db.models import Case, F, IntegerField, Q, Value, When
from rest_framework import mixins, viewsets
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.leads.models import Lead
from apps.leads.views import LeadPagination

from .models import Activity, ActivityStatus
from .serializers import ActivityQuerySerializer, ActivitySerializer

# Sorting by status puts Pending first.
STATUS_RANK = Case(When(status=ActivityStatus.PENDING, then=Value(0)), default=Value(1), output_field=IntegerField())


def filter_activities(activities, query_params, user):
    query = ActivityQuerySerializer(data=query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data

    if 'lead' in params:
        activities = activities.filter(lead_id=params['lead'])
    # The heading or notes, or the lead it's for: its name, phone, email, place or ID, as the leads list searches them.
    # A lead the user doesn't work (a follow-up merely assigned to them) matches only by what they're shown of it: its
    # name, phone and ID.
    if search := params.get('search', '').strip():
        activities = activities.filter(
            Q(title__icontains=search) | Q(description__icontains=search)
            | Q(lead__in=Lead.objects.visible_to(user).search(search).values('pk'))
            | Q(lead__in=Lead.objects.search(search, details=False).values('pk'))
        )
    for field in ('status', 'type'):
        if field in params:
            activities = activities.filter(**{field: params[field]})
    # The follow-up's own staff (not the lead's).
    if 'assigned_to' in params:
        activities = activities.filter(assigned_to_id=params['assigned_to'])
    if 'due_after' in params:
        activities = activities.filter(due_date__gte=params['due_after'])
    if 'due_before' in params:
        activities = activities.filter(due_date__lte=params['due_before'])

    ordering = params.get('ordering', '-created_at')
    field = F('status_rank' if ordering.lstrip('-') == 'status' else ordering.lstrip('-'))
    order = field.desc(nulls_last=True) if ordering.startswith('-') else field.asc(nulls_last=True)
    # The newest first among equals; the id keeps pages stable.
    return activities.annotate(status_rank=STATUS_RANK).order_by(order, '-created_at', '-id')


class FollowsLeadAccess(BasePermission):
    """Follow-ups go with leads: reading needs the Leads view permission, adding, editing, completing and deleting need
    the change permission. Which follow-ups count: all for an admin; for staff, those on their own leads and those
    assigned to them (Activity.objects.visible_to).
    The Activities page (the follow-ups of every lead the user can see, rather than one lead's) is also the Activities
    module."""

    def has_permission(self, request, view):
        user = request.user
        if request.method not in SAFE_METHODS:
            return user.has_perm('leads.change_lead')
        # Decided on the value the list will filter by: a blank ?lead= filters nothing, so it's the Activities page.
        if request.query_params.get('lead', '').strip():
            return user.has_perm('leads.view_lead')
        return user.has_perm('leads.view_lead') and user.has_perm('accounts.access_activities')


class ActivityViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ActivitySerializer
    permission_classes = [FollowsLeadAccess]
    # Paged like the leads list: 25 a page, ?page_size= up to 100.
    pagination_class = LeadPagination

    def get_queryset(self):
        user = self.request.user
        activities = Activity.objects.visible_to(user).select_related('lead', 'assigned_to', 'created_by')
        if self.action == 'list':
            return filter_activities(activities, self.request.query_params, self.request.user)
        if self.action == 'destroy':
            # Deleting stays with whoever works the lead; staff who only do the follow-up complete it instead.
            return activities.filter(lead__in=Lead.objects.visible_to(user))
        return activities

    def perform_create(self, serializer):
        # Every new follow-up starts Pending, whatever the request says; it is completed by changing its status after.
        serializer.save(created_by=self.request.user, status=ActivityStatus.PENDING)

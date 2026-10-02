from django.db.models import Case, F, IntegerField, Q, Value, When
from rest_framework import mixins, viewsets
import re

from django.db.models import Case, F, IntegerField, Q, Value, When
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
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
from .serializers import ActivitySerializer, WorkActivityQuerySerializer

PENDING_FIRST = Case(When(status=ActivityStatus.PENDING, then=Value(0)), default=Value(1), output_field=IntegerField())
# One Work's activities: pending first, the soonest due first; then completed, the latest first.
WITHIN_WORK = [PENDING_FIRST, F('completed_at').desc(nulls_last=True), F('due_date').asc(nulls_last=True), '-created_at', '-id']
# The Work Activities page. Every order but due date keeps each Work's activities together, in the order above.
WORK_ACTIVITY_ORDERS = {
    '-work': ['-work_id', *WITHIN_WORK],
    'work': ['work_id', *WITHIN_WORK],
    'customer_name': ['work__customer_name', 'work_id', *WITHIN_WORK],
    'due_date': [PENDING_FIRST, F('due_date').asc(nulls_last=True), '-work_id', '-id'],
}


def required_permission(on_work, method):
    """Activities follow what they belong to. A lead's: reading needs the Leads view permission, adding, editing and
    deleting need the change permission. A Work's: the Work module, as for the Work itself."""
    if on_work:
        return 'accounts.access_work'
    return 'leads.view_lead' if method in SAFE_METHODS else 'leads.change_lead'

class FollowsLeadAccess(BasePermission):
    """Follow-ups go with leads: reading needs the Leads view permission, adding, editing, completing and deleting need
    the change permission. Which follow-ups count: all for an admin; for staff, those on their own leads and those
    assigned to them (Activity.objects.visible_to).
    The Activities page (the follow-ups of every lead the user can see, rather than one lead's) is also the Activities
    module."""

class FollowsParentAccess(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if request.method not in SAFE_METHODS:
            return user.has_perm('leads.change_lead')
        # Decided on the value the list will filter by: a blank ?lead= filters nothing, so it's the Activities page.
        if request.query_params.get('lead', '').strip():
            return user.has_perm('leads.view_lead')
        return user.has_perm('leads.view_lead') and user.has_perm('accounts.access_activities')
        if view.action == 'works':
            # Every Work's activities on one page: the Work module and the Activities module.
            return request.user.has_perm('accounts.access_work') and request.user.has_perm('accounts.access_activities')
        if view.action == 'list':
            on_work = 'work' in request.query_params
        elif view.action == 'create':
            on_work = isinstance(request.data, dict) and 'work' in request.data
        else:
            # A single activity: checked against its lead or Work below, once it is found among the visible ones.
            return request.user.has_perm('leads.view_lead') or request.user.has_perm('accounts.access_work')
        return request.user.has_perm(required_permission(on_work, request.method))

    def has_object_permission(self, request, view, activity):
        return request.user.has_perm(required_permission(activity.work_id is not None, request.method))


class ActivityPagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    # ponytail: a Work's page grows with "Show more"; page through `next` if one ever holds more than 500 activities.
    max_page_size = 500


class ActivityViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
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
    permission_classes = [FollowsParentAccess]
    pagination_class = ActivityPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        user = self.request.user
        # Which leads count is the same as everywhere else: all for an admin, their own for staff. Works: all of them.
        visible = Q(pk__in=[])
        if user.has_perm('leads.view_lead'):
            visible |= Q(lead__in=Lead.objects.visible_to(user))
        if user.has_perm('accounts.access_work'):
            visible |= Q(work__isnull=False)
        # The Work and its plan come in the same query, for work_summary: no query per row.
        activities = Activity.objects.filter(visible).select_related(
            'created_by', 'assigned_to', 'completed_by', 'work__plan',
        )
        if self.action != 'list':
            return activities

        # Listed per lead, as the lead page shows them, or per Work.
        work = self.request.query_params.get('work')
        if work is not None:
            if not work.isdigit():
                raise ValidationError({'work': 'Choose a Work.'})
            return activities.filter(work_id=int(work)).order_by(*WITHIN_WORK)
        lead = self.request.query_params.get('lead', '')
        if not lead.isdigit():
            raise ValidationError({'lead': 'Choose a lead.'})
        return activities.filter(lead_id=int(lead)).order_by('-created_at', '-id')

    @action(detail=False)
    def works(self, request):
        """Every Work's activities, completed ones included, with search, filters and paging. Each Work's activities stay
        together (newest Work first by default)."""
        query = WorkActivityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        activities = self.get_queryset().filter(work__isnull=False)
        if search := params.get('search', '').strip():
            match = (
                Q(work__customer_name__icontains=search) | Q(description__icontains=search)
                | Q(assigned_to__name__icontains=search)
            )
            # "12" and "#12" also find Work 12's activities.
            work_id = search.removeprefix('#')
            if re.fullmatch(r'[0-9]{1,18}', work_id):
                match |= Q(work_id=int(work_id))
            activities = activities.filter(match)
        for field in ('work', 'assigned_to', 'type', 'status'):
            if field in params:
                activities = activities.filter(**{field: params[field]})
        if 'due_after' in params:
            activities = activities.filter(due_date__gte=params['due_after'])
        if 'due_before' in params:
            activities = activities.filter(due_date__lte=params['due_before'])

        page = self.paginate_queryset(activities.order_by(*WORK_ACTIVITY_ORDERS[params.get('ordering', '-work')]))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_destroy(self, activity):
        if activity.work_id:
            raise PermissionDenied("A Work's activities are kept as its history. Edit the activity instead.")
        activity.delete()

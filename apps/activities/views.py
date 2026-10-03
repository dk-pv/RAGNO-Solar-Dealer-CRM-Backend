import re

from django.db.models import Case, F, IntegerField, Q, Value, When
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.leads.models import Lead

from .models import Activity, ActivityStatus
from .serializers import ActivityQuerySerializer, ActivitySerializer, WorkActivityQuerySerializer

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


def filter_follow_ups(follow_ups, query_params, user):
    """The lead follow-ups list (one lead's, or the Lead Activities page): search, filters and sort."""
    query = ActivityQuerySerializer(data=query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data

    if 'lead' in params:
        follow_ups = follow_ups.filter(lead_id=params['lead'])
    # The heading or notes, or the lead it's for: its name, phone, email, place or ID, as the leads list searches them.
    # A lead the user doesn't work (a follow-up merely assigned to them) matches only by what they're shown of it: its
    # name, phone and ID.
    if search := params.get('search', '').strip():
        follow_ups = follow_ups.filter(
            Q(title__icontains=search) | Q(description__icontains=search)
            | Q(lead__in=Lead.objects.visible_to(user).search(search).values('pk'))
            | Q(lead__in=Lead.objects.search(search, details=False).values('pk'))
        )
    for field in ('status', 'type'):
        if field in params:
            follow_ups = follow_ups.filter(**{field: params[field]})
    # The follow-up's own staff (not the lead's).
    if 'assigned_to' in params:
        follow_ups = follow_ups.filter(assigned_to_id=params['assigned_to'])
    if 'due_after' in params:
        follow_ups = follow_ups.filter(due_date__gte=params['due_after'])
    if 'due_before' in params:
        follow_ups = follow_ups.filter(due_date__lte=params['due_before'])

    ordering = params.get('ordering', '-created_at')
    field = F('pending_rank' if ordering.lstrip('-') == 'status' else ordering.lstrip('-'))
    order = field.desc(nulls_last=True) if ordering.startswith('-') else field.asc(nulls_last=True)
    # Sorting by status puts Pending first; the newest first among equals; the id keeps pages stable.
    return follow_ups.annotate(pending_rank=PENDING_FIRST).order_by(order, '-created_at', '-id')


def required_permission(on_work, method):
    """Activities follow what they belong to. A lead's follow-ups: reading needs the Leads view permission; adding,
    editing, completing and deleting need the change permission. A Work's activities: the Work module, as the Work."""
    if on_work:
        return 'accounts.access_work'
    return 'leads.view_lead' if method in SAFE_METHODS else 'leads.change_lead'


class FollowsParentAccess(BasePermission):
    """Which activities count is decided in get_queryset: a lead's follow-ups as Activity.objects.visible_to() says (all
    for an admin; for staff, those on their own leads and those assigned to them), and every Work's activities with the
    Work module. The pages that list many at once also need the Activities module."""

    def has_permission(self, request, view):
        user = request.user
        if view.action == 'works':
            # Every Work's activities on one page: the Work module and the Activities module.
            return user.has_perm('accounts.access_work') and user.has_perm('accounts.access_activities')
        if view.action == 'list':
            if 'work' in request.query_params:
                return user.has_perm('accounts.access_work')
            # One lead's follow-ups, or (no lead: a blank ?lead= filters nothing) the Lead Activities page.
            if request.query_params.get('lead', '').strip():
                return user.has_perm('leads.view_lead')
            return user.has_perm('leads.view_lead') and user.has_perm('accounts.access_activities')
        if view.action == 'create':
            on_work = isinstance(request.data, dict) and 'work' in request.data
            return user.has_perm(required_permission(on_work, request.method))
        # A single activity: checked against its lead or Work below, once it is found among the visible ones.
        return user.has_perm('leads.view_lead') or user.has_perm('accounts.access_work')

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
    permission_classes = [FollowsParentAccess]
    pagination_class = ActivityPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        user = self.request.user
        visible = Q(pk__in=[])
        if user.has_perm('leads.view_lead'):
            visible |= Q(pk__in=Activity.objects.visible_to(user).values('pk'))
        if user.has_perm('accounts.access_work'):
            visible |= Q(work__isnull=False)
        # The lead, the Work and its plan come in the same query: no query per row.
        activities = Activity.objects.filter(visible).select_related(
            'lead', 'work__plan', 'assigned_to', 'completed_by', 'created_by',
        )
        if self.action == 'list':
            # One Work's activities, or lead follow-ups (one lead's, or the Lead Activities page).
            work = self.request.query_params.get('work')
            if work is not None:
                if not re.fullmatch(r'[0-9]+', work):
                    raise ValidationError({'work': 'Choose a Work.'})
                return activities.filter(work_id=int(work)).order_by(*WITHIN_WORK)
            return filter_follow_ups(activities.filter(lead__isnull=False), self.request.query_params, user)
        if self.action == 'destroy':
            # Deleting a follow-up stays with whoever works the lead; staff who only do it complete it instead.
            return activities.filter(Q(lead__in=Lead.objects.visible_to(user)) | Q(work__isnull=False))
        return activities

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

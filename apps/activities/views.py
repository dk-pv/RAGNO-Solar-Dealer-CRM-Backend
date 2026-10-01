import re

from django.db.models import Case, F, IntegerField, Q, Value, When
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.leads.models import Lead

from .models import Activity, ActivityStatus
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


class FollowsParentAccess(BasePermission):
    def has_permission(self, request, view):
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

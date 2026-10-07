import re

from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.leads.models import Lead
from apps.notifications import services as notifications

from .models import Activity, ActivityStatus
from .serializers import ActivityQuerySerializer, ActivitySerializer, CompletionSerializer, WorkActivityQuerySerializer

NOT_YOUR_ACTIVITY = 'You can only update activities assigned to you.'
ALREADY_COMPLETED = 'This activity is already completed.'

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
    # The heading or notes, or the lead it's for by what each row shows of it: its name, phone or ID. Its email and
    # place too, as the leads list searches them, only for whoever can open their leads (the Leads module): staff who
    # only have Activities never search a lead's other details.
    if search := params.get('search', '').strip():
        match = (
            Q(title__icontains=search) | Q(description__icontains=search)
            | Q(lead__in=Lead.objects.search(search, details=False).values('pk'))
        )
        if user.has_perm('leads.view_lead'):
            match |= Q(lead__in=Lead.objects.visible_to(user).search(search).values('pk'))
        follow_ups = follow_ups.filter(match)
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


class ActivityAccess(BasePermission):
    """Who reaches which activities. Every list holds, and every single activity opens for, only what the user may see:
    all of them for an admin; for staff only the activities assigned to them (Activity.objects.visible_to,
    Activity.status_changeable_by), whatever lead or Work they are on.
    - The Lead and Work Activities pages, and opening or completing one activity from them: the Activities module.
    - A lead's or a Work's own list (on its page): the Leads or the Work module.
    - Adding, editing (reassigning, reopening) and deleting: admins. Staff complete their own with the complete action,
      which changes nothing else."""

    def has_permission(self, request, view):
        user = request.user
        if view.action == 'list' and 'work' in request.query_params:
            return user.has_perm('accounts.access_work')
        # One lead's follow-ups; a blank ?lead= filters nothing (the Lead Activities page).
        if view.action == 'list' and request.query_params.get('lead', '').strip():
            return user.has_perm('leads.view_lead')
        if view.action in ('list', 'works', 'retrieve', 'complete'):
            return user.has_perm('accounts.access_activities')
        return bool(user.is_authenticated and user.role_id == Role.ADMIN)

    def has_object_permission(self, request, view, activity):
        # Another staff member's, an admin's or an unassigned activity is refused, never shown; an unknown id is a 404.
        if not activity.status_changeable_by(request.user):
            raise PermissionDenied(NOT_YOUR_ACTIVITY)
        return True


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
    permission_classes = [ActivityAccess]
    pagination_class = ActivityPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        # The lead, the Work and its plan come in the same query: no query per row.
        activities = Activity.objects.select_related('lead', 'work__plan', 'assigned_to', 'completed_by', 'created_by')
        if self.detail:
            # One activity is looked up among all of them: someone else's answers 403 (ActivityAccess), an unknown id 404.
            # Completing locks it, so two requests at once (or a reassignment meanwhile) take turns.
            return activities.select_for_update(of=('self',)) if self.action == 'complete' else activities
        # Every list searches, filters, sorts, counts and pages only what the user may see.
        activities = activities.visible_to(self.request.user)
        if self.action == 'list':
            # One Work's activities, or lead follow-ups (one lead's, or the Lead Activities page).
            work = self.request.query_params.get('work')
            if work is not None:
                if not re.fullmatch(r'[0-9]+', work):
                    raise ValidationError({'work': 'Choose a Work.'})
                return activities.filter(work_id=int(work)).order_by(*WITHIN_WORK)
            return filter_follow_ups(activities.filter(lead__isnull=False), self.request.query_params, self.request.user)
        return activities

    @action(detail=False)
    def works(self, request):
        """The Work Activities page: every Work's activities the user may see (staff: their own), completed ones included,
        with search, filters and paging. Each Work's activities stay together (newest Work first by default)."""
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

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Marks a pending activity completed, with an optional note on what was done: its assignee (with the Activities
        module) or an admin. Only once: a second request is refused and keeps the first note. Admins and the assignee are
        told, as when an admin completes it by editing it."""
        activity = self.get_object()
        if activity.status == ActivityStatus.COMPLETED:
            raise ValidationError({'status': [ALREADY_COMPLETED]})
        completion = CompletionSerializer(data=request.data)
        completion.is_valid(raise_exception=True)
        activity.status, activity.completed_by = ActivityStatus.COMPLETED, request.user
        activity.completed_at, activity.completion_note = timezone.now(), completion.validated_data['completion_note']
        activity.save(update_fields=['status', 'completed_at', 'completed_by', 'completion_note', 'updated_at'])
        notifications.activity_status_changed(activity, request.user)
        return Response(self.get_serializer(activity).data)

    def perform_create(self, serializer):
        activity = serializer.save(created_by=self.request.user)
        notifications.activity_assigned(activity, self.request.user)

    def perform_update(self, serializer):
        """Editing is for admins (ActivityAccess). Whoever it is newly assigned to is told, as are the admins and the
        assignee when its status changes."""
        activity, user = serializer.instance, self.request.user
        changes = serializer.validated_data
        status_changes = 'status' in changes and changes['status'] != activity.status
        assignee_before = activity.assigned_to_id
        activity = serializer.save()
        if activity.assigned_to_id != assignee_before:
            notifications.activity_assigned(activity, user)
        if status_changes:
            notifications.activity_status_changed(activity, user)

    def perform_destroy(self, activity):
        if activity.work_id:
            raise PermissionDenied("A Work's activities are kept as its history. Edit the activity instead.")
        activity.delete()

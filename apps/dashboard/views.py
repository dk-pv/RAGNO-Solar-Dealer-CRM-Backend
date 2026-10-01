"""The Dashboard: counts, follow-ups that need attention, recent records and a timeline, all read from the Lead, Work and
Activity records. The Dashboard stores nothing of its own.

Who sees what follows the modules themselves, on every endpoint: the Dashboard needs the Dashboard module; leads need the
Leads module, and staff see only the leads assigned to them (Lead.objects.visible_to); Works and their activities need
the Work module. A section the user has no module for is null rather than zero.

Follow-ups: a Work's follow-ups are its pending activities (each with its own due date); a lead's follow-up is the
next_follow_up date scheduled on an open lead. A lead's activities are its log (calls, visits, notes): the Leads screens
set no due date or status on them, so they are history in the timeline and never counted as pending follow-ups.

The timeline is the CRM's real events (records.event_feed). Every lead and Work appears in it at least once, through its
creation, with its latest activity or none.
"""

from datetime import date

from django.db.models import Count, F, Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activities.models import Activity, ActivityStatus
from apps.leads.models import PIPELINE, LeadStatus
from apps.works.models import WorkStage

from .records import EVENT_RECORDS, describe_events, event_feed, lead_summary, name, visible, with_latest_activity, work_summary

FOLLOW_UP_BUCKETS = ['overdue', 'today', 'upcoming', 'pending', 'completed']
RECENT = 5


class CanOpenDashboard(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('accounts.access_dashboard')


def scheduled_lead_follow_ups(leads):
    """Open leads with a follow-up date scheduled. Won and Lost leads need no more follow-ups."""
    return leads.filter(status__in=PIPELINE, next_follow_up__isnull=False)


def due_filter(field, bucket, today):
    return {
        'overdue': {f'{field}__lt': today},
        'today': {field: today},
        'upcoming': {f'{field}__gt': today},
        'pending': {},
    }[bucket]


def follow_up_counts(user, leads, works, today, mine=False):
    def counts(queryset, field):
        return queryset.aggregate(
            pending=Count('pk'),
            overdue=Count('pk', filter=Q(**due_filter(field, 'overdue', today))),
            today=Count('pk', filter=Q(**due_filter(field, 'today', today))),
            upcoming=Count('pk', filter=Q(**due_filter(field, 'upcoming', today))),
        )

    sources = {'works': None, 'leads': None}
    if works is not None:
        pending = Activity.objects.filter(work__isnull=False, status=ActivityStatus.PENDING)
        sources['works'] = counts(pending.filter(assigned_to=user) if mine else pending, 'due_date')
    if leads is not None:
        scheduled = scheduled_lead_follow_ups(leads)
        sources['leads'] = counts(scheduled.filter(assigned_to=user) if mine else scheduled, 'next_follow_up')
    total = {
        key: sum(source[key] for source in sources.values() if source)
        for key in ('pending', 'overdue', 'today', 'upcoming')
    }
    return {'total': total, **sources}


class SummaryView(APIView):
    """The counts at the top of the Dashboard, the leads by status and the signed-in user's own numbers."""

    permission_classes = [CanOpenDashboard]

    def get(self, request):
        user, today = request.user, timezone.localdate()
        leads, works = visible(user)
        data = {'today': today, 'leads': None, 'works': None}
        if leads is not None:
            data['leads'] = leads.aggregate(
                total=Count('pk'),
                active=Count('pk', filter=Q(status__in=PIPELINE)),
                confirmed=Count('pk', filter=Q(status=LeadStatus.WON)),
                lost=Count('pk', filter=Q(status=LeadStatus.LOST)),
            )
            by_status = dict(leads.order_by().values_list('status').annotate(Count('pk')))
            data['leads']['by_status'] = [
                {'status': status, 'label': label, 'count': by_status.get(status, 0)} for status, label in LeadStatus.choices
            ]
        if works is not None:
            data['works'] = works.aggregate(
                total=Count('pk'),
                active=Count('pk', filter=~Q(stage=WorkStage.COMPLETED)),
                completed=Count('pk', filter=Q(stage=WorkStage.COMPLETED)),
            )
        data['follow_ups'] = follow_up_counts(user, leads, works, today)
        data['mine'] = {
            'leads': None if leads is None else leads.filter(assigned_to=user, status__in=PIPELINE).count(),
            'works': None if works is None else works.filter(assigned_to=user).exclude(stage=WorkStage.COMPLETED).count(),
            'follow_ups': follow_up_counts(user, leads, works, today, mine=True),
        }
        return Response(data)


def work_follow_up(activity):
    work = activity.work
    return {
        'key': f'activity-{activity.pk}', 'source': 'work', 'activity': activity.pk, 'lead': None, 'work': work.pk,
        'title': activity.get_type_display(), 'description': activity.description, 'customer_name': work.customer_name,
        'assigned_to_name': name(activity.assigned_to), 'due_date': activity.due_date, 'status': activity.status,
        'created_at': activity.created_at, 'created_by_name': name(activity.created_by),
        'completed_at': activity.completed_at, 'completed_by_name': name(activity.completed_by),
    }


def lead_follow_up(lead):
    # A date scheduled on the lead: there is no separate record of when or by whom it was scheduled.
    return {
        'key': f'lead-{lead.pk}', 'source': 'lead', 'activity': None, 'lead': lead.pk, 'work': None,
        'title': 'Next follow-up', 'description': '', 'customer_name': lead.name,
        'assigned_to_name': name(lead.assigned_to), 'due_date': lead.next_follow_up, 'status': ActivityStatus.PENDING,
        'created_at': None, 'created_by_name': None, 'completed_at': None, 'completed_by_name': None,
    }


class FollowUpQuerySerializer(serializers.Serializer):
    bucket = serializers.ChoiceField(choices=FOLLOW_UP_BUCKETS)
    mine = serializers.BooleanField(required=False, default=False)
    limit = serializers.IntegerField(required=False, default=8, min_value=1, max_value=50)


class FollowUpsView(APIView):
    """One group of follow-ups (overdue, due today, upcoming, all pending, or recently completed) from Works and leads
    together, soonest due first (completed: latest first). `count` is the whole group; the first `limit` are listed."""

    permission_classes = [CanOpenDashboard]

    def get(self, request):
        query = FollowUpQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        bucket, mine, limit = query.validated_data['bucket'], query.validated_data['mine'], query.validated_data['limit']
        user, today = request.user, timezone.localdate()
        leads, works = visible(user)
        count, items = 0, []

        if works is not None:
            activities = Activity.objects.filter(work__isnull=False).select_related(
                'work', 'assigned_to', 'created_by', 'completed_by',
            )
            if mine:
                activities = activities.filter(assigned_to=user)
            if bucket == 'completed':
                activities = activities.filter(status=ActivityStatus.COMPLETED).order_by('-completed_at', '-pk')
            else:
                activities = activities.filter(status=ActivityStatus.PENDING, **due_filter('due_date', bucket, today))
                activities = activities.order_by(F('due_date').asc(nulls_last=True), 'created_at', 'pk')
            count += activities.count()
            items += [work_follow_up(activity) for activity in activities[:limit]]

        # Leads have no completed follow-ups to list: completing one means scheduling the next date or closing the lead.
        if leads is not None and bucket != 'completed':
            scheduled = scheduled_lead_follow_ups(leads).select_related('assigned_to')
            if mine:
                scheduled = scheduled.filter(assigned_to=user)
            scheduled = scheduled.filter(**due_filter('next_follow_up', bucket, today)).order_by('next_follow_up', 'pk')
            count += scheduled.count()
            items += [lead_follow_up(lead) for lead in scheduled[:limit]]

        if bucket == 'completed':
            items.sort(key=lambda item: item['completed_at'], reverse=True)
        else:
            items.sort(key=lambda item: (item['due_date'] is None, item['due_date'] or date.max))
        return Response({'count': count, 'results': items[:limit]})


class RecentView(APIView):
    """The most recently added or changed leads and Works, each with its latest activity, or none yet."""

    permission_classes = [CanOpenDashboard]

    def get(self, request):
        leads, works = visible(request.user)
        data = {'leads': None, 'works': None}
        if leads is not None:
            recent = with_latest_activity(leads.select_related('assigned_to'), 'lead').order_by('-updated_at', '-pk')
            data['leads'] = [lead_summary(lead) for lead in recent[:RECENT]]
        if works is not None:
            recent = with_latest_activity(works.select_related('assigned_to'), 'work').order_by('-updated_at', '-pk')
            data['works'] = [work_summary(work) for work in recent[:RECENT]]
        return Response(data)


class TimelineQuerySerializer(serializers.Serializer):
    """The timeline's filters. Dates are calendar days in the CRM's time zone (Asia/Kolkata), both ends included."""

    user = serializers.IntegerField(required=False)
    record = serializers.ChoiceField(choices=EVENT_RECORDS, required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        if 'date_from' in attrs and 'date_to' in attrs and attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError({'date_to': 'Choose an end date on or after the start date.'})
        return attrs


class TimelinePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class TimelineView(APIView):
    """What happened in the CRM, newest first, by everyone whose work this user may see: leads added, leads converted
    into Works, activities added and activities completed. Paginated; filtered by user, record and date."""

    permission_classes = [CanOpenDashboard]

    def get(self, request):
        query = TimelineQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        paginator = TimelinePagination()
        rows = paginator.paginate_queryset(event_feed(request.user, query.validated_data), request, view=self)
        return paginator.get_paginated_response(describe_events(rows))

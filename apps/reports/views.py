"""Reports: what happened over a chosen period, grouped, with the records behind every number, and exports of exactly what
is shown. Read from the Lead, Work and Activity records; Reports store nothing of their own.

Access: every report needs the Reports module. Then the same rules as everywhere else: leads need the Leads module and
staff see only the leads assigned to them; Works and their activities need the Work module. Every summary, row and
export is computed from those visible records only. Exports leave out phone numbers and emails.

Periods are calendar days in the CRM's time zone (Asia/Kolkata), both ends included: leads and Works by when they were
created (a Work is created when its lead is converted), activities by when they were created, due or completed.

Not reported, because it isn't recorded: when a Work reached a stage or was completed, who changed a lead's status, a
Work's stage or an assignee, and who last edited a record. Only each record's current state and last-changed time are
stored. A lead's activities are its log: the Leads screens set no status or due date on them, so they count as
activities but never as pending, completed or overdue follow-ups, which are a Work's.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DateField, DateTimeField, F, Q, Sum
from django.db.models.functions import Trunc
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Role, User
from apps.activities.models import Activity, ActivityStatus, ActivityType
from apps.dashboard.records import EVENT_KINDS, EVENT_RECORDS, describe_events, event_feed, latest_activity, name, visible, with_latest_activity
from apps.leads.models import PIPELINE, Lead, LeadSource, LeadStatus
from apps.leads.views import STATUS_RANK
from apps.works.models import Work, WorkStage
from apps.works.views import STAGE_RANK

from .exports import export

EXPORT_FORMATS = ['csv', 'xlsx']


class CanOpenReports(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('accounts.access_reports')


class ReportPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


class AssigneeField(serializers.CharField):
    """A user's id, or "none" for records assigned to no one."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value == 'none':
            return None
        if not value.isdigit():
            raise serializers.ValidationError('Choose a user, or "none" for unassigned.')
        return int(value)


class PeriodQuery(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    export = serializers.ChoiceField(choices=EXPORT_FORMATS, required=False)

    def validate(self, attrs):
        if 'date_from' in attrs and 'date_to' in attrs and attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError({'date_to': 'Choose an end date on or after the start date.'})
        return attrs


def in_period(records, field, params):
    """Records whose `field` falls on the period's days in the CRM's time zone."""
    is_moment = isinstance(records.model._meta.get_field(field.split('__')[0]), DateTimeField)
    day = f'{field}__date' if is_moment else field
    if 'date_from' in params:
        records = records.filter(**{f'{day}__gte': params['date_from']})
    if 'date_to' in params:
        records = records.filter(**{f'{day}__lte': params['date_to']})
    return records


def money(value):
    return str((value or Decimal(0)).quantize(Decimal('0.01')))


def period_start(unit, day):
    if unit == 'week':
        return day - timedelta(days=day.weekday())  # weeks start on Monday, as the database counts them
    if unit == 'month':
        return day.replace(day=1)
    return day


def next_period(unit, day):
    if unit == 'week':
        return day + timedelta(days=7)
    if unit == 'month':
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day + timedelta(days=1)


def trend(params, **series):
    """Counts per day, week or month (by the period's length) in the CRM's time zone, empty periods included.
    series: name -> (records already limited to the period, date field, {name: aggregate} summed alongside)."""
    start, end = params.get('date_from'), params.get('date_to')
    span = (end - start).days if start and end else None
    unit = 'day' if span is not None and span <= 31 else 'week' if span is not None and span <= 186 else 'month'
    found = {}
    for key, (records, field, sums) in series.items():
        rows = (
            records.order_by().annotate(period=Trunc(field, unit, output_field=DateField()))
            .values('period').annotate(count=Count('pk'), **sums)
        )
        for row in rows:
            point = found.setdefault(row['period'], {})
            point[key] = row['count']
            for total in sums:
                point[f'{key}_{total}'] = money(row[total])
    if start is None and not found:
        return {'unit': unit, 'points': []}
    first = period_start(unit, start or min(found))
    last = end or max([timezone.localdate(), *found])
    blank = {key: 0 for key in series} | {f'{key}_{total}': money(0) for key, (_, _, sums) in series.items() for total in sums}
    points, current = [], first
    while current <= last:
        points.append({'start': current, **blank, **found.get(current, {})})
        current = next_period(unit, current)
    return {'unit': unit, 'points': points}


def by_user(records, field='assigned_to', **aggregates):
    """Counts per user (and one row for none), ordered by name. Facts only: no ranking."""
    rows = records.order_by().values(field, f'{field}__name').annotate(count=Count('pk'), **aggregates)
    result = [
        {'id': row[field], 'name': row[f'{field}__name'], 'count': row['count'], **{key: row[key] for key in aggregates}}
        for row in rows
    ]
    return sorted(result, key=lambda row: (row['id'] is None, (row['name'] or '').lower()))


def choice_counts(records, field, choices, empty_label=None):
    found = dict(records.order_by().values_list(field).annotate(Count('pk')))
    rows = [{'value': value, 'label': label, 'count': found.get(value, 0)} for value, label in choices]
    if empty_label and found.get(''):
        rows.append({'value': 'none', 'label': empty_label, 'count': found['']})
    return rows


class RowsView(APIView):
    """A report's records, a page at a time, or all of them as CSV or Excel with the same filters."""

    permission_classes = [CanOpenReports]
    query_class = PeriodQuery
    export_name = 'report'
    columns = []  # (header, value(record)) for exports

    def records(self, params):
        raise NotImplementedError

    def row(self, record):
        raise NotImplementedError

    def get(self, request):
        query = self.query_class(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        records = self.records(params)
        if params.get('export'):
            rows = ([value(record) for _, value in self.columns] for record in records.iterator(chunk_size=500))
            return export(params['export'], self.export_name, [header for header, _ in self.columns], rows)
        paginator = ReportPagination()
        page = paginator.paginate_queryset(records, request, view=self)
        return paginator.get_paginated_response([self.row(record) for record in page])


class SummaryView(APIView):
    permission_classes = [CanOpenReports]
    query_class = PeriodQuery

    def get(self, request):
        query = self.query_class(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(self.summary(request.user, query.validated_data))


def latest_text(record):
    latest = latest_activity(record)
    return f"{latest['type_display']} · {timezone.localtime(latest['at']):%Y-%m-%d %H:%M}" if latest else 'No activity yet'


# Leads

LEAD_ORDERS = {
    '-created_at': ['-created_at', '-pk'],
    'created_at': ['created_at', 'pk'],
    '-updated_at': ['-updated_at', '-pk'],
    'name': ['name', 'pk'],
    'status': [STATUS_RANK, '-created_at', '-pk'],
}


class LeadQuery(PeriodQuery):
    status = serializers.ChoiceField(choices=LeadStatus.choices, required=False)
    assigned_to = AssigneeField(required=False)
    source = serializers.ChoiceField(choices=[*LeadSource.values, 'none'], required=False)
    ordering = serializers.ChoiceField(choices=list(LEAD_ORDERS), required=False)


def lead_records(user, params):
    """Leads created in the period, with the report's filters."""
    leads, _ = visible(user)
    if leads is None:
        raise PermissionDenied("Your role doesn't include the Leads module.")
    leads = in_period(leads, 'created_at', params)
    if 'status' in params:
        leads = leads.filter(status=params['status'])
    if 'assigned_to' in params:
        leads = leads.filter(assigned_to=params['assigned_to'])
    if 'source' in params:
        leads = leads.filter(source='' if params['source'] == 'none' else params['source'])
    return leads


class LeadSummaryView(SummaryView):
    query_class = LeadQuery

    def summary(self, user, params):
        leads = lead_records(user, params)
        return {
            'totals': leads.aggregate(
                created=Count('pk'),
                open=Count('pk', filter=Q(status__in=PIPELINE)),
                confirmed=Count('pk', filter=Q(status=LeadStatus.WON)),
                lost=Count('pk', filter=Q(status=LeadStatus.LOST)),
            ),
            'by_status': choice_counts(leads, 'status', LeadStatus.choices),
            'by_source': choice_counts(leads, 'source', LeadSource.choices, empty_label='Not set'),
            'by_staff': by_user(
                leads, open=Count('pk', filter=Q(status__in=PIPELINE)), confirmed=Count('pk', filter=Q(status=LeadStatus.WON)),
            ),
            'trend': trend(params, created=(leads, 'created_at', {})),
        }


class LeadReportView(RowsView):
    query_class = LeadQuery
    export_name = 'lead-report'
    columns = [
        ('Lead ID', lambda lead: lead.pk),
        ('Customer', lambda lead: lead.name),
        ('Status', lambda lead: lead.get_status_display()),
        ('Assigned to', lambda lead: name(lead.assigned_to) or 'Unassigned'),
        ('Source', lambda lead: lead.get_source_display() or 'Not set'),
        ('Plan', lambda lead: lead.plan.name),
        ('Amount', lambda lead: lead.amount),
        ('Created', lambda lead: lead.created_at),
        ('Last updated', lambda lead: lead.updated_at),
        ('Latest activity', latest_text),
    ]

    def records(self, params):
        leads = lead_records(self.request.user, params).select_related('assigned_to', 'plan')
        return with_latest_activity(leads, 'lead').order_by(*LEAD_ORDERS[params.get('ordering', '-created_at')])

    def row(self, lead):
        return {
            'id': lead.pk, 'customer_name': lead.name, 'status': lead.status, 'status_display': lead.get_status_display(),
            'assigned_to': lead.assigned_to_id, 'assigned_to_name': name(lead.assigned_to), 'source': lead.source,
            'source_display': lead.get_source_display(), 'plan_name': lead.plan.name, 'amount': money(lead.amount),
            'created_at': lead.created_at, 'updated_at': lead.updated_at, 'latest_activity': latest_activity(lead),
        }


# Works

WORK_ORDERS = {
    '-created_at': ['-created_at', '-pk'],
    'created_at': ['created_at', 'pk'],
    '-updated_at': ['-updated_at', '-pk'],
    'customer_name': ['customer_name', 'pk'],
    'stage': [STAGE_RANK, '-created_at', '-pk'],
    '-amount': ['-amount', '-pk'],
    'amount': ['amount', 'pk'],
}


class WorkQuery(PeriodQuery):
    stage = serializers.ChoiceField(choices=WorkStage.choices, required=False)
    assigned_to = AssigneeField(required=False)
    ordering = serializers.ChoiceField(choices=list(WORK_ORDERS), required=False)


def work_records(user, params):
    """Works created (their lead converted) in the period, with the report's filters."""
    _, works = visible(user)
    if works is None:
        raise PermissionDenied("Your role doesn't include the Work module.")
    works = in_period(works, 'created_at', params)
    if 'stage' in params:
        works = works.filter(stage=params['stage'])
    if 'assigned_to' in params:
        works = works.filter(assigned_to=params['assigned_to'])
    return works


class WorkSummaryView(SummaryView):
    query_class = WorkQuery

    def summary(self, user, params):
        works = work_records(user, params)
        completed = Q(stage=WorkStage.COMPLETED)
        totals = works.aggregate(
            created=Count('pk'), value=Sum('amount'), completed=Count('pk', filter=completed),
            completed_value=Sum('amount', filter=completed),
        )
        by_stage = {row['stage']: row for row in works.order_by().values('stage').annotate(count=Count('pk'), value=Sum('amount'))}
        return {
            # Amounts are each Work's confirmed amount, fixed at conversion: later plan price changes never alter them.
            'totals': {
                'created': totals['created'], 'amount': money(totals['value']),
                'completed': totals['completed'], 'completed_amount': money(totals['completed_value']),
            },
            'by_stage': [
                {'value': stage, 'label': label, 'count': by_stage.get(stage, {}).get('count', 0),
                 'amount': money(by_stage.get(stage, {}).get('value'))}
                for stage, label in WorkStage.choices
            ],
            'by_staff': [
                {**row, 'amount': money(row.pop('value'))}
                for row in by_user(works, value=Sum('amount'), completed=Count('pk', filter=completed))
            ],
            # Each point: the Works created and their confirmed value (created_value).
            'trend': trend(params, created=(works, 'created_at', {'value': Sum('amount')})),
        }


class WorkReportView(RowsView):
    query_class = WorkQuery
    export_name = 'work-report'
    columns = [
        ('Work ID', lambda work: work.pk),
        ('Lead ID', lambda work: work.lead_id),
        ('Customer', lambda work: work.customer_name),
        ('Stage', lambda work: work.get_stage_display()),
        ('Assigned to', lambda work: name(work.assigned_to) or 'Unassigned'),
        ('Plan', lambda work: work.plan.name),
        ('Confirmed amount', lambda work: work.amount),
        ('Due date', lambda work: work.due_date),
        ('Created (converted)', lambda work: work.created_at),
        ('Last updated', lambda work: work.updated_at),
        ('Latest activity', latest_text),
    ]

    def records(self, params):
        works = work_records(self.request.user, params).select_related('assigned_to', 'plan')
        return with_latest_activity(works, 'work').order_by(*WORK_ORDERS[params.get('ordering', '-created_at')])

    def row(self, work):
        return {
            'id': work.pk, 'lead': work.lead_id, 'customer_name': work.customer_name, 'stage': work.stage,
            'stage_display': work.get_stage_display(), 'assigned_to': work.assigned_to_id,
            'assigned_to_name': name(work.assigned_to), 'plan_name': work.plan.name, 'amount': money(work.amount),
            'due_date': work.due_date, 'created_at': work.created_at, 'updated_at': work.updated_at,
            'latest_activity': latest_activity(work),
        }


# Activities

ACTIVITY_DATES = {'created': 'created_at', 'due': 'due_date', 'completed': 'completed_at'}
ACTIVITY_ORDERS = {
    '-created_at': ['-created_at', '-pk'],
    'created_at': ['created_at', 'pk'],
    'due_date': [F('due_date').asc(nulls_last=True), '-pk'],
    '-completed_at': [F('completed_at').desc(nulls_last=True), '-pk'],
}


class ActivityQuery(PeriodQuery):
    date_field = serializers.ChoiceField(choices=list(ACTIVITY_DATES), required=False, default='created')
    assigned_to = AssigneeField(required=False)
    status = serializers.ChoiceField(choices=ActivityStatus.choices, required=False)
    overdue = serializers.BooleanField(required=False, default=False)
    record = serializers.ChoiceField(choices=['lead', 'work'], required=False)
    type = serializers.ChoiceField(choices=ActivityType.choices, required=False)
    lead = serializers.IntegerField(required=False)
    work = serializers.IntegerField(required=False)
    ordering = serializers.ChoiceField(choices=list(ACTIVITY_ORDERS), required=False)


def activity_records(user, params, period=True):
    """Activities with the report's filters, and (unless period=False) on the period by the chosen date."""
    leads, works = visible(user)
    if leads is None and works is None:
        raise PermissionDenied("Your role doesn't include the Leads or Work module.")
    scope = Q(pk__in=[])
    if leads is not None:
        scope |= Q(lead__in=leads)
    if works is not None:
        scope |= Q(work__isnull=False)
    activities = Activity.objects.filter(scope)
    if params.get('record'):
        activities = activities.filter(**{f"{params['record']}__isnull": False})
    for field in ('type', 'assigned_to', 'lead', 'work'):
        if field in params:
            activities = activities.filter(**{field: params[field]})
    # Pending, completed and overdue are a Work's follow-ups: a lead's activities have no such status.
    if 'status' in params:
        activities = activities.filter(work__isnull=False, status=params['status'])
    if params.get('overdue'):
        activities = activities.filter(work__isnull=False, status=ActivityStatus.PENDING, due_date__lt=timezone.localdate())
    if period:
        activities = in_period(activities, ACTIVITY_DATES[params['date_field']], params)
    return activities


def follow_up_counts(today):
    on_work = Q(work__isnull=False)
    pending = on_work & Q(status=ActivityStatus.PENDING)
    return {
        'pending': Count('pk', filter=pending),
        'completed': Count('pk', filter=on_work & Q(status=ActivityStatus.COMPLETED)),
        'overdue': Count('pk', filter=pending & Q(due_date__lt=today)),
    }


class ActivitySummaryView(SummaryView):
    query_class = ActivityQuery

    def summary(self, user, params):
        today = timezone.localdate()
        activities = activity_records(user, params)
        unfiltered_by_period = activity_records(user, params, period=False)
        work_activities = unfiltered_by_period.filter(work__isnull=False)
        totals = activities.aggregate(
            total=Count('pk'), on_leads=Count('pk', filter=Q(lead__isnull=False)), on_works=Count('pk', filter=Q(work__isnull=False)),
            **follow_up_counts(today),
        )
        totals['due_in_period'] = in_period(work_activities.filter(due_date__isnull=False), 'due_date', params).count()
        totals['completed_in_period'] = in_period(work_activities.filter(completed_at__isnull=False), 'completed_at', params).count()

        top = list(activities.order_by().values('lead', 'work').annotate(count=Count('pk'), **follow_up_counts(today)).order_by('-count', 'lead', 'work')[:10])
        lead_names = dict(Lead.objects.filter(pk__in=[row['lead'] for row in top if row['lead']]).values_list('pk', 'name'))
        work_names = dict(Work.objects.filter(pk__in=[row['work'] for row in top if row['work']]).values_list('pk', 'customer_name'))
        return {
            'totals': totals,
            'by_staff': by_user(activities, **follow_up_counts(today)),
            'by_type': choice_counts(activities, 'type', ActivityType.choices),
            'by_record': [
                {**row, 'customer_name': work_names.get(row['work']) if row['work'] else lead_names.get(row['lead'])}
                for row in top
            ],
            'trend': trend(
                params,
                created=(in_period(unfiltered_by_period, 'created_at', params), 'created_at', {}),
                completed=(in_period(work_activities.filter(completed_at__isnull=False), 'completed_at', params), 'completed_at', {}),
            ),
        }


def record_label(activity):
    return f'Work #{activity.work_id}' if activity.work_id else f'Lead #{activity.lead_id}'


class ActivityReportView(RowsView):
    query_class = ActivityQuery
    export_name = 'activity-report'
    columns = [
        ('Activity ID', lambda activity: activity.pk),
        ('Type', lambda activity: activity.get_type_display()),
        ('Description', lambda activity: activity.description),
        ('Lead / Work', record_label),
        ('Customer', lambda activity: activity.work.customer_name if activity.work_id else activity.lead.name),
        ('Assigned to', lambda activity: name(activity.assigned_to) or 'Unassigned'),
        ('Due date', lambda activity: activity.due_date),
        ('Status', lambda activity: activity.get_status_display() if activity.work_id else 'Log'),
        ('Created', lambda activity: activity.created_at),
        ('Created by', lambda activity: name(activity.created_by)),
        ('Completed', lambda activity: activity.completed_at),
        ('Completed by', lambda activity: name(activity.completed_by)),
    ]

    def records(self, params):
        activities = activity_records(self.request.user, params).select_related(
            'lead', 'work', 'assigned_to', 'created_by', 'completed_by',
        )
        return activities.order_by(*ACTIVITY_ORDERS[params.get('ordering', '-created_at')])

    def row(self, activity):
        today = timezone.localdate()
        on_work = activity.work_id is not None
        return {
            'id': activity.pk, 'type': activity.type, 'type_display': activity.get_type_display(),
            'description': activity.description, 'lead': activity.lead_id, 'work': activity.work_id,
            'customer_name': activity.work.customer_name if on_work else activity.lead.name,
            'assigned_to': activity.assigned_to_id, 'assigned_to_name': name(activity.assigned_to),
            'due_date': activity.due_date,
            # null for a lead's activity: its log has no follow-up status.
            'status': activity.status if on_work else None,
            'overdue': on_work and activity.status == ActivityStatus.PENDING and activity.due_date is not None and activity.due_date < today,
            'created_at': activity.created_at, 'created_by_name': name(activity.created_by),
            'completed_at': activity.completed_at, 'completed_by_name': name(activity.completed_by),
        }


# Staff

STAFF_COLUMNS = [
    ('leads_assigned', 'Leads assigned'),
    ('works_assigned', 'Works assigned'),
    ('activities_assigned', 'Activities assigned'),
    ('pending', 'Pending follow-ups'),
    ('completed', 'Completed follow-ups'),
    ('overdue', 'Overdue follow-ups'),
    ('leads_added', 'Leads added'),
    ('works_converted', 'Leads converted to Works'),
    ('activities_added', 'Activities added'),
    ('follow_ups_completed', 'Follow-ups completed'),
]


class StaffReportView(APIView):
    """Per user, for the period: what was assigned to them and what they did, from the visible records. Facts only: no
    score and no ranking (users are listed by name). Edits aren't recorded, so there is no "records updated" column."""

    permission_classes = [CanOpenReports]

    def get(self, request):
        query = PeriodQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        user, today = request.user, timezone.localdate()
        leads, works = visible(user)
        if leads is None and works is None:
            raise PermissionDenied("Your role doesn't include the Leads or Work module.")

        counts = {}  # user id (None: unassigned) -> column -> count

        def add(records, field, **columns):
            for row in records.order_by().values(field).annotate(**columns):
                counts.setdefault(row[field], {}).update({key: row[key] for key in columns})

        activities = activity_records(user, {'date_field': 'created'}, period=False)
        if leads is not None:
            period_leads = in_period(leads, 'created_at', params)
            add(period_leads, 'assigned_to', leads_assigned=Count('pk'))
            add(period_leads, 'created_by', leads_added=Count('pk'))
        if works is not None:
            period_works = in_period(works, 'created_at', params)
            add(period_works, 'assigned_to', works_assigned=Count('pk'))
            add(period_works, 'created_by', works_converted=Count('pk'))
        period_activities = in_period(activities, 'created_at', params)
        add(period_activities, 'assigned_to', activities_assigned=Count('pk'), **follow_up_counts(today))
        add(period_activities, 'created_by', activities_added=Count('pk'))
        add(in_period(activities.filter(completed_at__isnull=False), 'completed_at', params), 'completed_by', follow_ups_completed=Count('pk'))

        # Admins see everyone active, even with nothing to show; staff see the people in their own records, and themselves.
        ids = {key for key in counts if key is not None} | {user.pk}
        people = User.objects.filter(Q(pk__in=ids) | Q(is_active=True)) if user.role_id == Role.ADMIN else User.objects.filter(pk__in=ids)
        rows = [
            {'user': {'id': person.pk, 'name': person.name, 'is_active': person.is_active},
             **{key: counts.get(person.pk, {}).get(key, 0) for key, _ in STAFF_COLUMNS}}
            for person in people.order_by('name', 'pk')
        ]
        if None in counts:
            rows.append({'user': None, **{key: counts[None].get(key, 0) for key, _ in STAFF_COLUMNS}})

        if params.get('export'):
            header = ['User', *[label for _, label in STAFF_COLUMNS]]
            body = ([row['user']['name'] if row['user'] else 'Unassigned', *[row[key] for key, _ in STAFF_COLUMNS]] for row in rows)
            return export(params['export'], 'staff-report', header, body)
        return Response({'results': rows})


# History

class HistoryQuery(PeriodQuery):
    user = serializers.IntegerField(required=False)
    record = serializers.ChoiceField(choices=EVENT_RECORDS, required=False)
    kind = serializers.ChoiceField(choices=list(EVENT_KINDS), required=False)


def event_columns(event):
    record = event['work'] or event['lead']
    moment = timezone.localtime(event['at'])
    activity = event['activity']
    work = event['work']
    return [
        moment.strftime('%Y-%m-%d'), moment.strftime('%H:%M:%S'), (event['user'] or {}).get('name') or '',
        EVENT_KINDS[event['kind']],
        f"Work #{work['id']}" if work else f"Lead #{event['lead']['id']}" if event['lead'] else '',
        record['customer_name'] if record else '', (record or {}).get('assigned_to_name') or 'Unassigned',
        work['stage_display'] if work else (event['lead'] or {}).get('status_display', ''),
        f"{activity['type_display']}: {activity['description']}" if activity else '',
    ]


class HistoryReportView(APIView):
    """The CRM's real events in the period (the same ones as the Dashboard timeline), filterable, paged and exportable.
    Only recorded events: a record without activity has no event beyond its creation."""

    permission_classes = [CanOpenReports]

    def get(self, request):
        query = HistoryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        feed = event_feed(request.user, params)
        if params.get('export'):
            # ponytail: the whole filtered history is described in memory; stream it in chunks if exports reach 100k events.
            header = ['Date', 'Time', 'User', 'Event', 'Record', 'Customer', 'Assigned to', 'Status / stage', 'Activity']
            return export(params['export'], 'crm-history', header, (event_columns(event) for event in describe_events(list(feed))))
        paginator = ReportPagination()
        rows = paginator.paginate_queryset(feed, request, view=self)
        return paginator.get_paginated_response(describe_events(rows))

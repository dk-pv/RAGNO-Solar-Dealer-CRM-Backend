"""What the Dashboard and Reports read: which leads and Works a user may see, each record in brief with its latest
activity, and the CRM's real events.

Events come from what the records already store with a user and a time: a lead added (created_by, created_at), a lead
converted into its Work (the Work's created_by, created_at), an activity added (created_by, created_at) and an activity
completed (completed_by, completed_at). Status and stage changes, assignments and edits keep no record of who made them
or when, so they are never shown rather than guessed. A record with no activity shows none: no event is made up for it.
"""

from django.contrib.auth import get_user_model
from django.db.models import BigIntegerField, CharField, F, OuterRef, Subquery, Value
from django.db.models.functions import Cast

from apps.activities.models import Activity, ActivityType
from apps.leads.models import Lead
from apps.works.models import Work

User = get_user_model()
TYPE_LABELS = dict(ActivityType.choices)
EVENT_KINDS = {
    'lead_created': 'Lead added',
    'work_created': 'Lead converted to Work',
    'activity_added': 'Activity added',
    'activity_completed': 'Activity completed',
}
EVENT_RECORDS = ['leads', 'works', 'activities']


def visible(user):
    """The leads and the Works this user may see, or None for a module they don't have. Leads: the Leads module, and
    staff see only the leads assigned to them. Works and their activities: the Work module."""
    leads = Lead.objects.visible_to(user) if user.has_perm('leads.view_lead') else None
    works = Work.objects.all() if user.has_perm('accounts.access_work') else None
    return leads, works


def name(user):
    return user.name if user else None


def with_latest_activity(records, field):
    """Each record with its newest activity's type and time, in the same query (None when it has no activity)."""
    latest = Activity.objects.filter(**{field: OuterRef('pk')}).order_by('-created_at', '-pk')
    return records.annotate(
        latest_activity_type=Subquery(latest.values('type')[:1]),
        latest_activity_at=Subquery(latest.values('created_at')[:1]),
    )


def latest_activity(record):
    if record.latest_activity_at is None:
        return None
    return {'type_display': TYPE_LABELS.get(record.latest_activity_type, record.latest_activity_type), 'at': record.latest_activity_at}


def lead_summary(lead):
    return {
        'id': lead.pk, 'customer_name': lead.name, 'status': lead.status, 'status_display': lead.get_status_display(),
        'assigned_to_name': name(lead.assigned_to), 'created_at': lead.created_at, 'updated_at': lead.updated_at,
        'latest_activity': latest_activity(lead),
    }


def work_summary(work):
    return {
        'id': work.pk, 'lead': work.lead_id, 'customer_name': work.customer_name, 'stage': work.stage,
        'stage_display': work.get_stage_display(), 'assigned_to_name': name(work.assigned_to),
        'created_at': work.created_at, 'updated_at': work.updated_at, 'latest_activity': latest_activity(work),
    }


# Typed, so Postgres can combine it with the id columns of the other kinds of event (a bare NULL reads as text there).
NO_ID = Cast(Value(None), output_field=BigIntegerField())


def _events(records, kind, at, user, params, lead=None, work=None, activity=None):
    """One kind of event as rows of one shape, so the database can combine every kind into a single ordered feed."""
    if 'user' in params:
        records = records.filter(**{user: params['user']})
    if 'date_from' in params:
        records = records.filter(**{f'{at}__date__gte': params['date_from']})
    if 'date_to' in params:
        records = records.filter(**{f'{at}__date__lte': params['date_to']})
    return records.order_by().values(
        ev_kind=Value(kind, output_field=CharField()),
        ev_at=F(at),
        ev_user=F(user),
        ev_lead=F(lead) if lead else NO_ID,
        ev_work=F(work) if work else NO_ID,
        ev_activity=F(activity) if activity else NO_ID,
    )


def event_feed(user, params):
    """Every real event this user may see, newest first, as one database query (a list when there is none to see).
    params: user, record (leads, works or activities), kind, date_from and date_to (calendar days in Asia/Kolkata)."""
    leads, works = visible(user)
    record, kind = params.get('record'), params.get('kind')
    wanted = lambda event: kind in (None, event)  # noqa: E731
    sources = []
    if leads is not None:
        lead_activities = Activity.objects.filter(lead__in=leads)
        if record in (None, 'leads') and wanted('lead_created'):
            sources.append(_events(leads, 'lead_created', 'created_at', 'created_by', params, lead='pk'))
        if record in (None, 'leads', 'activities'):
            if wanted('activity_added'):
                sources.append(_events(lead_activities, 'activity_added', 'created_at', 'created_by', params, lead='lead', activity='pk'))
            if wanted('activity_completed'):
                sources.append(_events(
                    lead_activities.filter(completed_at__isnull=False), 'activity_completed', 'completed_at', 'completed_by',
                    params, lead='lead', activity='pk',
                ))
    if works is not None:
        work_activities = Activity.objects.filter(work__isnull=False)
        if record in (None, 'works') and wanted('work_created'):
            sources.append(_events(works, 'work_created', 'created_at', 'created_by', params, lead='lead', work='pk'))
        if record in (None, 'works', 'activities'):
            if wanted('activity_added'):
                sources.append(_events(work_activities, 'activity_added', 'created_at', 'created_by', params, work='work', activity='pk'))
            if wanted('activity_completed'):
                sources.append(_events(
                    work_activities.filter(completed_at__isnull=False), 'activity_completed', 'completed_at', 'completed_by',
                    params, work='work', activity='pk',
                ))
    return sources[0].union(*sources[1:], all=True).order_by('-ev_at', 'ev_kind') if sources else []


def describe_events(rows):
    """Events with their user, record and activity, loaded together: one query each, not one per row."""
    lead_ids = {row['ev_lead'] for row in rows if row['ev_lead'] and row['ev_kind'] != 'work_created'}
    work_ids = {row['ev_work'] for row in rows if row['ev_work']}
    activity_ids = {row['ev_activity'] for row in rows if row['ev_activity']}
    user_ids = {row['ev_user'] for row in rows if row['ev_user']}
    leads = with_latest_activity(Lead.objects.filter(pk__in=lead_ids).select_related('assigned_to'), 'lead')
    works = with_latest_activity(Work.objects.filter(pk__in=work_ids).select_related('assigned_to'), 'work')
    leads = {lead.pk: lead_summary(lead) for lead in leads} if lead_ids else {}
    works = {work.pk: work_summary(work) for work in works} if work_ids else {}
    activities = {
        activity.pk: {
            'id': activity.pk, 'type': activity.type, 'type_display': activity.get_type_display(),
            'description': activity.description, 'status': activity.status, 'due_date': activity.due_date,
        }
        for activity in Activity.objects.filter(pk__in=activity_ids)
    } if activity_ids else {}
    users = dict(User.objects.filter(pk__in=user_ids).values_list('pk', 'name')) if user_ids else {}

    return [
        {
            'key': f"{row['ev_kind']}-{row['ev_activity'] or row['ev_work'] or row['ev_lead']}",
            'kind': row['ev_kind'],
            'at': row['ev_at'],
            'user': {'id': row['ev_user'], 'name': users.get(row['ev_user'])} if row['ev_user'] else None,
            # A Work's events carry its lead's number (work.lead), not the lead's details.
            'lead': leads.get(row['ev_lead']) if row['ev_kind'] != 'work_created' else None,
            'work': works.get(row['ev_work']),
            'activity': activities.get(row['ev_activity']),
        }
        for row in rows
    ]

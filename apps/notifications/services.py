"""What the CRM notifies people about, called from the views that make the change (each inside the request's transaction,
so a notification is never left behind by a change that was rolled back).

- A staff member or admin is told when an activity is assigned to them, and when one of theirs is completed or reopened
  by someone else.
- Admins are told when a lead is added, a lead is converted to its Work, a Work is completed, an activity is completed or
  reopened, and the CRM's data is reset.

Whoever made the change is never told about it (they know), so an admin working alone gets no notifications of their own
doing. An assignment that doesn't change sends nothing: the views call activity_assigned() only when the assignee changes.
"""

from django.contrib.auth import get_user_model

from apps.accounts.models import Role
from apps.activities.models import ActivityStatus

from .models import Notification, NotificationKind

User = get_user_model()


def admins(exclude=None):
    """The active admins, without `exclude` (whoever made the change)."""
    users = User.objects.filter(is_active=True, role_id=Role.ADMIN)
    return users.exclude(pk=exclude.pk) if exclude is not None else users


def send(recipients, kind, title, message, *, lead=None, work=None, activity=None):
    """One notification per distinct recipient (None and inactive users are skipped), in one query."""
    seen = {}
    for user in recipients:
        if user is not None and user.is_active:
            seen.setdefault(user.pk, user)
    Notification.objects.bulk_create([
        Notification(recipient=user, kind=kind, title=title, message=message[:300], lead=lead, work=work, activity=activity)
        for user in seen.values()
    ])


def _about(activity):
    """"Call about the quotation for Asha Menon": what the activity is and whose it is."""
    subject = activity.title or activity.get_type_display()
    customer = activity.lead.name if activity.lead_id else activity.work.customer_name
    return f'{subject} for {customer}'


def activity_assigned(activity, actor):
    """The assignee is told, unless they assigned it to themselves."""
    if activity.assigned_to is None or activity.assigned_to == actor:
        return
    send(
        [activity.assigned_to], NotificationKind.ACTIVITY_ASSIGNED, 'New activity assigned to you',
        f'{_about(activity)} is assigned to you.', lead=activity.lead, work=activity.work, activity=activity,
    )


def activity_status_changed(activity, actor):
    """Admins and the assignee are told that it was completed (or, a Work's activity, reopened)."""
    completed = activity.status == ActivityStatus.COMPLETED
    send(
        [*admins(exclude=actor), activity.assigned_to if activity.assigned_to != actor else None],
        NotificationKind.ACTIVITY_STATUS, 'Activity completed' if completed else 'Activity reopened',
        f'{actor.name} marked {_about(activity)} as {"completed" if completed else "pending"}.',
        lead=activity.lead, work=activity.work, activity=activity,
    )


def lead_created(lead, actor):
    send(
        admins(exclude=actor), NotificationKind.LEAD_CREATED, 'New lead created',
        f'{lead.name} was added as a new lead by {actor.name}.', lead=lead,
    )


def lead_converted(lead, actor):
    send(
        admins(exclude=actor), NotificationKind.LEAD_CONVERTED, 'Lead converted to Work',
        f'{lead.name} was converted to Work #{lead.work.pk} by {actor.name}.', lead=lead, work=lead.work,
    )


def work_completed(work, actor):
    send(
        admins(exclude=actor), NotificationKind.WORK_COMPLETED, 'Work completed',
        f'Installation work for {work.customer_name} (Work #{work.pk}) has been completed by {actor.name}.', work=work,
    )


def crm_reset(actor, counts):
    """Every admin, the one who did it included: the record that the data was cleared, and by whom."""
    send(
        admins(), NotificationKind.CRM_RESET, 'CRM data reset',
        f"{actor.name} cleared the CRM's data: {counts['leads']} leads, {counts['works']} works and "
        f"{counts['activities']} activities were removed. Users and settings were kept.",
    )

"""Clearing the CRM's business data, and nothing else.

What goes: every lead, every Work with its documents (their files are deleted from Cloudinary once the reset commits),
every activity (lead follow-ups and Work activities) and every notification about them. What stays, untouched: users,
roles and their module access, departments, the solar plans and their prices, the notifications that record earlier
resets (the audit trail), and Django's own tables (sessions, permissions, migrations). No table is dropped or recreated:
the records are deleted through the models, in one transaction, so a failure part-way leaves everything as it was.

Only an admin can run it: through the API (POST /api/maintenance/reset-crm-data/, with the confirmation phrase) or on the
server (python manage.py reset_crm_data --yes). Both go through reset_crm_data() below, which also records who did it in
the server log and in a notification to every admin.
"""

import logging

from django.db import transaction

from apps.activities.models import Activity
from apps.leads.models import Lead
from apps.notifications import services as notifications
from apps.notifications.models import Notification, NotificationKind
from apps.works.models import Work, WorkDocument

logger = logging.getLogger(__name__)


def crm_notifications():
    """The notifications a reset clears: all but the records of earlier resets."""
    return Notification.objects.exclude(kind=NotificationKind.CRM_RESET)


def crm_record_counts():
    """How many CRM records there are to clear."""
    return {
        'leads': Lead.objects.count(),
        'works': Work.objects.count(),
        'activities': Activity.objects.count(),
        'notifications': crm_notifications().count(),
    }


def reset_crm_data(actor):
    """Deletes every CRM record (see the module docstring) and returns how many of each were deleted."""
    with transaction.atomic():
        counts = crm_record_counts()
        # In dependency order: documents protect their Works and Works their leads (both PROTECT), so they go first;
        # activities and notifications cascade from both, and are deleted explicitly so the counts above are exact.
        # Deleting the documents through their queryset also deletes their files, once this commits.
        crm_notifications().delete()
        Activity.objects.all().delete()
        WorkDocument.objects.all().delete()
        Work.objects.all().delete()
        Lead.objects.all().delete()
        notifications.crm_reset(actor, counts)
    logger.warning('CRM data reset by %s (user %s): %s', actor.email, actor.pk, counts)
    return counts

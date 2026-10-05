from django.conf import settings
from django.db import models


class NotificationKind(models.TextChoices):
    ACTIVITY_ASSIGNED = 'ACTIVITY_ASSIGNED', 'Activity assigned'
    ACTIVITY_STATUS = 'ACTIVITY_STATUS', 'Activity status updated'
    LEAD_CREATED = 'LEAD_CREATED', 'Lead created'
    LEAD_CONVERTED = 'LEAD_CONVERTED', 'Lead converted to Work'
    WORK_COMPLETED = 'WORK_COMPLETED', 'Work completed'
    CRM_RESET = 'CRM_RESET', 'CRM data reset'


class Notification(models.Model):
    """One message for one user about something that happened in the CRM (see services.py for what sends them).
    A user reads only their own; the record it is about is linked so the screen can open it. Deleting that record
    deletes the notifications about it, so nothing points at a lead, Work or activity that is gone."""

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=30, choices=NotificationKind.choices)
    title = models.CharField(max_length=150)
    message = models.CharField(max_length=300)
    lead = models.ForeignKey('leads.Lead', on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    work = models.ForeignKey('works.Work', on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    activity = models.ForeignKey(
        'activities.Activity', on_delete=models.CASCADE, null=True, blank=True, related_name='notifications',
    )
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=NotificationKind.values), name='notifications_kind_valid',
            ),
        ]
        # The bell lists one user's newest notifications and counts their unread ones.
        indexes = [
            models.Index(fields=['recipient', '-created_at'], name='notifications_recipient_idx'),
            models.Index(fields=['recipient', 'read_at'], name='notifications_unread_idx'),
        ]

    def __str__(self):
        return f'{self.title} → {self.recipient}'

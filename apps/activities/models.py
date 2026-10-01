from django.conf import settings
from django.db import models


class ActivityType(models.TextChoices):
    PHONE_CALL = 'PHONE_CALL', 'Phone call'
    FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'
    SITE_VISIT = 'SITE_VISIT', 'Site visit'
    MEETING = 'MEETING', 'Customer meeting'
    NOTE = 'NOTE', 'Note'


class ActivityStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    COMPLETED = 'COMPLETED', 'Completed'


class Activity(models.Model):
    """Something done with a lead or a Work: a call, follow-up, visit, meeting or note. The CRM's one activity log:
    each activity belongs to exactly one lead or one Work (a Work's customer is the Work's own record)."""

    # A lead's activities are part of it, so deleting the lead (an admin action) deletes them too.
    lead = models.ForeignKey('leads.Lead', on_delete=models.CASCADE, null=True, blank=True, related_name='activities')
    # A Work's activities are its history. Works are never deleted.
    work = models.ForeignKey('works.Work', on_delete=models.CASCADE, null=True, blank=True, related_name='activities')
    type = models.CharField(max_length=20, choices=ActivityType.choices)
    description = models.TextField()
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='assigned_activities',
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=ActivityStatus.choices, default=ActivityStatus.PENDING)
    # Set when the activity is completed, cleared if it is reopened.
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='completed_activities',
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='activities')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'activities'
        constraints = [
            models.CheckConstraint(condition=models.Q(type__in=ActivityType.values), name='activities_type_valid'),
            models.CheckConstraint(condition=models.Q(status__in=ActivityStatus.values), name='activities_status_valid'),
            models.CheckConstraint(
                condition=models.Q(lead__isnull=False, work__isnull=True) | models.Q(lead__isnull=True, work__isnull=False),
                name='activities_one_lead_or_work',
            ),
            models.CheckConstraint(
                condition=models.Q(status=ActivityStatus.COMPLETED, completed_at__isnull=False)
                | models.Q(status=ActivityStatus.PENDING, completed_at__isnull=True),
                name='activities_completed_at_matches_status',
            ),
        ]
        indexes = [
            models.Index(fields=['work', 'status'], name='activities_work_status_idx'),
        ]

    def __str__(self):
        return f'{self.get_type_display()}: {self.lead or self.work}'

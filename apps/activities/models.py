from django.conf import settings
from django.db import models


class ActivityType(models.TextChoices):
    PHONE_CALL = 'PHONE_CALL', 'Phone call'
    FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'
    SITE_VISIT = 'SITE_VISIT', 'Site visit'
    MEETING = 'MEETING', 'Customer meeting'
    NOTE = 'NOTE', 'Note'


class Activity(models.Model):
    """Something done with a lead: a call, follow-up, visit, meeting or note. The CRM's one activity log: the Works
    module adds its link to a Work here rather than keeping a log of its own."""

    # A lead's activities are part of it, so deleting the lead (an admin action) deletes them too.
    lead = models.ForeignKey('leads.Lead', on_delete=models.CASCADE, related_name='activities')
    type = models.CharField(max_length=20, choices=ActivityType.choices)
    description = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='activities')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'activities'
        constraints = [
            models.CheckConstraint(condition=models.Q(type__in=ActivityType.values), name='activities_type_valid'),
        ]

    def __str__(self):
        return f'{self.get_type_display()}: {self.lead}'

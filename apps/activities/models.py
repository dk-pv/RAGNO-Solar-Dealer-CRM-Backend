from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.accounts.models import Role


class ActivityType(models.TextChoices):
    PHONE_CALL = 'PHONE_CALL', 'Phone call'
    FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'
    SITE_VISIT = 'SITE_VISIT', 'Site visit'
    MEETING = 'MEETING', 'Customer meeting'
    NOTE = 'NOTE', 'Note'


class ActivityStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    COMPLETED = 'COMPLETED', 'Completed'


class ActivityQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Every follow-up for an admin. Staff see the follow-ups on the leads assigned to them, and the follow-ups
        assigned to them on other leads (whoever has to do a follow-up can see it)."""
        if user.role_id == Role.ADMIN:
            return self
        return self.filter(Q(lead__assigned_to=user) | Q(assigned_to=user))


class Activity(models.Model):
    """A lead's follow-up: a heading, a type (call, follow-up, site visit, meeting or note), who does it, when it is
    due, and notes. Pending until someone marks it Completed. Lead activities only; Work activities belong to Works."""

    # A lead's activities are part of it, so deleting the lead (an admin action) deletes them too.
    lead = models.ForeignKey('leads.Lead', on_delete=models.CASCADE, related_name='activities')
    # Required for every new or edited follow-up (the API checks it). Follow-ups added before headings existed keep an
    # empty one rather than an invented one.
    title = models.CharField(max_length=150, blank=True, default='')
    type = models.CharField(max_length=20, choices=ActivityType.choices)
    # Who does the follow-up: separate from the lead's assigned staff, and never changes it. Required for new
    # follow-ups; empty only on follow-ups added before follow-ups had their own staff.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='assigned_activities',
    )
    # Notes: optional.
    description = models.TextField(blank=True)
    # Every new follow-up starts Pending: the API sets it, whatever the request says. Completed is final.
    status = models.CharField(max_length=10, choices=ActivityStatus.choices, default=ActivityStatus.PENDING)
    # The day the follow-up is due. Required for new follow-ups; empty only on follow-ups added before it was.
    due_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='activities')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ActivityQuerySet.as_manager()

    class Meta:
        verbose_name_plural = 'activities'
        constraints = [
            models.CheckConstraint(condition=models.Q(type__in=ActivityType.values), name='activities_type_valid'),
            models.CheckConstraint(
                condition=models.Q(status__in=ActivityStatus.values), name='activities_status_valid',
            ),
        ]
        # The Activities page filters by status and due date and sorts by those or by when it was added.
        indexes = [
            models.Index(fields=['status'], name='activities_status_idx'),
            models.Index(fields=['due_date'], name='activities_due_date_idx'),
            models.Index(fields=['created_at'], name='activities_created_at_idx'),
        ]

    def __str__(self):
        return self.title or f'{self.get_type_display()}: {self.lead}'

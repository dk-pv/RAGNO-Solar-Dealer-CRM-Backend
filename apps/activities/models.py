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
        """A lead's follow-ups a user works with: every one for an admin; for staff, those on the leads assigned to them
        and those assigned to them on other leads (whoever has to do a follow-up can see it). Work activities aren't
        among them: they follow the Work module (the activities API adds them for users who have it)."""
        follow_ups = self.filter(lead__isnull=False)
        if user.role_id == Role.ADMIN:
            return follow_ups
        return follow_ups.filter(Q(lead__assigned_to=user) | Q(assigned_to=user))


class Activity(models.Model):
    """The CRM's one activity log: each activity belongs to exactly one lead or one Work.
    A lead's follow-up has a heading, a type, who does it, when it is due and notes; it is Pending until someone marks it
    Completed, which is final. A Work's activity is its history: a type, notes, optionally who does it and when, and it
    can be completed and reopened."""

    # A lead's activities are part of it, so deleting the lead (an admin action) deletes them too.
    lead = models.ForeignKey('leads.Lead', on_delete=models.CASCADE, null=True, blank=True, related_name='activities')
    # A Work's activities are its history and go with it: an admin can delete Works in bulk from the list, and a CRM
    # reset removes them all.
    work = models.ForeignKey('works.Work', on_delete=models.CASCADE, null=True, blank=True, related_name='activities')
    # A lead's follow-up: required for every new or edited one (the API checks it). Follow-ups added before headings
    # existed, and Work activities, keep an empty one rather than an invented one.
    title = models.CharField(max_length=150, blank=True, default='')
    type = models.CharField(max_length=20, choices=ActivityType.choices)
    # Who does it: for a lead's follow-up, separate from the lead's assigned staff and required for new ones.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='assigned_activities',
    )
    # Notes: optional on a lead's follow-up, required on a Work's activity (the API checks both).
    description = models.TextField(blank=True)
    # A lead's follow-up always starts Pending (the API sets it); a Work's activity may be added already completed.
    status = models.CharField(max_length=10, choices=ActivityStatus.choices, default=ActivityStatus.PENDING)
    # The day it is due: required for a lead's new follow-ups, optional for a Work's activities.
    due_date = models.DateField(null=True, blank=True)
    # Set when the activity is completed, cleared if a Work's activity is reopened.
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

    objects = ActivityQuerySet.as_manager()

    class Meta:
        verbose_name_plural = 'activities'
        constraints = [
            models.CheckConstraint(condition=models.Q(type__in=ActivityType.values), name='activities_type_valid'),
            models.CheckConstraint(
                condition=models.Q(status__in=ActivityStatus.values), name='activities_status_valid',
            ),
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
        # The Activities pages filter by status and due date and sort by those or by when it was added; a Work's page
        # lists its activities by status.
        indexes = [
            models.Index(fields=['status'], name='activities_status_idx'),
            models.Index(fields=['due_date'], name='activities_due_date_idx'),
            models.Index(fields=['created_at'], name='activities_created_at_idx'),
            models.Index(fields=['work', 'status'], name='activities_work_status_idx'),
        ]

    def __str__(self):
        return self.title or f'{self.get_type_display()}: {self.lead or self.work}'

    def status_changeable_by(self, user):
        """Whether `user` may complete or reopen this activity, on top of being allowed to edit it at all: an admin, the
        staff member it is assigned to, or (for an activity assigned to no one) anyone who can edit it. Another staff
        member can see it but never changes its status."""
        return user.role_id == Role.ADMIN or self.assigned_to_id is None or self.assigned_to_id == user.pk

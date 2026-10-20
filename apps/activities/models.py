from django.conf import settings
from django.db import models

from apps.accounts.models import LegacyAssigneeMixin, Role


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
        """The activities a user sees: every one for an admin; for staff only those assigned to them (alone or with
        others), on any lead or Work. Never one that isn't assigned to them, whoever the lead or Work belongs to. One row
        per activity: the join matches at most one of its assignees."""
        return self if user.role_id == Role.ADMIN else self.filter(assignees=user)


class Activity(LegacyAssigneeMixin, models.Model):
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
    # Who does it, one person or several (see ActivityAssignee): for a lead's follow-up, separate from the lead's
    # assigned staff and at least one for new ones.
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='ActivityAssignee', blank=True, related_name='assigned_activities',
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
    # What was done, as whoever completed it noted it: optional, and cleared if a Work's activity is reopened. The
    # task's own notes stay in description.
    completion_note = models.TextField(blank=True)
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
        """Whether `user` may open and complete this activity: an admin, or a staff member it is assigned to (any of its
        assignees: completing it completes it for all of them). Never another staff member, and no staff member for an
        activity assigned to no one (it can't be taken over first: editing and reassigning are for admins). Reads the
        prefetched assignees when there are some."""
        return user.role_id == Role.ADMIN or any(assignee.pk == user.pk for assignee in self.assignees.all())


class ActivityAssignee(models.Model):
    """One of the people an activity is assigned to. PROTECT, as the one assignee was: someone with assigned activities
    is deactivated, never deleted (see the Users API)."""

    activity = models.ForeignKey(Activity, on_delete=models.CASCADE, related_name='+')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['activity', 'user'], name='activities_assignee_once_per_activity')]

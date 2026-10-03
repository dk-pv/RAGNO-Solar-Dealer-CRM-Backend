import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F

from ._operations import AddConstraintIfMissing, AddFieldIfMissing, AddIndexIfMissing


def date_completed_follow_ups(apps, schema_editor):
    """Follow-ups completed before completion times were recorded get their last update as that time, so every
    completed activity has one (the constraint below). Who completed them wasn't recorded, so it stays empty."""
    Activity = apps.get_model('activities', 'Activity')
    Activity.objects.filter(status='COMPLETED', completed_at__isnull=True).update(completed_at=F('updated_at'))


class Migration(migrations.Migration):
    """Work activities in the one activity log: an activity belongs to a lead or a Work, and records when and by whom it
    was completed. Follows the lead follow-up migrations (0002, 0003), which already added the status, due date and
    assigned staff."""

    dependencies = [
        ('activities', '0003_follow_up_heading_and_staff'),
        ('works', '0002_work_pin'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    # Adds only what the database doesn't have yet: a database migrated on the Work activities branch before it was
    # combined with this one already has some of these (see _operations.py).
    operations = [
        AddFieldIfMissing(
            model_name='activity',
            name='work',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='activities', to='works.work'),
        ),
        AddFieldIfMissing(
            model_name='activity',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        AddFieldIfMissing(
            model_name='activity',
            name='completed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='completed_activities', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='activity',
            name='lead',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='activities', to='leads.lead'),
        ),
        AddIndexIfMissing(
            model_name='activity',
            index=models.Index(fields=['work', 'status'], name='activities_work_status_idx'),
        ),
        migrations.RunPython(date_completed_follow_ups, migrations.RunPython.noop),
        AddConstraintIfMissing(
            model_name='activity',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('lead__isnull', False), ('work__isnull', True)), models.Q(('lead__isnull', True), ('work__isnull', False)), _connector='OR'), name='activities_one_lead_or_work'),
        ),
        AddConstraintIfMissing(
            model_name='activity',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('completed_at__isnull', False), ('status', 'COMPLETED')), models.Q(('completed_at__isnull', True), ('status', 'PENDING')), _connector='OR'), name='activities_completed_at_matches_status'),
        ),
    ]

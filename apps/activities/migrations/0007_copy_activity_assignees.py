from django.db import migrations


def copy_assignees(apps, schema_editor):
    """Each activity's one assigned_to becomes its first assignee. Activities assigned to no one stay unassigned."""
    Activity = apps.get_model('activities', 'Activity')
    ActivityAssignee = apps.get_model('activities', 'ActivityAssignee')
    ActivityAssignee.objects.bulk_create(
        ActivityAssignee(activity_id=activity_id, user_id=user_id)
        for activity_id, user_id in Activity.objects.exclude(assigned_to=None).values_list('pk', 'assigned_to')
    )


def restore_assigned_to(apps, schema_editor):
    """Back to one assignee per activity (migrating back to 0006): the first by name, the one the old API field showed.
    An activity's other assignees are dropped with the assignees table when 0006 is reversed."""
    Activity = apps.get_model('activities', 'Activity')
    ActivityAssignee = apps.get_model('activities', 'ActivityAssignee')
    first = {}
    rows = ActivityAssignee.objects.order_by('activity', 'user__name', 'user').values_list('activity', 'user')
    for activity_id, user_id in rows:
        first.setdefault(activity_id, user_id)
    for activity_id, user_id in first.items():
        Activity.objects.filter(pk=activity_id).update(assigned_to_id=user_id)


class Migration(migrations.Migration):
    dependencies = [
        ('activities', '0006_activity_assignees'),
    ]

    operations = [
        migrations.RunPython(copy_assignees, restore_assigned_to),
    ]

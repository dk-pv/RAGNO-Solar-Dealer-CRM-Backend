from django.db import migrations


class Migration(migrations.Migration):
    """The one assigned_to, now copied into the assignees (0007). Reversible: migrating back re-adds it and 0007 fills
    it."""

    dependencies = [
        ('activities', '0007_copy_activity_assignees'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='activity',
            name='assigned_to',
        ),
    ]

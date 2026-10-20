from django.db import migrations


class Migration(migrations.Migration):
    """The one assigned_to, now copied into the assignees (0004). Reversible: migrating back re-adds it and 0004 fills it."""

    dependencies = [
        ('leads', '0004_copy_lead_assignees'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='lead',
            name='assigned_to',
        ),
    ]

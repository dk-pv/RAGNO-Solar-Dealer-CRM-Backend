import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """An activity can be assigned to several people (ActivityAssignee). Nothing is lost: the one assigned_to stays until
    0007 has copied it into the assignees, and 0008 removes it after that."""

    dependencies = [
        ('activities', '0005_activity_completion_note'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Hands its related name to the assignees below. No database change.
        migrations.AlterField(
            model_name='activity',
            name='assigned_to',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name='ActivityAssignee',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('activity', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='+', to='activities.activity',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(fields=('activity', 'user'), name='activities_assignee_once_per_activity'),
                ],
            },
        ),
        migrations.AddField(
            model_name='activity',
            name='assignees',
            field=models.ManyToManyField(
                blank=True, related_name='assigned_activities', through='activities.ActivityAssignee',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

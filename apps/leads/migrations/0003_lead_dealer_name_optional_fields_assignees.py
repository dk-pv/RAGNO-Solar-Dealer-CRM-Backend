import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Leads get a dealer name; their district, plan and amount become optional; and a lead can be assigned to several
    people (LeadAssignee). Nothing is lost: the one assigned_to stays until 0004 has copied it into the assignees, and
    0005 removes it after that. Existing rows keep every value they have."""

    dependencies = [
        ('leads', '0002_default_solar_plans'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='dealer_name',
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AlterField(
            model_name='lead',
            name='district',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AlterField(
            model_name='lead',
            name='plan',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='leads',
                to='leads.solarplan',
            ),
        ),
        migrations.AlterField(
            model_name='lead',
            name='amount',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True,
                validators=[django.core.validators.MinValueValidator(0)],
            ),
        ),
        # Hands its related name to the assignees below. No database change.
        migrations.AlterField(
            model_name='lead',
            name='assigned_to',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name='LeadAssignee',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('lead', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='+', to='leads.lead',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('lead', 'user'), name='leads_assignee_once_per_lead')],
            },
        ),
        migrations.AddField(
            model_name='lead',
            name='assignees',
            field=models.ManyToManyField(
                blank=True, related_name='assigned_leads', through='leads.LeadAssignee', to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

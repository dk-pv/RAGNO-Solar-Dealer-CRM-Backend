from decimal import Decimal

from django.db import migrations

# The 4 kW plan. Like 0002, this only creates it: a plan that already exists keeps its price, so an admin's change to it
# is never overwritten.


def create_plan(apps, schema_editor):
    SolarPlan = apps.get_model('leads', 'SolarPlan')
    SolarPlan.objects.get_or_create(capacity=Decimal(4), defaults={'name': '4 kW', 'amount': Decimal(180000)})


class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0005_remove_lead_assigned_to'),
    ]

    operations = [
        # Reversing leaves the plan in place: leads refer to it.
        migrations.RunPython(create_plan, migrations.RunPython.noop),
    ]

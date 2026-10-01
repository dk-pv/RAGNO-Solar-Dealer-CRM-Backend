from decimal import Decimal

from django.db import migrations

# The four plans the business sells, at their prices when the CRM went live. From here on the database is the source
# of truth: admins change prices there, and a plan that already exists is never touched by this migration.
PLANS = [('3 kW', 3, 150000), ('5 kW', 5, 200000), ('8 kW', 8, 245000), ('10 kW', 10, 289000)]


def create_plans(apps, schema_editor):
    SolarPlan = apps.get_model('leads', 'SolarPlan')
    for name, capacity, amount in PLANS:
        SolarPlan.objects.get_or_create(capacity=Decimal(capacity), defaults={'name': name, 'amount': Decimal(amount)})


class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0001_initial'),
    ]

    operations = [
        # Reversing leaves the plans in place: leads refer to them.
        migrations.RunPython(create_plans, migrations.RunPython.noop),
    ]

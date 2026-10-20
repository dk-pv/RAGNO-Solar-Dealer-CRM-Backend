from django.db import migrations


def copy_assignees(apps, schema_editor):
    """Each lead's one assigned_to becomes its first assignee. Leads assigned to no one stay unassigned."""
    Lead = apps.get_model('leads', 'Lead')
    LeadAssignee = apps.get_model('leads', 'LeadAssignee')
    LeadAssignee.objects.bulk_create(
        LeadAssignee(lead_id=lead_id, user_id=user_id)
        for lead_id, user_id in Lead.objects.exclude(assigned_to=None).values_list('pk', 'assigned_to')
    )


def restore_assigned_to(apps, schema_editor):
    """Back to one assignee per lead (migrating back to 0003): the first by name, the one the old API field showed. A
    lead's other assignees are dropped with the assignees table when 0003 is reversed."""
    Lead = apps.get_model('leads', 'Lead')
    LeadAssignee = apps.get_model('leads', 'LeadAssignee')
    first = {}
    for lead_id, user_id in LeadAssignee.objects.order_by('lead', 'user__name', 'user').values_list('lead', 'user'):
        first.setdefault(lead_id, user_id)
    for lead_id, user_id in first.items():
        Lead.objects.filter(pk=lead_id).update(assigned_to_id=user_id)


class Migration(migrations.Migration):
    dependencies = [
        ('leads', '0003_lead_dealer_name_optional_fields_assignees'),
    ]

    operations = [
        migrations.RunPython(copy_assignees, restore_assigned_to),
    ]

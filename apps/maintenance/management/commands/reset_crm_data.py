"""Clears the CRM's business data (leads, Works, activities, notifications) and keeps users, roles, departments and plans.

    python manage.py reset_crm_data --yes --by admin@example.com

The same operation as Settings → Data → Reset CRM data (apps.maintenance.reset). It refuses to run without --yes and
--by: the reset is recorded against the admin named by --by, in the log and in a notification to every admin, so the
record never blames someone who didn't run it.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role
from apps.maintenance.reset import crm_record_counts, reset_crm_data

User = get_user_model()


class Command(BaseCommand):
    help = 'Deletes every lead, Work, activity and notification. Users, roles, departments and plans are kept.'

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='Confirm the deletion (without it, only the counts are shown).')
        parser.add_argument('--by', help='The email of the active admin running this; the reset is recorded against them (required with --yes).')

    def handle(self, *args, **options):
        counts = crm_record_counts()
        summary = ', '.join(f'{count} {name}' for name, count in counts.items())
        if not options['yes']:
            self.stdout.write(f'Would delete {summary}. Run again with --yes --by <your admin email> to delete them.')
            return
        if not options['by']:
            raise CommandError('Pass --by with your admin email: the reset is recorded against whoever ran it.')
        admins = User.objects.filter(is_active=True, role_id=Role.ADMIN)
        actor = admins.filter(email=User.objects.normalize_email(options['by'])).first()
        if actor is None:
            raise CommandError(f"{options['by']} is not an active admin. Pass --by with an active admin's email.")
        deleted = reset_crm_data(actor)
        self.stdout.write(self.style.SUCCESS(
            'Deleted ' + ', '.join(f'{count} {name}' for name, count in deleted.items()) + f'. Recorded against {actor.email}.'
        ))

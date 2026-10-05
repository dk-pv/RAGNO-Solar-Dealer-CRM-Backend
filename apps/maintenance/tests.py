import io
from datetime import date

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, User
from apps.activities.models import Activity
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.notifications.models import Notification, NotificationKind
from apps.works.models import Work

PASSWORD = 'Solar-Panel-2026'


class CrmResetTests(APITestCase):
    """The CRM's data goes; the people, their access and the configuration stay."""

    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(name='Sales')
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff', department=cls.department)
        role = Role.objects.get(name=Role.STAFF)
        role.permissions.add(*Permission.objects.filter(content_type__app_label='leads', codename__in=['view_lead', 'change_lead']))
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        lead = {'district': 'Ernakulam', 'plan': self.plan, 'amount': self.plan.amount, 'created_by': self.admin}
        open_lead = Lead.objects.create(name='Open Lead', phone='9876500001', assigned_to=self.staff, **lead)
        won = Lead.objects.create(name='Won Lead', phone='9876500002', status=LeadStatus.WON, **lead)
        won.convert(self.admin)
        Activity.objects.create(lead=open_lead, title='Call', type='PHONE_CALL', assigned_to=self.staff, due_date=date(2026, 10, 10), created_by=self.admin)
        Activity.objects.create(work=won.work, type='NOTE', description='Panels delivered.', created_by=self.admin)
        Notification.objects.create(recipient=self.staff, kind=NotificationKind.ACTIVITY_ASSIGNED, title='Call', message='x', lead=open_lead)
        self.url = reverse('maintenance-reset-crm-data')

    def assert_everything_but_crm_data_is_kept(self):
        self.assertEqual((Lead.objects.count(), Work.objects.count(), Activity.objects.count()), (0, 0, 0))
        self.assertEqual(set(User.objects.values_list('email', flat=True)), {'admin@example.com', 'staff@example.com'})
        self.assertTrue(User.objects.get(email='admin@example.com').check_password(PASSWORD))
        self.assertTrue(User.objects.get(email='staff@example.com').check_password(PASSWORD))
        self.assertEqual(set(Role.objects.values_list('name', flat=True)), {'ADMIN', 'STAFF'})
        self.assertEqual(Role.objects.get(name=Role.STAFF).permissions.count(), 2)
        self.assertEqual(Department.objects.get().name, 'Sales')
        self.assertEqual(SolarPlan.objects.count(), 4)
        # The one notification left is the record of the reset itself, to every admin.
        [notice] = Notification.objects.all()
        self.assertEqual((notice.recipient, notice.kind, notice.title), (self.admin, NotificationKind.CRM_RESET, 'CRM data reset'))
        self.assertIn('2 leads, 1 works and 2 activities were removed', notice.message)

    def test_an_admin_sees_the_counts_and_resets_with_the_confirmation_phrase(self):
        self.client.force_authenticate(self.admin)
        preview = self.client.get(self.url)
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data, {
            'counts': {'leads': 2, 'works': 1, 'activities': 2, 'notifications': 1}, 'confirmation': 'RESET CRM DATA',
        })

        for bad in ({}, {'confirm': ''}, {'confirm': 'reset crm data'}, {'confirm': 'DELETE'}):
            self.assertEqual(self.client.post(self.url, bad, format='json').status_code, status.HTTP_400_BAD_REQUEST, bad)
        self.assertEqual(Lead.objects.count(), 2)

        done = self.client.post(self.url, {'confirm': 'RESET CRM DATA'}, format='json')
        self.assertEqual(done.status_code, status.HTTP_200_OK, done.data)
        self.assertEqual(done.data, {'deleted': {'leads': 2, 'works': 1, 'activities': 2, 'notifications': 1}})
        self.assert_everything_but_crm_data_is_kept()
        # A second reset keeps the record of the first: the audit trail survives resets.
        again = self.client.post(self.url, {'confirm': 'RESET CRM DATA'}, format='json')
        self.assertEqual(again.data, {'deleted': {'leads': 0, 'works': 0, 'activities': 0, 'notifications': 0}})
        self.assertEqual(Notification.objects.filter(kind=NotificationKind.CRM_RESET, recipient=self.admin).count(), 2)
        # The admin still signs in afterwards.
        self.client.force_authenticate(None)
        login = self.client.post(reverse('auth-login'), {'email': 'admin@example.com', 'password': PASSWORD}, format='json')
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {login.data["access"]}')
        self.assertEqual(self.client.get(reverse('lead-list')).data['count'], 0)

    def test_staff_and_signed_out_requests_are_refused_and_delete_nothing(self):
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(self.url, {'confirm': 'RESET CRM DATA'}, format='json').status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.url, {'confirm': 'RESET CRM DATA'}, format='json').status_code, 401)
        self.assertEqual((Lead.objects.count(), Work.objects.count(), Activity.objects.count(), Notification.objects.count()), (2, 1, 2, 1))

    def test_the_management_command_needs_yes_and_records_the_admin(self):
        output = io.StringIO()
        call_command('reset_crm_data', stdout=output)
        self.assertIn('Would delete 2 leads, 1 works, 2 activities, 1 notifications', output.getvalue())
        self.assertEqual(Lead.objects.count(), 2)

        with self.assertRaisesMessage(CommandError, 'active admin'):
            call_command('reset_crm_data', '--yes', '--by', 'staff@example.com')
        # Without --by the reset would have to be blamed on someone: it refuses instead.
        with self.assertRaisesMessage(CommandError, 'Pass --by'):
            call_command('reset_crm_data', '--yes')
        self.assertEqual(Lead.objects.count(), 2)

        output = io.StringIO()
        call_command('reset_crm_data', '--yes', '--by', 'Admin@Example.com', stdout=output)
        self.assertIn('Deleted 2 leads, 1 works, 2 activities, 1 notifications. Recorded against admin@example.com.', output.getvalue())
        self.assert_everything_but_crm_data_is_kept()

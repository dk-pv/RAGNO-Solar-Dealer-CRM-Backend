"""Deleting one Work, as the All Works row menu does: through the bulk endpoint, by admins only, with its activities,
documents (their files once that commits) and notifications, and never its lead or anything else."""

from unittest import mock

from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity, ActivityAssignee, ActivityStatus
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.notifications.models import Notification, NotificationKind

from . import storage
from .models import Work, WorkDocument
from .test_documents import add_document
from .tests import PASSWORD, convert_lead

GONE = "This record no longer exists, or you don't have access to it."


class WorkDeletionTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Priya Nair')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.work = convert_lead(self.plan, self.admin, name='Suresh P', assigned_to=self.staff)
        self.other = convert_lead(self.plan, self.admin, name='Anu Joseph', phone='9876500002')
        patcher = mock.patch.object(storage, 'delete', autospec=True)
        self.addCleanup(patcher.stop)
        self.storage_delete = patcher.start()

    def delete(self, work):
        return self.client.post(reverse('work-bulk-delete'), {'ids': [work.pk]}, format='json')

    def test_only_admins_can_delete_a_work_even_with_the_work_module(self):
        Role.objects.get(pk=Role.STAFF).permissions.add(Permission.objects.get(codename='access_work'))
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.delete(self.work).status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(None)
        self.assertEqual(self.delete(self.work).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(Work.objects.filter(pk=self.work.pk).exists())

        self.client.force_authenticate(self.admin)
        response = self.delete(self.work)
        self.assertEqual((response.status_code, response.data), (200, {'succeeded': [self.work.pk], 'failed': []}))
        # Still no single-record DELETE: the menu goes through the bulk endpoint's rules.
        self.assertEqual(self.client.delete(reverse('work-detail', args=[self.other.pk])).status_code, 405)

    def test_its_activities_documents_and_notifications_go_with_it_and_its_lead_stays_won(self):
        follow_up = Activity.objects.create(work=self.work, type='SITE_VISIT', description='Roof survey.', created_by=self.admin)
        follow_up.assignees.set([self.staff, self.admin])
        Activity.objects.create(work=self.work, type='NOTE', description='Done.', status=ActivityStatus.COMPLETED,
                                completed_at=follow_up.created_at, completed_by=self.admin, created_by=self.admin)
        kept_activity = Activity.objects.create(work=self.other, type='NOTE', description='Other.', created_by=self.admin)
        removed = add_document(self.work, 'pan_card', self.admin)
        kept_document = add_document(self.other, 'pan_card', self.admin)
        Notification.objects.create(recipient=self.staff, kind=NotificationKind.WORK_COMPLETED, title='Done', message='x', work=self.work)
        Notification.objects.create(recipient=self.staff, kind=NotificationKind.WORK_COMPLETED, title='Done', message='y', work=self.other)
        leads = Lead.objects.count()
        self.client.force_authenticate(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.delete(self.work)
            self.storage_delete.assert_not_called()  # files only once the deletion has committed

        self.assertEqual(response.data['succeeded'], [self.work.pk])
        self.assertFalse(Work.objects.filter(pk=self.work.pk).exists())
        self.assertEqual(list(Activity.objects.all()), [kept_activity])
        self.assertFalse(ActivityAssignee.objects.filter(activity_id=follow_up.pk).exists())
        self.assertEqual(list(WorkDocument.objects.all()), [kept_document])
        self.storage_delete.assert_called_once_with([('raw', removed.cloudinary_public_id)])
        self.assertEqual(list(Notification.objects.values_list('message', flat=True)), ['y'])
        # The lead, the users and every other record stay; the lead is still Won and can be converted again.
        self.assertEqual(Lead.objects.count(), leads)
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())
        lead = self.client.get(reverse('lead-detail', args=[self.work.lead_id])).data
        self.assertEqual((lead['status'], lead['work'], lead['can_convert']), (LeadStatus.WON, None, True))
        self.assertEqual(self.client.get(reverse('work-list')).data['count'], 1)
        self.assertEqual(self.client.post(reverse('lead-convert', args=[self.work.lead_id])).status_code, 200)

    def test_a_work_already_deleted_is_reported_rather_than_deleted_twice(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.delete(self.work).data['succeeded'], [self.work.pk])

        again = self.delete(self.work)

        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual((again.data['succeeded'], again.data['failed']), ([], [{'id': self.work.pk, 'name': None, 'reason': GONE}]))
        self.assertTrue(Work.objects.filter(pk=self.other.pk).exists())

    def test_who_deleted_a_work_goes_to_the_server_log(self):
        self.client.force_authenticate(self.admin)

        with self.assertLogs('apps.works.views', 'WARNING') as logs, self.captureOnCommitCallbacks(execute=True):
            self.delete(self.work)

        self.assertIn(f'Works [{self.work.pk}] deleted by user {self.admin.pk}', logs.output[0])

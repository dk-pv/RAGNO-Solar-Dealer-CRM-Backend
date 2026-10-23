"""Deleting an activity (DELETE /api/activities/{id}/): admins only, a lead's follow-up and a Work's activity alike, and
for good. Nothing else goes with it: not its lead or Work, not their other activities, not other notifications."""

from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.notifications.models import Notification, NotificationKind
from apps.works.models import Work

from .models import Activity, ActivityType

PASSWORD = 'Solar-Panel-2026'


class ActivityDeleteTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')
        # Every module a staff role could ever have held, and both activities assigned to them: still not theirs to delete.
        Role.objects.get(pk=Role.STAFF).permissions.add(*Permission.objects.filter(
            codename__in=['access_activities', 'access_work', 'view_lead', 'add_lead', 'change_lead'],
        ))
        plan = SolarPlan.objects.get(capacity=5)
        cls.lead = Lead.objects.create(
            name='Asha Menon', phone='9876543210', plan=plan, amount=plan.amount, created_by=cls.admin,
            assigned_to=cls.staff,
        )
        won = Lead.objects.create(
            name='Ravi Kumar', phone='9876500001', plan=plan, amount=plan.amount, created_by=cls.admin,
            status=LeadStatus.SUPERHOT,
        )
        won.move_to(LeadStatus.WON)
        won.convert(cls.admin)
        cls.work = won.work
        follow_up = {'lead': cls.lead, 'type': ActivityType.PHONE_CALL, 'due_date': '2026-10-30', 'created_by': cls.admin}
        cls.on_lead = Activity.objects.create(title='Call about the quotation', assigned_to=cls.staff, **follow_up)
        cls.lead_sibling = Activity.objects.create(title='Send the brochure', assigned_to=cls.staff, **follow_up)
        work_activity = {'work': cls.work, 'created_by': cls.admin, 'assigned_to': cls.staff}
        cls.on_work = Activity.objects.create(type=ActivityType.SITE_VISIT, description='Roof survey.', **work_activity)
        cls.work_sibling = Activity.objects.create(type=ActivityType.NOTE, description='Panels ordered.', **work_activity)
        for activity in (cls.on_lead, cls.lead_sibling, cls.on_work, cls.work_sibling):
            Notification.objects.create(
                recipient=cls.staff, kind=NotificationKind.ACTIVITY_ASSIGNED, title='New activity assigned to you',
                message='...', lead=activity.lead, work=activity.work, activity=activity,
            )
        Notification.objects.create(
            recipient=cls.admin, kind=NotificationKind.LEAD_CREATED, title='New lead created', message='...', lead=cls.lead,
        )

    def delete(self, activity_id):
        return self.client.delete(reverse('activity-detail', args=[activity_id]))

    def remaining(self):
        return set(Activity.objects.values_list('pk', flat=True))

    def rows(self):
        """Both Activities pages' rows, as the signed-in user sees them."""
        pages = [self.client.get(reverse(name)).data['results'] for name in ('activity-list', 'activity-works')]
        return [row for page in pages for row in page]

    def test_an_admin_deletes_a_lead_follow_up_for_good_and_nothing_else(self):
        self.client.force_authenticate(self.admin)

        with self.assertLogs('apps.activities.views', 'WARNING') as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.delete(self.on_lead.pk)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.remaining(), {self.lead_sibling.pk, self.on_work.pk, self.work_sibling.pk})
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk, status=LeadStatus.NEW).exists())
        # The notifications about it go with it (they would point at nothing); every other one stays.
        self.assertFalse(Notification.objects.filter(activity_id=self.on_lead.pk).exists())
        self.assertEqual(Notification.objects.count(), 4)
        self.assertIn(f'Activity {self.on_lead.pk} (lead {self.lead.pk}) deleted by user {self.admin.pk}', logs.output[0])
        # Gone for good: a second delete finds nothing, and neither do the lead's lists.
        self.assertEqual(self.delete(self.on_lead.pk).status_code, status.HTTP_404_NOT_FOUND)
        listed = self.client.get(reverse('activity-list'), {'lead': self.lead.pk}).data
        self.assertEqual((listed['count'], [row['title'] for row in listed['results']]), (1, ['Send the brochure']))

    def test_an_admin_deletes_a_work_activity_for_good_and_nothing_else(self):
        self.client.force_authenticate(self.admin)

        response = self.delete(self.on_work.pk)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.remaining(), {self.on_lead.pk, self.lead_sibling.pk, self.work_sibling.pk})
        work = Work.objects.get(pk=self.work.pk)
        self.assertEqual((work.lead.status, work.amount), (LeadStatus.WON, self.work.amount))
        self.assertEqual(Notification.objects.filter(work=work).count(), 1)
        # The Work's own counts and the Work Activities page see one activity fewer.
        self.assertEqual(self.client.get(reverse('work-detail', args=[work.pk])).data['activity_count'], 1)
        page = self.client.get(reverse('activity-works')).data
        self.assertEqual((page['count'], [row['description'] for row in page['results']]), (1, ['Panels ordered.']))

    def test_staff_cant_delete_either_kind_even_their_own(self):
        self.client.force_authenticate(self.staff)

        for activity in (self.on_lead, self.on_work):
            with self.subTest(activity=str(activity)):
                self.assertEqual(self.delete(activity.pk).status_code, status.HTTP_403_FORBIDDEN)
        # Nor one that doesn't exist: they learn nothing about which ids do.
        self.assertEqual(self.delete(999999).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(len(self.remaining()), 4)
        self.assertEqual(Notification.objects.count(), 5)
        # Their rows don't offer it.
        rows = self.rows()
        self.assertEqual((len(rows), {row['can_delete'] for row in rows}), (4, {False}))

    def test_signed_out_users_cant_delete_activities(self):
        for activity in (self.on_lead, self.on_work):
            with self.subTest(activity=str(activity)):
                self.assertEqual(self.delete(activity.pk).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(len(self.remaining()), 4)

    def test_unknown_and_malformed_ids_find_nothing(self):
        self.client.force_authenticate(self.admin)

        self.assertEqual(self.delete(999999).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete('/api/activities/abc/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete('/api/activities/-1/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(len(self.remaining()), 4)

    def test_an_admins_rows_offer_delete_on_both_pages(self):
        self.client.force_authenticate(self.admin)

        rows = self.rows()

        self.assertEqual((len(rows), {row['can_delete'] for row in rows}), (4, {True}))

from datetime import date

from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.works.models import WorkStage

from .models import Notification, NotificationKind

PASSWORD = 'Solar-Panel-2026'


def grant(*perms):
    role = Role.objects.get(name=Role.STAFF)
    for perm in perms:
        app_label, codename = perm.split('.')
        role.permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))


class NotificationTestCase(APITestCase):
    """Two admins and two staff members with the Leads, Work and Activities modules."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.admin_2 = User.objects.create_user(email='admin2@example.com', password=PASSWORD, name='Second Admin', role=Role.ADMIN)
        cls.staff_a = User.objects.create_user(email='a@example.com', password=PASSWORD, name='Staff A')
        cls.staff_b = User.objects.create_user(email='b@example.com', password=PASSWORD, name='Staff B')
        grant('leads.view_lead', 'leads.add_lead', 'leads.change_lead', 'accounts.access_work', 'accounts.access_activities')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def make_lead(self, **fields):
        values = {'name': 'Asha Menon', 'phone': '9876543210', 'district': 'Ernakulam', 'plan': self.plan,
                  'amount': self.plan.amount, 'created_by': self.admin, **fields}
        return Lead.objects.create(**values)

    def received(self, user, kind=None):
        notifications = Notification.objects.filter(recipient=user).order_by('id')
        if kind:
            notifications = notifications.filter(kind=kind)
        return list(notifications)


class NotificationEventTests(NotificationTestCase):
    def test_a_new_lead_tells_the_other_admins_and_its_initial_follow_ups_assignee(self):
        self.as_user(self.admin)
        body = {
            'name': 'Danish PV', 'phone': '9876500001', 'district': 'Thrissur', 'plan': self.plan.pk,
            'initial_follow_up': {'title': 'Follow up with Danish PV', 'type': 'PHONE_CALL', 'assigned_to': self.staff_a.pk,
                                  'due_date': '2026-10-10', 'description': ''},
        }
        created = self.client.post(reverse('lead-list'), body, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        # The admin who added it isn't told; the other admin is.
        self.assertEqual(self.received(self.admin), [])
        [notice] = self.received(self.admin_2)
        self.assertEqual((notice.kind, notice.title, notice.lead_id, notice.read_at), (
            NotificationKind.LEAD_CREATED, 'New lead created', created.data['id'], None,
        ))
        self.assertEqual(notice.message, 'Danish PV was added as a new lead by Admin.')
        [assigned] = self.received(self.staff_a)
        self.assertEqual((assigned.kind, assigned.title), (NotificationKind.ACTIVITY_ASSIGNED, 'New activity assigned to you'))
        self.assertEqual(assigned.message, 'Follow up with Danish PV for Danish PV is assigned to you.')
        self.assertEqual((assigned.lead_id, assigned.activity_id), (created.data['id'], Activity.objects.get().pk))
        self.assertEqual(self.received(self.staff_b), [])

    def test_a_lead_added_by_staff_tells_every_admin(self):
        self.as_user(self.staff_a)
        body = {'name': 'Danish PV', 'phone': '9876500001', 'district': 'Thrissur', 'plan': self.plan.pk}
        self.assertEqual(self.client.post(reverse('lead-list'), body, format='json').status_code, 201)
        self.assertEqual(len(self.received(self.admin, NotificationKind.LEAD_CREATED)), 1)
        self.assertEqual(len(self.received(self.admin_2, NotificationKind.LEAD_CREATED)), 1)
        self.assertEqual(self.received(self.staff_a), [])

    def test_assigning_an_activity_tells_the_assignee_once_and_reassigning_tells_the_new_one(self):
        lead = self.make_lead(assigned_to=self.staff_a)
        self.as_user(self.admin)
        body = {'lead': lead.pk, 'title': 'Call about the quote', 'type': 'PHONE_CALL', 'assigned_to': self.staff_a.pk,
                'due_date': '2026-10-10'}
        created = self.client.post(reverse('activity-list'), body, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        url = reverse('activity-detail', args=[created.data['id']])
        self.assertEqual(len(self.received(self.staff_a, NotificationKind.ACTIVITY_ASSIGNED)), 1)

        # An edit that keeps the assignee sends nothing again; one that changes it tells the new person only.
        self.assertEqual(self.client.patch(url, {'title': 'Call about the quotation', 'assigned_to': self.staff_a.pk}, format='json').status_code, 200)
        self.assertEqual(len(self.received(self.staff_a, NotificationKind.ACTIVITY_ASSIGNED)), 1)
        self.assertEqual(self.client.patch(url, {'assigned_to': self.staff_b.pk}, format='json').status_code, 200)
        self.assertEqual(len(self.received(self.staff_a, NotificationKind.ACTIVITY_ASSIGNED)), 1)
        [to_b] = self.received(self.staff_b)
        self.assertEqual(to_b.message, 'Call about the quotation for Asha Menon is assigned to you.')
        # An admin assigned an activity is told too; one who assigns it to themselves is not.
        self.assertEqual(self.client.patch(url, {'assigned_to': self.admin_2.pk}, format='json').status_code, 200)
        self.assertEqual(len(self.received(self.admin_2, NotificationKind.ACTIVITY_ASSIGNED)), 1)
        self.assertEqual(self.client.patch(url, {'assigned_to': self.admin.pk}, format='json').status_code, 200)
        self.assertEqual(self.received(self.admin, NotificationKind.ACTIVITY_ASSIGNED), [])

    def test_completing_an_activity_tells_the_admins_and_the_assignee_but_not_the_one_who_did_it(self):
        lead = self.make_lead(assigned_to=self.staff_a)
        activity = Activity.objects.create(
            lead=lead, title='Site visit', type='SITE_VISIT', assigned_to=self.staff_a, due_date=date(2026, 10, 10),
            created_by=self.admin,
        )
        self.as_user(self.staff_a)
        done = self.client.patch(reverse('activity-detail', args=[activity.pk]), {'status': 'COMPLETED'}, format='json')
        self.assertEqual(done.status_code, status.HTTP_200_OK, done.data)
        for admin in (self.admin, self.admin_2):
            [notice] = self.received(admin)
            self.assertEqual((notice.kind, notice.title), (NotificationKind.ACTIVITY_STATUS, 'Activity completed'))
            self.assertEqual(notice.message, 'Staff A marked Site visit for Asha Menon as completed.')
            self.assertEqual((notice.lead_id, notice.activity_id), (lead.pk, activity.pk))
        self.assertEqual(self.received(self.staff_a), [])

        # An admin completing a staff member's activity tells that staff member.
        other = Activity.objects.create(
            lead=lead, title='Send brochure', type='NOTE', assigned_to=self.staff_a, due_date=date(2026, 10, 11),
            created_by=self.admin,
        )
        self.as_user(self.admin)
        self.assertEqual(self.client.patch(reverse('activity-detail', args=[other.pk]), {'status': 'COMPLETED'}, format='json').status_code, 200)
        [told] = self.received(self.staff_a)
        self.assertEqual(told.message, 'Admin marked Send brochure for Asha Menon as completed.')
        self.assertEqual(len(self.received(self.admin_2)), 2)
        self.assertEqual(len(self.received(self.admin)), 1)  # only the first one, by Staff A

    def test_converting_a_lead_and_completing_its_work_tell_the_admins(self):
        lead = self.make_lead(status=LeadStatus.WON, assigned_to=self.staff_a)
        self.as_user(self.staff_a)
        converted = self.client.post(reverse('lead-convert', args=[lead.pk]))
        self.assertEqual(converted.status_code, status.HTTP_200_OK, converted.data)
        work_id = converted.data['work']
        for admin in (self.admin, self.admin_2):
            [notice] = self.received(admin)
            self.assertEqual((notice.kind, notice.title, notice.lead_id, notice.work_id), (
                NotificationKind.LEAD_CONVERTED, 'Lead converted to Work', lead.pk, work_id,
            ))
            self.assertEqual(notice.message, f'Asha Menon was converted to Work #{work_id} by Staff A.')

        url = reverse('work-detail', args=[work_id])
        self.assertEqual(self.client.patch(url, {'stage': WorkStage.FEASIBILITY}, format='json').status_code, 200)
        self.assertEqual(len(self.received(self.admin)), 1)  # a stage change short of Completed tells no one
        self.assertEqual(self.client.patch(url, {'stage': WorkStage.COMPLETED}, format='json').status_code, 200)
        self.assertEqual(self.client.patch(url, {'due_date': '2026-12-01'}, format='json').status_code, 200)  # still Completed
        for admin in (self.admin, self.admin_2):
            [converted_notice, completed] = self.received(admin)
            self.assertEqual((completed.kind, completed.title, completed.work_id), (NotificationKind.WORK_COMPLETED, 'Work completed', work_id))
            self.assertEqual(completed.message, f'Installation work for Asha Menon (Work #{work_id}) has been completed by Staff A.')
        self.assertEqual(self.received(self.staff_a), [])

    def test_deleting_the_record_deletes_the_notifications_about_it(self):
        lead = self.make_lead()
        self.as_user(self.staff_a)
        self.client.post(reverse('lead-list'), {'name': 'Danish PV', 'phone': '9876500001', 'district': 'Thrissur', 'plan': self.plan.pk}, format='json')
        new_lead = Lead.objects.get(name='Danish PV')
        self.assertEqual(Notification.objects.filter(lead=new_lead).count(), 2)
        self.as_user(self.admin)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[new_lead.pk])).status_code, 204)
        self.assertFalse(Notification.objects.filter(lead=new_lead).exists())
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())


class NotificationApiTests(NotificationTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        lead = Lead.objects.create(name='Asha Menon', phone='9876543210', district='Ernakulam', plan=cls.plan,
                                   amount=cls.plan.amount, created_by=cls.admin)
        cls.for_a = [
            Notification.objects.create(recipient=cls.staff_a, kind=NotificationKind.ACTIVITY_ASSIGNED, title=f'Task {i}',
                                        message='Assigned to you.', lead=lead)
            for i in range(3)
        ]
        cls.for_b = Notification.objects.create(recipient=cls.staff_b, kind=NotificationKind.ACTIVITY_ASSIGNED, title='B task',
                                                message='Assigned to you.', lead=lead)

    def test_each_user_lists_only_their_own_newest_first_with_an_unread_count(self):
        self.as_user(self.staff_a)
        listed = self.client.get(reverse('notification-list'))
        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        self.assertEqual(listed.data['count'], 3)
        self.assertEqual([row['title'] for row in listed.data['results']], ['Task 2', 'Task 1', 'Task 0'])
        row = listed.data['results'][0]
        self.assertEqual((row['kind'], row['message'], row['lead'], row['work'], row['activity'], row['is_read'], row['read_at']),
                         ('ACTIVITY_ASSIGNED', 'Assigned to you.', self.for_a[0].lead_id, None, None, False, None))
        self.assertEqual(self.client.get(reverse('notification-unread-count')).data, {'count': 3})
        # Nothing of Staff B's reaches Staff A, even by id.
        self.assertEqual(self.client.post(reverse('notification-read', args=[self.for_b.pk])).status_code, 404)
        self.assertIsNone(Notification.objects.get(pk=self.for_b.pk).read_at)
        self.as_user(self.staff_b)
        self.assertEqual([row['title'] for row in self.client.get(reverse('notification-list')).data['results']], ['B task'])
        # Any signed-in user has their notifications, modules or not; a signed-out request is refused.
        Role.objects.get(name=Role.STAFF).permissions.clear()
        self.as_user(self.staff_b)
        self.assertEqual(self.client.get(reverse('notification-list')).status_code, status.HTTP_200_OK)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('notification-list')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_mark_one_and_mark_all_as_read(self):
        self.as_user(self.staff_a)
        read = self.client.post(reverse('notification-read', args=[self.for_a[0].pk]))
        self.assertEqual((read.status_code, read.data['is_read']), (200, True))
        self.assertIsNotNone(read.data['read_at'])
        first_read_at = Notification.objects.get(pk=self.for_a[0].pk).read_at
        # Reading it again keeps the first time.
        self.client.post(reverse('notification-read', args=[self.for_a[0].pk]))
        self.assertEqual(Notification.objects.get(pk=self.for_a[0].pk).read_at, first_read_at)
        self.assertEqual(self.client.get(reverse('notification-unread-count')).data, {'count': 2})

        self.assertEqual(self.client.post(reverse('notification-read-all')).data, {'count': 2})
        self.assertEqual(self.client.get(reverse('notification-unread-count')).data, {'count': 0})
        self.assertEqual(self.client.post(reverse('notification-read-all')).data, {'count': 0})
        # Staff B's stays unread: read-all is per user.
        self.assertIsNone(Notification.objects.get(pk=self.for_b.pk).read_at)

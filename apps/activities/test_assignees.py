"""Activities assigned to several people: one row each, seen only by their assignees (and admins), completed once by any
of them, edited only by admins, and each person told once."""

from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, SolarPlan
from apps.notifications.models import Notification, NotificationKind
from apps.works.tests import convert_lead

from .models import Activity, ActivityStatus

PASSWORD = 'Solar-Panel-2026'


class AssigneesTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.second_admin = User.objects.create_user(
            email='admin2@example.com', password=PASSWORD, name='Second Admin', role=Role.ADMIN,
        )
        cls.asha = User.objects.create_user(email='asha@example.com', password=PASSWORD, name='Asha Nair')
        cls.binu = User.objects.create_user(email='binu@example.com', password=PASSWORD, name='Binu Nair')
        cls.carol = User.objects.create_user(email='carol@example.com', password=PASSWORD, name='Carol Thomas')
        # Staff hold the Activities module only, as Settings -> Roles & Access allows.
        Role.objects.get(pk=Role.STAFF).permissions.add(Permission.objects.get(codename='access_activities'))
        plan = SolarPlan.objects.get(capacity=5)
        cls.lead = Lead.objects.create(name='Ravi Kumar', phone='9876543210', plan=plan, amount=plan.amount, created_by=cls.admin)
        cls.work = convert_lead(plan, cls.admin, name='Suresh P', phone='9876500001')

    def setUp(self):
        self.as_user(self.admin)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def add(self, assignees, **fields):
        """Adds an activity assigned to `assignees`; `assignee_ids=None` leaves them out (a legacy client's request)."""
        body = {'assignee_ids': [user.pk for user in assignees], **fields}
        if body['assignee_ids'] is None:
            del body['assignee_ids']
        return self.client.post(reverse('activity-list'), body, format='json')

    def add_follow_up(self, *assignees, **fields):
        return self.add(assignees, **{
            'lead': self.lead.pk, 'title': 'Call', 'type': 'PHONE_CALL', 'due_date': '2026-10-21', **fields,
        })

    def add_work_activity(self, *assignees, **fields):
        return self.add(assignees, **{'work': self.work.pk, 'type': 'SITE_VISIT', 'description': 'Roof survey.', **fields})

    def created(self, response):
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return Activity.objects.get(pk=response.data['id'])

    def patch(self, activity, **body):
        return self.client.patch(reverse('activity-detail', args=[activity.pk]), body, format='json')

    def complete(self, activity, note=''):
        return self.client.post(reverse('activity-complete', args=[activity.pk]), {'completion_note': note}, format='json')

    def page(self, name='activity-list', **params):
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def told(self, kind):
        return sorted(Notification.objects.filter(kind=kind).values_list('recipient__name', flat=True))


class SeveralAssigneesTests(AssigneesTestCase):
    def test_a_follow_up_or_a_work_activity_can_have_several_assignees_and_is_listed_once(self):
        follow_up = self.add_follow_up(self.binu, self.asha)
        self.assertEqual(
            follow_up.data['assignees'], [{'id': self.asha.pk, 'name': 'Asha Nair'}, {'id': self.binu.pk, 'name': 'Binu Nair'}],
        )
        self.assertEqual((follow_up.data['assigned_to'], follow_up.data['assigned_to_name']), (self.asha.pk, 'Asha Nair, Binu Nair'))
        self.created(self.add_work_activity(self.asha, self.binu))

        self.assertEqual(self.page()['count'], 1)
        for user, count in ((self.asha, 1), (self.binu, 1), (self.carol, 0)):
            self.assertEqual(self.page(assigned_to=user.pk)['count'], count, user.name)
            self.assertEqual(self.page('activity-works', assigned_to=user.pk)['count'], count, user.name)
        # Both names match the search, and the activity is still one row.
        self.assertEqual(self.page('activity-works', search='nair')['count'], 1)

    def test_a_follow_up_needs_someone_and_only_active_users_can_be_newly_assigned(self):
        missing = self.add_follow_up()
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assignee_ids', missing.data)
        gone = User.objects.create_user(email='gone@example.com', password=PASSWORD, name='Gone', is_active=False)
        for response in (self.add_follow_up(self.asha, gone), self.add_work_activity(gone)):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn('assignee_ids', response.data)
        self.created(self.add_work_activity())  # a Work's activity may have no one
        self.assertEqual(Activity.objects.count(), 1)

    def test_the_one_assignee_field_of_earlier_clients_still_works(self):
        activity = self.created(self.add_work_activity(assignee_ids=None, assigned_to=self.binu.pk))
        self.assertEqual(list(activity.assignees.all()), [self.binu])
        self.patch(activity, assignee_ids=[self.asha.pk, self.binu.pk])

        self.assertEqual(self.patch(activity, assigned_to=self.carol.pk).data['assignees'], [{'id': self.carol.pk, 'name': 'Carol Thomas'}])
        self.assertEqual(self.patch(activity, assigned_to=None).data['assigned_to'], None)
        refused = self.add_follow_up(assignee_ids=None, assigned_to=None)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assigned_to', refused.data)
        both = self.patch(activity, assigned_to=self.asha.pk, assignee_ids=[self.binu.pk])
        self.assertEqual(both.status_code, status.HTTP_400_BAD_REQUEST)

    def test_listing_takes_the_same_number_of_queries_however_many_assignees(self):
        def count(name):
            with CaptureQueriesContext(connection) as queries:
                self.page(name)
            return len(queries)

        self.created(self.add_follow_up(self.asha))
        self.created(self.add_work_activity(self.asha))
        # As staff: whether each row's lead is theirs comes from the same prefetch as the assignees.
        self.as_user(self.asha)
        few = count('activity-list'), count('activity-works')
        self.as_user(self.admin)
        for _ in range(4):
            self.created(self.add_follow_up(self.asha, self.binu, self.carol))
            self.created(self.add_work_activity(self.asha, self.binu, self.carol))
        self.as_user(self.asha)
        self.assertEqual((count('activity-list'), count('activity-works')), few)


class SharedActivityAccessTests(AssigneesTestCase):
    def setUp(self):
        super().setUp()
        self.shared = self.created(self.add_follow_up(self.asha, self.binu, self.second_admin, title='Shared call'))
        self.ashas = self.created(self.add_follow_up(self.asha, title='Asha only'))
        self.carols = self.created(self.add_work_activity(self.carol, description="Carol's visit."))
        self.nobodys = self.created(self.add_work_activity(description='Unassigned visit.'))
        Notification.objects.all().delete()

    def visible(self, user):
        self.as_user(user)
        follow_ups = [row['title'] for row in self.page()['results']]
        work = [row['description'] for row in self.page('activity-works')['results']]
        return sorted(follow_ups), work

    def test_each_staff_member_sees_only_the_activities_assigned_to_them(self):
        self.assertEqual(self.visible(self.asha), (['Asha only', 'Shared call'], []))
        self.assertEqual(self.visible(self.binu), (['Shared call'], []))
        self.assertEqual(self.visible(self.carol), ([], ["Carol's visit."]))
        self.as_user(self.binu)
        for other in (self.ashas, self.carols, self.nobodys):
            self.assertEqual(self.client.get(reverse('activity-detail', args=[other.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('activity-detail', args=[self.shared.pk])).status_code, 200)

    def test_any_assignee_completes_a_shared_activity_once_and_no_one_else_can(self):
        self.as_user(self.carol)
        self.assertEqual(self.complete(self.shared).status_code, status.HTTP_403_FORBIDDEN)
        self.as_user(self.binu)
        done = self.complete(self.shared, 'Spoke to Ravi.')
        self.assertEqual((done.status_code, done.data['completed_by_name']), (200, 'Binu Nair'))
        self.as_user(self.asha)
        again = self.complete(self.shared, 'Me too.')
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        shared = Activity.objects.get(pk=self.shared.pk)
        self.assertEqual((shared.status, shared.completed_by, shared.completion_note), (ActivityStatus.COMPLETED, self.binu, 'Spoke to Ravi.'))
        # The admins and the other assignees are told, once each (the second admin is both), and not whoever did it.
        self.assertEqual(self.told(NotificationKind.ACTIVITY_STATUS), ['Admin', 'Asha Nair', 'Second Admin'])

    def test_sharing_an_activity_gives_staff_no_right_to_edit_reassign_reopen_or_delete_it(self):
        stored = Activity.objects.order_by('pk', 'assignees').values_list('pk', 'title', 'description', 'status', 'assignees')
        before = list(stored)
        self.as_user(self.asha)
        for body in ({'assignee_ids': [self.asha.pk]}, {'assigned_to': self.asha.pk}, {'title': 'Mine now'},
                     {'assignee_ids': [self.asha.pk, self.binu.pk, self.second_admin.pk, self.carol.pk]}):
            self.assertEqual(self.patch(self.shared, **body).status_code, status.HTTP_403_FORBIDDEN, body)
        self.assertEqual(self.client.delete(reverse('activity-detail', args=[self.shared.pk])).status_code, 403)
        self.as_user(self.carol)
        self.assertEqual(self.complete(self.carols).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.carols, status='PENDING').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(list(stored.exclude(pk=self.carols.pk)), [row for row in before if row[0] != self.carols.pk])


class AssignmentNotificationTests(AssigneesTestCase):
    def test_only_people_newly_assigned_are_told_and_each_once(self):
        activity = self.created(self.add_work_activity(self.asha, self.binu, self.admin))
        # Not the admin who assigned it to themselves too.
        self.assertEqual(self.told(NotificationKind.ACTIVITY_ASSIGNED), ['Asha Nair', 'Binu Nair'])

        self.patch(activity, assignee_ids=[self.asha.pk, self.binu.pk, self.carol.pk])
        self.assertEqual(self.told(NotificationKind.ACTIVITY_ASSIGNED), ['Asha Nair', 'Binu Nair', 'Carol Thomas'])
        self.patch(activity, assignee_ids=[self.binu.pk])
        self.patch(activity, description='Roof survey, with photos.')
        self.assertEqual(Notification.objects.filter(kind=NotificationKind.ACTIVITY_ASSIGNED).count(), 3)

        self.as_user(self.second_admin)
        self.patch(activity, status='COMPLETED')
        self.assertEqual(self.told(NotificationKind.ACTIVITY_STATUS), ['Admin', 'Binu Nair'])


class SharedActivityCountsTests(AssigneesTestCase):
    def test_the_dashboard_and_reports_count_a_shared_activity_once_and_once_for_each_assignee(self):
        today = timezone.localdate()
        self.created(self.add_work_activity(self.admin, self.asha, due_date=str(today)))

        summary = self.page('dashboard-summary')
        self.assertEqual(summary['follow_ups']['works']['pending'], 1)
        self.assertEqual(summary['mine']['follow_ups']['works']['pending'], 1)
        report = self.page('report-activities-summary')
        self.assertEqual(report['totals']['total'], 1)
        self.assertEqual({row['name']: row['count'] for row in report['by_staff']}, {'Admin': 1, 'Asha Nair': 1})
        rows = self.page('report-activities')
        self.assertEqual((rows['count'], rows['results'][0]['assigned_to_name']), (1, 'Admin, Asha Nair'))
        self.assertEqual(self.page('report-activities', assigned_to=self.asha.pk)['count'], 1)
        self.assertEqual(self.page('report-activities', assigned_to='none')['count'], 0)
        staff = {row['user']['name']: row['activities_assigned'] for row in self.page('report-staff')['results'] if row['user']}
        self.assertEqual((staff['Admin'], staff['Asha Nair'], staff['Binu Nair']), (1, 1, 0))

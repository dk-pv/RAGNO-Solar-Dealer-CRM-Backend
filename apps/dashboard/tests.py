from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity, ActivityStatus
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.works.models import Work, WorkStage

PASSWORD = 'Solar-Panel-2026'


class DashboardTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.danish = User.objects.create_user(email='danish@example.com', password=PASSWORD, name='Danish')
        cls.hrithik = User.objects.create_user(email='hrithik@example.com', password=PASSWORD, name='Hrithik')
        cls.plan = SolarPlan.objects.get(capacity=5)
        cls.today = timezone.localdate()

    def lead(self, name, assigned_to=None, **fields):
        return Lead.objects.create(
            name=name, phone='9876543210', district='Ernakulam', plan=self.plan, amount=self.plan.amount,
            created_by=fields.pop('created_by', self.admin), assigned_to=assigned_to, **fields,
        )

    def work(self, name, assigned_to=None, **fields):
        lead = self.lead(name, assigned_to=assigned_to, status=LeadStatus.SUPERHOT)
        lead.convert(fields.pop('created_by', self.admin))
        Work.objects.filter(pk=lead.work.pk).update(**fields)
        return Work.objects.get(pk=lead.work.pk)

    def activity(self, created_by=None, **fields):
        fields.setdefault('type', 'FOLLOW_UP')
        fields.setdefault('description', 'Follow up.')
        if fields.get('status') == ActivityStatus.COMPLETED:
            fields.setdefault('completed_at', timezone.now())
            fields.setdefault('completed_by', created_by or self.admin)
        return Activity.objects.create(created_by=created_by or self.admin, **fields)

    def give_staff(self, *codenames):
        role = Role.objects.get(pk=Role.STAFF)
        role.permissions.add(*Permission.objects.filter(codename__in=codenames))

    def get(self, name, as_user=None, **params):
        self.client.force_authenticate(User.objects.get(pk=(as_user or self.admin).pk))
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


LEADS_MODULE = ['view_lead', 'add_lead', 'change_lead']


class SummaryTests(DashboardTestCase):
    def test_counts_come_from_the_records(self):
        day = timedelta(days=1)
        self.lead('New', status=LeadStatus.NEW, next_follow_up=self.today - day)  # overdue
        self.lead('Hot', status=LeadStatus.HOT, next_follow_up=self.today)  # due today
        self.lead('Lost', status=LeadStatus.LOST, next_follow_up=self.today - day)  # closed: not a follow-up
        logged = self.lead('Logged', status=LeadStatus.INITIAL_CONTACT, assigned_to=self.danish)
        # A lead's activity is its log, never a pending follow-up.
        self.activity(lead=logged, description='Called.')
        done = self.work('Done', stage=WorkStage.COMPLETED)
        busy = self.work('Busy', assigned_to=self.danish)
        self.activity(work=busy, due_date=self.today + day, assigned_to=self.danish)  # upcoming
        self.activity(work=busy, due_date=self.today - day)  # overdue
        self.activity(work=busy)  # pending, no date
        self.activity(work=done, status=ActivityStatus.COMPLETED)

        data = self.get('dashboard-summary')

        self.assertEqual(
            {key: data['leads'][key] for key in ('total', 'active', 'confirmed', 'lost')},
            {'total': 6, 'active': 3, 'confirmed': 2, 'lost': 1},
        )
        self.assertEqual({row['status']: row['count'] for row in data['leads']['by_status']}['WON'], 2)
        self.assertEqual([row['status'] for row in data['leads']['by_status']], LeadStatus.values)
        self.assertEqual(data['works'], {'total': 2, 'active': 1, 'completed': 1})
        self.assertEqual(data['follow_ups']['total'], {'pending': 5, 'overdue': 2, 'today': 1, 'upcoming': 1})
        self.assertEqual(data['follow_ups']['works'], {'pending': 3, 'overdue': 1, 'today': 0, 'upcoming': 1})
        self.assertEqual(data['follow_ups']['leads'], {'pending': 2, 'overdue': 1, 'today': 1, 'upcoming': 0})
        # The admin's own numbers: nothing is assigned to them.
        self.assertEqual((data['mine']['leads'], data['mine']['works'], data['mine']['follow_ups']['total']['pending']), (0, 0, 0))

    def test_my_numbers(self):
        self.give_staff('access_dashboard', 'access_work', *LEADS_MODULE)
        self.lead('Mine', assigned_to=self.danish, next_follow_up=self.today)
        self.lead('Not mine', assigned_to=self.hrithik, next_follow_up=self.today)
        work = self.work('My work', assigned_to=self.danish)
        self.activity(work=work, assigned_to=self.danish, due_date=self.today)
        self.activity(work=work, assigned_to=self.hrithik, due_date=self.today)

        mine = self.get('dashboard-summary', self.danish)['mine']

        self.assertEqual((mine['leads'], mine['works']), (1, 1))
        self.assertEqual(mine['follow_ups']['total'], {'pending': 2, 'overdue': 0, 'today': 2, 'upcoming': 0})


class PermissionTests(DashboardTestCase):
    ENDPOINTS = ['dashboard-summary', 'dashboard-recent', 'dashboard-timeline']

    def test_the_dashboard_needs_the_dashboard_module(self):
        self.give_staff('access_work', *LEADS_MODULE)
        self.client.force_authenticate(self.danish)
        for name in self.ENDPOINTS:
            self.assertEqual(self.client.get(reverse(name)).status_code, status.HTTP_403_FORBIDDEN, name)
        self.assertEqual(self.client.get(reverse('dashboard-follow-ups'), {'bucket': 'today'}).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('dashboard-summary')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sections_follow_the_modules_and_staff_see_only_their_own_leads(self):
        self.lead('Mine', assigned_to=self.danish, next_follow_up=self.today)
        self.lead('Hrithik lead', assigned_to=self.hrithik, next_follow_up=self.today)
        self.work('A Work')
        self.give_staff('access_dashboard')

        bare = self.get('dashboard-summary', self.danish)
        self.assertEqual((bare['leads'], bare['works'], bare['follow_ups']['total']['pending']), (None, None, 0))
        self.assertEqual(self.get('dashboard-recent', self.danish), {'leads': None, 'works': None})
        self.assertEqual(self.get('dashboard-timeline', self.danish)['count'], 0)

        self.give_staff(*LEADS_MODULE)
        with_leads = self.get('dashboard-summary', self.danish)
        self.assertEqual((with_leads['leads']['total'], with_leads['works']), (1, None))
        today = self.get('dashboard-follow-ups', self.danish, bucket='today')
        self.assertEqual([item['customer_name'] for item in today['results']], ['Mine'])
        timeline = self.get('dashboard-timeline', self.danish)
        self.assertEqual([event['lead']['customer_name'] for event in timeline['results']], ['Mine'])

        self.give_staff('access_work')
        self.assertEqual(self.get('dashboard-summary', self.danish)['works']['total'], 1)


class FollowUpTests(DashboardTestCase):
    def test_each_group_lists_works_and_leads_together(self):
        day = timedelta(days=1)
        work = self.work('Suresh P', assigned_to=self.hrithik)
        self.activity(work=work, description='Collect documents.', due_date=self.today - 2 * day, assigned_to=self.hrithik)
        self.activity(work=work, description='Site visit.', due_date=self.today + day)
        self.activity(work=work, description='Call back.')
        self.activity(work=work, description='Done.', status=ActivityStatus.COMPLETED, created_by=self.danish)
        self.lead('Arun Kumar', next_follow_up=self.today - day)
        self.lead('Won lead', status=LeadStatus.WON, next_follow_up=self.today - day)

        def listed(bucket, **params):
            data = self.get('dashboard-follow-ups', bucket=bucket, **params)
            return data['count'], [item['description'] or item['customer_name'] for item in data['results']]

        self.assertEqual(listed('overdue'), (2, ['Collect documents.', 'Arun Kumar']))
        self.assertEqual(listed('upcoming'), (1, ['Site visit.']))
        self.assertEqual(listed('today'), (0, []))
        # All pending, the undated last.
        self.assertEqual(listed('pending'), (4, ['Collect documents.', 'Arun Kumar', 'Site visit.', 'Call back.']))
        self.assertEqual(listed('pending', limit=2), (4, ['Collect documents.', 'Arun Kumar']))
        completed = self.get('dashboard-follow-ups', bucket='completed')['results']
        self.assertEqual([(item['description'], item['completed_by_name']) for item in completed], [('Done.', 'Danish')])
        overdue = self.get('dashboard-follow-ups', bucket='overdue')['results'][0]
        self.assertEqual(
            (overdue['source'], overdue['work'], overdue['customer_name'], overdue['assigned_to_name'], overdue['status']),
            ('work', work.pk, 'Suresh P', 'Hrithik', 'PENDING'),
        )
        self.assertEqual(listed('overdue', mine='true'), (0, []))
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(reverse('dashboard-follow-ups'), {'bucket': 'later'}).status_code, 400)


class TimelineTests(DashboardTestCase):
    def events(self, as_user=None, **params):
        return self.get('dashboard-timeline', as_user, **params)['results']

    def test_records_with_no_activity_are_still_shown_and_never_given_one(self):
        lead = self.lead('Arun Kumar', assigned_to=self.danish, created_by=self.hrithik)
        work = self.work('Suresh P', assigned_to=self.hrithik, stage=WorkStage.STRUCTURE)

        events = self.events()

        # Converting creates the Work's own lead too: two leads added, one Work, and no activity at all.
        self.assertEqual(sorted(event['kind'] for event in events), ['lead_created', 'lead_created', 'work_created'])
        added = next(event for event in events if event['lead'] and event['lead']['id'] == lead.pk)
        self.assertEqual(
            (added['user']['name'], added['lead']['id'], added['lead']['customer_name'], added['lead']['assigned_to_name'],
             added['lead']['status'], added['lead']['latest_activity'], added['activity']),
            ('Hrithik', lead.pk, 'Arun Kumar', 'Danish', 'NEW', None, None),
        )
        converted = next(event for event in events if event['kind'] == 'work_created')
        self.assertEqual(
            (converted['work']['id'], converted['work']['stage'], converted['work']['assigned_to_name'],
             converted['work']['latest_activity'], converted['lead']),
            (work.pk, 'STRUCTURE', 'Hrithik', None, None),
        )
        self.assertFalse(Activity.objects.exists())

    def test_an_activity_shows_who_did_it_and_exactly_when(self):
        work = self.work('Suresh P')
        added = self.activity(work=work, created_by=self.danish, description='Collect KSEB documents.')
        moment = datetime(2026, 9, 30, 4, 42, 7, tzinfo=dt_timezone.utc)  # 10:12:07 in Asia/Kolkata
        Activity.objects.filter(pk=added.pk).update(
            created_at=moment, status=ActivityStatus.COMPLETED, completed_by=self.hrithik,
            completed_at=moment + timedelta(hours=1),
        )

        events = [event for event in self.events(record='activities')]

        self.assertEqual([(event['kind'], event['user']['name']) for event in events], [
            ('activity_completed', 'Hrithik'), ('activity_added', 'Danish'),
        ])
        self.assertEqual(events[1]['at'], moment)
        self.assertEqual(events[0]['at'], moment + timedelta(hours=1))
        self.assertEqual(
            (events[1]['activity']['description'], events[1]['work']['id'], events[1]['work']['latest_activity']['type_display']),
            ('Collect KSEB documents.', work.pk, 'Follow-up'),
        )

    def test_filters_by_user_record_and_day_in_the_crm_time_zone(self):
        lead = self.lead('Arun Kumar', created_by=self.danish)
        work = self.work('Suresh P', created_by=self.hrithik)
        self.activity(work=work, created_by=self.danish)
        # 20:00 UTC on 1 Oct is 01:30 on 2 Oct in Asia/Kolkata: it belongs to 2 Oct. Everything else is on 20 Sept.
        Lead.objects.filter(pk=lead.pk).update(created_at=datetime(2026, 10, 1, 20, 0, tzinfo=dt_timezone.utc))
        earlier = datetime(2026, 9, 20, 6, 0, tzinfo=dt_timezone.utc)
        Lead.objects.exclude(pk=lead.pk).update(created_at=earlier)
        Work.objects.update(created_at=earlier)
        Activity.objects.update(created_at=earlier)

        kinds = lambda **params: sorted(event['kind'] for event in self.events(**params))  # noqa: E731
        self.assertEqual(kinds(user=self.danish.pk), ['activity_added', 'lead_created'])
        self.assertEqual(kinds(user=self.hrithik.pk), ['work_created'])
        self.assertEqual(kinds(record='leads'), ['lead_created', 'lead_created'])
        self.assertEqual(kinds(record='works'), ['activity_added', 'work_created'])
        self.assertEqual(kinds(date_from='2026-10-02', date_to='2026-10-02'), ['lead_created'])
        self.assertEqual(kinds(date_from='2026-10-01', date_to='2026-10-01'), [])
        self.assertEqual(kinds(date_to='2026-09-30'), ['activity_added', 'lead_created', 'work_created'])
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('dashboard-timeline'), {'date_from': '2026-10-05', 'date_to': '2026-10-01'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_newest_first_paginated_and_one_query_per_kind_of_record_whatever_the_page_size(self):
        for index in range(3):
            self.work(f'Customer {index}')

        def page(**params):
            with CaptureQueriesContext(connection) as queries:
                data = self.get('dashboard-timeline', **params)
            return data, len(queries)

        small, small_queries = page(page_size=2)
        self.assertEqual((small['count'], len(small['results'])), (6, 2))  # 3 leads added, 3 Works
        times = [event['at'] for event in self.get('dashboard-timeline')['results']]
        self.assertEqual(times, sorted(times, reverse=True))
        for index in range(5):
            self.activity(work=Work.objects.first(), description=f'Note {index}')
        large, large_queries = page(page_size=20)
        self.assertEqual(len(large['results']), 11)
        self.assertEqual(large_queries, small_queries + 1)  # the activities on the page: one more query in all


class RecentTests(DashboardTestCase):
    def test_recent_records_show_their_latest_activity_or_none(self):
        quiet = self.lead('Quiet lead')
        busy = self.work('Busy work')
        self.activity(work=busy, type='SITE_VISIT')

        data = self.get('dashboard-recent')

        self.assertEqual(
            [(lead['customer_name'], lead['latest_activity']) for lead in data['leads']][-1], ('Quiet lead', None),
        )
        self.assertIn(quiet.pk, [lead['id'] for lead in data['leads']])
        self.assertEqual(data['works'][0]['latest_activity']['type_display'], 'Site visit')

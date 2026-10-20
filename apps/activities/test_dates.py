"""Activity dates and times. Moments (created, completed) are stored time-zone aware and sent with their offset, so a
browser shows them in Asia/Kolkata whatever its own zone; due dates are calendar days, sent as YYYY-MM-DD and never moved
by a time zone. Overdue, today and upcoming are decided on the CRM's calendar day (Asia/Kolkata), not UTC's."""

from datetime import date, datetime, timezone as dt_timezone
from unittest import mock

from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, SolarPlan
from apps.works.tests import convert_lead

from .models import Activity, ActivityStatus

PASSWORD = 'Solar-Panel-2026'
UTC = dt_timezone.utc
# 00:30 on 21 October in Asia/Kolkata, while it is still 20 October in UTC.
JUST_AFTER_MIDNIGHT = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)


class ActivityDateTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        plan = SolarPlan.objects.get(capacity=5)
        cls.lead = Lead.objects.create(name='Ravi Kumar', phone='9876543210', plan=plan, amount=plan.amount, created_by=cls.admin)
        cls.work = convert_lead(plan, cls.admin, name='Suresh P', phone='9876500001')

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def get(self, name, *args, **params):
        response = self.client.get(reverse(name, args=args), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()  # as the browser receives it: strings, not Python dates

    def test_moments_are_sent_with_their_offset_and_due_dates_as_plain_days(self):
        activity = Activity.objects.create(
            work=self.work, type='SITE_VISIT', description='Roof survey.', due_date=date(2026, 10, 21), created_by=self.admin,
        )
        # Added just after midnight and completed just before it, in Asia/Kolkata.
        Activity.objects.filter(pk=activity.pk).update(
            created_at=JUST_AFTER_MIDNIGHT, status=ActivityStatus.COMPLETED,
            completed_at=datetime(2026, 10, 20, 18, 29, tzinfo=UTC), completed_by=self.admin,
        )

        data = self.get('activity-detail', activity.pk)

        self.assertEqual(data['created_at'], '2026-10-21T00:30:00+05:30')
        self.assertEqual(data['completed_at'], '2026-10-20T23:59:00+05:30')
        self.assertEqual(data['due_date'], '2026-10-21')
        # The Dashboard sends its moments in UTC instead: the same instants, and a browser reads them as such.
        completed = self.get('dashboard-follow-ups', bucket='completed')['results'][0]
        self.assertEqual(datetime.fromisoformat(completed['created_at']), JUST_AFTER_MIDNIGHT)
        self.assertEqual(datetime.fromisoformat(completed['completed_at']), datetime(2026, 10, 20, 18, 29, tzinfo=UTC))
        self.assertEqual(completed['due_date'], '2026-10-21')
        for moment in (data['created_at'], completed['created_at'], completed['completed_at']):
            self.assertIsNotNone(datetime.fromisoformat(moment).tzinfo, moment)  # never a bare local time

    def test_overdue_today_and_upcoming_follow_the_day_in_asia_kolkata_just_after_midnight(self):
        for due in (date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), None):
            Activity.objects.create(work=self.work, type='FOLLOW_UP', description=f'Due {due}', due_date=due, created_by=self.admin)

        with mock.patch('django.utils.timezone.now', return_value=JUST_AFTER_MIDNIGHT):
            summary = self.get('dashboard-summary')
            listed = {
                bucket: [item['description'] for item in self.get('dashboard-follow-ups', bucket=bucket)['results']]
                for bucket in ('overdue', 'today', 'upcoming', 'pending')
            }
            overdue_report = [row['description'] for row in self.get('report-activities', overdue='true')['results']]

        self.assertEqual(summary['today'], '2026-10-21')
        self.assertEqual(summary['follow_ups']['works'], {'pending': 4, 'overdue': 1, 'today': 1, 'upcoming': 1})
        self.assertEqual(listed['overdue'], ['Due 2026-10-20'])
        self.assertEqual(listed['today'], ['Due 2026-10-21'])
        self.assertEqual(listed['upcoming'], ['Due 2026-10-22'])
        # Unscheduled: listed last, and never given a date.
        self.assertEqual(listed['pending'], ['Due 2026-10-20', 'Due 2026-10-21', 'Due 2026-10-22', 'Due None'])
        self.assertEqual(overdue_report, ['Due 2026-10-20'])

    def test_due_date_filters_are_whole_days_and_creation_days_are_asia_kolkata_days(self):
        for due in (date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)):
            follow_up = Activity.objects.create(
                lead=self.lead, title=f'Due {due}', type='PHONE_CALL', due_date=due, created_by=self.admin,
            )
            follow_up.assignees.set([self.admin])
        titles = lambda **params: sorted(row['title'] for row in self.get('activity-list', **params)['results'])  # noqa: E731

        self.assertEqual(titles(due_after='2026-10-21', due_before='2026-10-21'), ['Due 2026-10-21'])
        self.assertEqual(titles(due_before='2026-10-21'), ['Due 2026-10-20', 'Due 2026-10-21'])
        # A record made at 00:30 in Asia/Kolkata belongs to that day (21 October), not to UTC's 20 October.
        Activity.objects.filter(work=None).update(created_at=JUST_AFTER_MIDNIGHT)
        rows = self.get('report-activities', date_from='2026-10-21', date_to='2026-10-21')['results']
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.get('report-activities', date_from='2026-10-20', date_to='2026-10-20')['count'], 0)

import csv
import io
import zipfile
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from xml.etree import ElementTree

from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity, ActivityStatus
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.works.models import Work, WorkStage

PASSWORD = 'Solar-Panel-2026'
SEPT = {'date_from': '2026-09-01', 'date_to': '2026-09-30'}


def at(day, hour=6):
    """A moment on `day` (YYYY-MM-DD) at `hour` UTC, which is the same calendar day in Asia/Kolkata for hours < 18."""
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=dt_timezone.utc)


class ReportTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.danish = User.objects.create_user(email='danish@example.com', password=PASSWORD, name='Danish')
        cls.hrithik = User.objects.create_user(email='hrithik@example.com', password=PASSWORD, name='Hrithik')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def lead(self, name, day='2026-09-10', **fields):
        fields.setdefault('created_by', self.admin)
        lead = Lead.objects.create(
            name=name, phone='9876543210', district='Ernakulam', plan=self.plan, amount=fields.pop('amount', self.plan.amount),
            **fields,
        )
        Lead.objects.filter(pk=lead.pk).update(created_at=at(day))
        return Lead.objects.get(pk=lead.pk)

    def work(self, name, day='2026-09-10', amount=Decimal('185000'), by=None, **fields):
        lead = self.lead(name, day=day, status=LeadStatus.SUPERHOT, amount=amount, assigned_to=fields.pop('assigned_to', None))
        lead.convert(by or self.admin)
        Work.objects.filter(pk=lead.work.pk).update(created_at=at(day), **fields)
        return Work.objects.get(pk=lead.work.pk)

    def give_staff(self, *codenames):
        Role.objects.get(pk=Role.STAFF).permissions.add(*Permission.objects.filter(codename__in=codenames))

    def get(self, name, as_user=None, **params):
        self.client.force_authenticate(User.objects.get(pk=(as_user or self.admin).pk))
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, getattr(response, 'data', response))
        return response


LEADS = ['view_lead', 'add_lead', 'change_lead']


class LeadReportTests(ReportTestCase):
    def test_period_filters_and_summary(self):
        self.lead('In Sept', status=LeadStatus.HOT, assigned_to=self.danish, source='REFERRAL')
        self.lead('Won Sept', status=LeadStatus.WON, assigned_to=self.danish)
        self.lead('August', day='2026-08-31')
        # 20:00 UTC on 30 Sept is 01:30 on 1 Oct in Asia/Kolkata: outside September.
        late = self.lead('Late')
        Lead.objects.filter(pk=late.pk).update(created_at=datetime(2026, 9, 30, 20, 0, tzinfo=dt_timezone.utc))

        summary = self.get('report-leads-summary', **SEPT).data
        names = [row['customer_name'] for row in self.get('report-leads', **SEPT).data['results']]

        self.assertEqual(sorted(names), ['In Sept', 'Won Sept'])
        self.assertEqual(summary['totals'], {'created': 2, 'open': 1, 'confirmed': 1, 'lost': 0})
        self.assertEqual({row['value']: row['count'] for row in summary['by_status']}['HOT'], 1)
        self.assertEqual(
            [(row['name'], row['count'], row['open'], row['confirmed']) for row in summary['by_staff']], [('Danish', 2, 1, 1)],
        )
        self.assertEqual(
            {row['label']: row['count'] for row in summary['by_source'] if row['count']}, {'Referral': 1, 'Not set': 1},
        )
        filtered = self.get('report-leads', **SEPT, status='HOT', source='REFERRAL', assigned_to=self.danish.pk).data
        self.assertEqual([row['customer_name'] for row in filtered['results']], ['In Sept'])
        self.assertEqual(self.get('report-leads', **SEPT, assigned_to='none').data['count'], 0)

    def test_a_lead_with_no_activity_is_listed_as_having_none(self):
        quiet = self.lead('Quiet')
        busy = self.lead('Busy')
        Activity.objects.create(lead=busy, type='PHONE_CALL', description='Called.', created_by=self.admin)

        rows = {row['id']: row for row in self.get('report-leads', **SEPT).data['results']}

        self.assertIsNone(rows[quiet.pk]['latest_activity'])
        self.assertEqual(rows[busy.pk]['latest_activity']['type_display'], 'Phone call')
        self.assertEqual(Activity.objects.count(), 1)

    def test_trend_counts_each_day_including_empty_ones(self):
        self.lead('One', day='2026-09-02')
        self.lead('Two', day='2026-09-02')
        self.lead('Three', day='2026-09-04')

        trend = self.get('report-leads-summary', date_from='2026-09-01', date_to='2026-09-05').data['trend']

        self.assertEqual(trend['unit'], 'day')
        self.assertEqual([(str(point['start']), point['created']) for point in trend['points']], [
            ('2026-09-01', 0), ('2026-09-02', 2), ('2026-09-03', 0), ('2026-09-04', 1), ('2026-09-05', 0),
        ])
        by_week = self.get('report-leads-summary', date_from='2026-06-01', date_to='2026-09-30').data['trend']
        self.assertEqual(by_week['unit'], 'week')
        self.assertEqual(sum(point['created'] for point in by_week['points']), 3)
        self.assertTrue(all(point['start'].weekday() == 0 for point in by_week['points']))

    def test_pagination(self):
        for index in range(30):
            self.lead(f'Lead {index}')
        first = self.get('report-leads', **SEPT).data
        second = self.get('report-leads', **SEPT, page=2).data
        self.assertEqual((first['count'], len(first['results']), len(second['results'])), (30, 25, 5))


class WorkReportTests(ReportTestCase):
    def test_works_by_stage_and_staff_with_their_confirmed_amounts(self):
        self.work('Feasible', amount=Decimal('150000'), stage=WorkStage.FEASIBILITY, assigned_to=self.hrithik)
        self.work('Done', amount=Decimal('190000'), stage=WorkStage.COMPLETED, assigned_to=self.hrithik)
        self.work('July', day='2026-07-15', amount=Decimal('999999'))
        # A later plan price change never alters a Work's confirmed amount.
        SolarPlan.objects.filter(pk=self.plan.pk).update(amount=Decimal('500000'))

        summary = self.get('report-works-summary', **SEPT).data

        self.assertEqual(
            summary['totals'], {'created': 2, 'amount': '340000.00', 'completed': 1, 'completed_amount': '190000.00'},
        )
        stages = {row['value']: (row['count'], row['amount']) for row in summary['by_stage']}
        self.assertEqual((stages['FEASIBILITY'], stages['COMPLETED'], stages['STRUCTURE']), ((1, '150000.00'), (1, '190000.00'), (0, '0.00')))
        self.assertEqual(list(stages), WorkStage.values)
        self.assertEqual(
            [(row['name'], row['count'], row['amount'], row['completed']) for row in summary['by_staff']],
            [('Hrithik', 2, '340000.00', 1)],
        )
        rows = self.get('report-works', **SEPT, stage='FEASIBILITY').data['results']
        self.assertEqual([(row['customer_name'], row['amount'], row['latest_activity']) for row in rows], [('Feasible', '150000.00', None)])

    def test_staff_need_the_work_module(self):
        self.give_staff('access_reports', *LEADS)
        self.client.force_authenticate(self.danish)
        self.assertEqual(self.client.get(reverse('report-works')).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(reverse('report-works-summary')).status_code, status.HTTP_403_FORBIDDEN)


class ActivityReportTests(ReportTestCase):
    def setUp(self):
        self.done_work = self.work('Suresh P')
        self.lead_with_log = self.lead('Arun Kumar')
        common = {'work': self.done_work, 'type': 'FOLLOW_UP', 'created_by': self.admin}
        # Due in August, outside the September period, and still pending: overdue.
        self.overdue = Activity.objects.create(
            description='Overdue.', due_date='2026-08-20', assigned_to=self.danish, **common,
        )
        self.completed = Activity.objects.create(
            description='Done.', status=ActivityStatus.COMPLETED, completed_at=at('2026-09-20'), completed_by=self.hrithik,
            assigned_to=self.danish, **common,
        )
        self.log = Activity.objects.create(
            lead=self.lead_with_log, type='NOTE', description='Lead note.', created_by=self.hrithik,
        )
        Activity.objects.update(created_at=at('2026-09-15'))

    def test_counts_keep_completed_activities_and_never_treat_a_lead_log_as_a_follow_up(self):
        totals = self.get('report-activities-summary', **SEPT).data['totals']
        self.assertEqual(
            totals,
            {'total': 3, 'on_leads': 1, 'on_works': 2, 'pending': 1, 'completed': 1, 'overdue': 1,
             'due_in_period': 0, 'completed_in_period': 1},
        )
        rows = {row['description']: row for row in self.get('report-activities', **SEPT).data['results']}
        self.assertEqual((rows['Lead note.']['status'], rows['Done.']['status'], rows['Overdue.']['overdue']), (None, 'COMPLETED', True))
        self.assertEqual(rows['Done.']['completed_by_name'], 'Hrithik')

    def test_filters(self):
        def described(**params):
            return sorted(row['description'] for row in self.get('report-activities', **SEPT, **params).data['results'])

        self.assertEqual(described(status='COMPLETED'), ['Done.'])
        self.assertEqual(described(overdue='true'), ['Overdue.'])
        self.assertEqual(described(record='lead'), ['Lead note.'])
        self.assertEqual(described(type='NOTE'), ['Lead note.'])
        self.assertEqual(described(assigned_to=self.danish.pk), ['Done.', 'Overdue.'])
        self.assertEqual(described(work=self.done_work.pk), ['Done.', 'Overdue.'])
        # By completion date: only what was completed in the period.
        self.assertEqual(described(date_field='completed'), ['Done.'])
        self.assertEqual(self.get('report-activities', date_field='completed', date_from='2026-09-21').data['count'], 0)
        staff = {row['name']: (row['count'], row['pending'], row['completed']) for row in self.get('report-activities-summary', **SEPT).data['by_staff'] if row['name']}
        self.assertEqual(staff, {'Danish': (2, 1, 1)})


class ExportTests(ReportTestCase):
    def test_exports_hold_exactly_the_filtered_rows(self):
        self.work('Feasible', stage=WorkStage.FEASIBILITY)
        self.work('Structure', stage=WorkStage.STRUCTURE)
        self.work('July', day='2026-07-15', stage=WorkStage.FEASIBILITY)

        response = self.get('report-works', **SEPT, stage='FEASIBILITY', export='csv')
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertIn('attachment; filename="work-report-', response['Content-Disposition'])
        self.assertEqual(rows[0][:4], ['Work ID', 'Lead ID', 'Customer', 'Stage'])
        self.assertEqual([(row[2], row[3], row[6]) for row in rows[1:]], [('Feasible', 'Feasibility', '185000.00')])
        self.assertEqual(rows[1][-1], 'No activity yet')

        workbook = self.get('report-works', **SEPT, export='xlsx').content
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        main = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        cells = [
            [cell.findtext(f'{main}v') or ''.join(cell.itertext()) for cell in row]
            for row in sheet.iter(f'{main}row')
        ]
        self.assertEqual(len(cells), 3)  # the header and the two September Works
        self.assertEqual(sorted(row[2] for row in cells[1:]), ['Feasible', 'Structure'])
        self.assertEqual(cells[1][6], '185000.00')  # a number cell, not text

    def test_staff_exports_hold_only_their_own_leads(self):
        self.lead('Mine', assigned_to=self.danish)
        self.lead('Not mine', assigned_to=self.hrithik)
        self.give_staff('access_reports', *LEADS)

        response = self.get('report-leads', self.danish, **SEPT, export='csv')
        names = [row[1] for row in csv.reader(io.StringIO(response.content.decode('utf-8-sig')))][1:]

        self.assertEqual(names, ['Mine'])


class PermissionTests(ReportTestCase):
    def test_reports_need_the_reports_module(self):
        self.give_staff('access_work', *LEADS)
        self.client.force_authenticate(self.danish)
        for name in ('report-leads', 'report-works', 'report-activities', 'report-staff', 'report-history'):
            self.assertEqual(self.client.get(reverse(name)).status_code, status.HTTP_403_FORBIDDEN, name)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('report-leads')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_see_only_their_own_leads_whatever_they_ask_for(self):
        self.lead('Mine', assigned_to=self.danish)
        self.lead('Not mine', assigned_to=self.hrithik)
        self.give_staff('access_reports', *LEADS)

        asked_for_other = self.get('report-leads', self.danish, **SEPT, assigned_to=self.hrithik.pk).data
        summary = self.get('report-leads-summary', self.danish, **SEPT).data
        admin = self.get('report-leads', **SEPT).data

        self.assertEqual(asked_for_other['count'], 0)
        self.assertEqual(summary['totals']['created'], 1)
        self.assertEqual(admin['count'], 2)


class StaffReportTests(ReportTestCase):
    def test_what_each_user_was_assigned_and_did_with_no_ranking(self):
        self.lead('Assigned', assigned_to=self.danish, created_by=self.hrithik)
        work = self.work('Work', assigned_to=self.danish, by=self.hrithik)
        activity = Activity.objects.create(
            work=work, type='FOLLOW_UP', description='x', assigned_to=self.danish, created_by=self.hrithik,
            status=ActivityStatus.COMPLETED, completed_at=at('2026-09-12'), completed_by=self.danish,
        )
        Activity.objects.filter(pk=activity.pk).update(created_at=at('2026-09-12'))

        rows = {(row['user'] or {}).get('name'): row for row in self.get('report-staff', **SEPT).data['results']}

        self.assertEqual([name for name in rows], ['Admin', 'Danish', 'Hrithik'])  # by name, everyone active
        danish, hrithik = rows['Danish'], rows['Hrithik']
        self.assertEqual(
            (danish['leads_assigned'], danish['works_assigned'], danish['activities_assigned'], danish['completed'],
             danish['follow_ups_completed']),
            (2, 1, 1, 1, 1),  # both leads: the converted Work's lead is assigned to Danish too
        )
        self.assertEqual((hrithik['leads_added'], hrithik['works_converted'], hrithik['activities_added']), (1, 1, 1))
        self.assertNotIn('score', danish)

    def test_staff_see_only_people_in_their_own_records(self):
        self.lead('Mine', assigned_to=self.danish, created_by=self.admin)
        self.lead('Not mine', assigned_to=self.hrithik, created_by=self.hrithik)
        self.give_staff('access_reports', *LEADS)

        rows = self.get('report-staff', self.danish, **SEPT).data['results']

        self.assertEqual(sorted(row['user']['name'] for row in rows), ['Admin', 'Danish'])


class HistoryReportTests(ReportTestCase):
    def test_only_recorded_events_never_one_for_a_record_without_activity(self):
        quiet = self.lead('Quiet', created_by=self.danish)
        work = self.work('Busy', by=self.hrithik)
        activity = Activity.objects.create(work=work, type='SITE_VISIT', description='Roof survey.', created_by=self.danish)
        Activity.objects.filter(pk=activity.pk).update(created_at=at('2026-09-11'))

        events = self.get('report-history', **SEPT).data['results']
        kinds = sorted((event['kind'], (event['lead'] or event['work'])['customer_name']) for event in events)

        # The quiet lead appears only as added; the Busy lead as added and converted; one real activity.
        self.assertEqual(kinds, [
            ('activity_added', 'Busy'), ('lead_created', 'Busy'), ('lead_created', 'Quiet'), ('work_created', 'Busy'),
        ])
        self.assertEqual(next(e for e in events if e['lead'] and e['lead']['id'] == quiet.pk)['user']['name'], 'Danish')
        only_activities = self.get('report-history', **SEPT, kind='activity_added', user=self.danish.pk).data['results']
        self.assertEqual([event['activity']['description'] for event in only_activities], ['Roof survey.'])

        response = self.get('report-history', **SEPT, record='works', export='csv')
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual(rows[0][:4], ['Date', 'Time', 'User', 'Event'])
        self.assertEqual(sorted(row[3] for row in rows[1:]), ['Activity added', 'Lead converted to Work'])
        self.assertEqual(rows[1][0:2], ['2026-09-11', '11:30:00'])  # 06:00 UTC is 11:30 in Asia/Kolkata

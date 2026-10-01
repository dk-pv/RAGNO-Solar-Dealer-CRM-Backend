from datetime import datetime, timezone
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, LeadConflict, LeadStatus, SolarPlan

from .models import Work, WorkStage

PASSWORD = 'Solar-Panel-2026'


def convert_lead(plan, user, **fields):
    """A Superhot lead converted by `user`: the only way a Work is created."""
    values = {'name': 'Asha Menon', 'phone': '9876543210', 'district': 'Ernakulam', 'amount': plan.amount, **fields}
    lead = Lead.objects.create(plan=plan, created_by=user, status=LeadStatus.SUPERHOT, **values)
    lead.convert(user)
    return lead.work


class WorkTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Priya Nair')
        # The default plans come from the leads migrations.
        cls.plan = SolarPlan.objects.get(capacity=5)

    def give_staff_work_access(self):
        Role.objects.get(pk=Role.STAFF).permissions.add(Permission.objects.get(codename='access_work'))
        return User.objects.get(pk=self.staff.pk)


class ConversionTests(WorkTestCase):
    def test_converting_a_lead_creates_its_work_with_the_customer_plan_and_confirmed_amount(self):
        work = convert_lead(
            self.plan, self.admin, name='Ravi Kumar', phone='9876500001', area='Kakkanad', amount=Decimal('185000'),
            assigned_to=self.staff,
        )

        self.assertEqual(work.lead.status, LeadStatus.WON)
        self.assertEqual(
            (work.customer_name, work.phone, work.area, work.plan, work.amount, work.assigned_to, work.created_by),
            ('Ravi Kumar', '9876500001', 'Kakkanad', self.plan, Decimal('185000'), self.staff, self.admin),
        )
        self.assertEqual(work.stage, WorkStage.LOAN_DOCUMENTS)

    def test_the_confirmed_amount_never_follows_a_plan_price_change(self):
        work = convert_lead(self.plan, self.admin, amount=Decimal('185000'))

        SolarPlan.objects.filter(pk=self.plan.pk).update(amount=Decimal('240000'))
        self.client.force_authenticate(self.admin)

        self.assertEqual(self.client.get(reverse('work-detail', args=[work.pk])).data['amount'], '185000.00')

    def test_a_lead_is_converted_only_once(self):
        work = convert_lead(self.plan, self.admin)

        with self.assertRaisesMessage(LeadConflict, 'This lead has already been converted.'):
            work.lead.convert(self.admin)
        self.assertEqual(Work.objects.count(), 1)


class WorkApiTests(WorkTestCase):
    def test_works_need_the_work_module(self):
        url = reverse('work-list')
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.give_staff_work_access())
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_search_filters_and_ordering(self):
        first = convert_lead(self.plan, self.admin, name='Anu Joseph', phone='9876500001', amount=Decimal('150000'))
        second = convert_lead(self.plan, self.admin, name='Biju Paul', phone='9876500002', amount=Decimal('190000'))
        Work.objects.filter(pk=second.pk).update(stage=WorkStage.FEASIBILITY, assigned_to=self.staff)
        self.client.force_authenticate(self.admin)

        def listed(**params):
            return [work['id'] for work in self.client.get(reverse('work-list'), params).data['results']]

        self.assertEqual(listed(search='anu'), [first.pk])
        self.assertEqual(listed(search='+91 98765 00002'), [second.pk])
        self.assertEqual(listed(search=f'#{first.pk}'), [first.pk])
        self.assertEqual(listed(stage=WorkStage.FEASIBILITY), [second.pk])
        self.assertEqual(listed(assigned_to=self.staff.pk), [second.pk])
        self.assertEqual(listed(ordering='amount'), [first.pk, second.pk])
        self.assertEqual(listed(ordering='-amount'), [second.pk, first.pk])
        self.assertEqual(self.client.get(reverse('work-list'), {'stage': 'INSTALLED'}).status_code, 400)

    def test_created_date_filters_and_sorting_by_stage_in_pipeline_order(self):
        early = convert_lead(self.plan, self.admin, phone='9876500001')
        late = convert_lead(self.plan, self.admin, phone='9876500002')
        Work.objects.filter(pk=early.pk).update(
            stage=WorkStage.COMPLETED, created_at=datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        )
        self.client.force_authenticate(self.admin)

        def listed(**params):
            return [work['id'] for work in self.client.get(reverse('work-list'), params).data['results']]

        self.assertEqual(listed(created_before='2026-01-31'), [early.pk])
        self.assertEqual(listed(created_after='2026-02-01'), [late.pk])
        self.assertEqual(listed(ordering='stage'), [late.pk, early.pk])
        self.assertEqual(listed(ordering='-stage'), [early.pk, late.pk])

    def test_a_lead_converted_through_the_api_reports_its_work(self):
        lead = Lead.objects.create(
            plan=self.plan, created_by=self.admin, status=LeadStatus.SUPERHOT, name='Asha Menon', phone='9876543210',
            district='Ernakulam', amount=self.plan.amount,
        )
        self.client.force_authenticate(self.admin)

        converted = self.client.post(reverse('lead-convert', args=[lead.pk]))
        listed = self.client.get(reverse('lead-list')).data['results']

        work_id = Lead.objects.get(pk=lead.pk).work.pk
        self.assertEqual(converted.data['work'], work_id)
        self.assertEqual([(row['id'], row['work']) for row in listed], [(lead.pk, work_id)])
        self.assertEqual([row['id'] for row in self.client.get(reverse('work-list')).data['results']], [work_id])

    def test_summary_counts_and_totals_every_stage_from_confirmed_amounts(self):
        convert_lead(self.plan, self.admin, phone='9876500001', amount=Decimal('150000'))
        convert_lead(self.plan, self.admin, phone='9876500002', amount=Decimal('190000'))
        moved = convert_lead(self.plan, self.admin, phone='9876500003', amount=Decimal('210000'))
        Work.objects.filter(pk=moved.pk).update(stage=WorkStage.COMPLETED)
        SolarPlan.objects.filter(pk=self.plan.pk).update(amount=Decimal('999999'))
        self.client.force_authenticate(self.admin)

        summary = {row['stage']: row for row in self.client.get(reverse('work-summary')).data}

        self.assertEqual(list(summary), WorkStage.values)
        self.assertEqual((summary['LOAN_DOCUMENTS']['count'], summary['LOAN_DOCUMENTS']['total_amount']), (2, '340000.00'))
        self.assertEqual((summary['COMPLETED']['count'], summary['COMPLETED']['total_amount']), (1, '210000.00'))
        self.assertEqual((summary['FEASIBILITY']['count'], summary['FEASIBILITY']['total_amount']), (0, '0.00'))
        searched = {row['stage']: row['count'] for row in self.client.get(reverse('work-summary'), {'search': '9876500003'}).data}
        self.assertEqual((searched['COMPLETED'], searched['LOAN_DOCUMENTS']), (1, 0))

    def test_a_work_moves_to_any_stage_in_either_direction_and_only_pipeline_fields_change(self):
        work = convert_lead(self.plan, self.admin, amount=Decimal('185000'))
        url = reverse('work-detail', args=[work.pk])
        self.client.force_authenticate(self.give_staff_work_access())

        forward = self.client.patch(url, {'stage': WorkStage.COMPLETED}, format='json')
        back = self.client.patch(url, {'stage': WorkStage.FEASIBILITY, 'due_date': '2026-10-15'}, format='json')
        tampered = self.client.patch(url, {'amount': '1', 'customer_name': 'Someone else'}, format='json')
        invalid = self.client.patch(url, {'stage': 'INSTALLED'}, format='json')

        self.assertEqual((forward.status_code, forward.data['stage']), (200, WorkStage.COMPLETED))
        self.assertEqual((back.data['stage'], back.data['due_date']), (WorkStage.FEASIBILITY, '2026-10-15'))
        self.assertEqual((tampered.data['amount'], tampered.data['customer_name']), ('185000.00', 'Asha Menon'))
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_without_the_work_module_cannot_move_a_work(self):
        work = convert_lead(self.plan, self.admin)
        self.client.force_authenticate(self.staff)

        response = self.client.patch(reverse('work-detail', args=[work.pk]), {'stage': WorkStage.COMPLETED}, format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Work.objects.get(pk=work.pk).stage, WorkStage.LOAN_DOCUMENTS)

    def test_works_are_assigned_to_active_users_only_and_are_never_created_or_deleted_through_the_api(self):
        work = convert_lead(self.plan, self.admin)
        inactive = User.objects.create_user(email='left@example.com', password=PASSWORD, name='Left', is_active=False)
        self.client.force_authenticate(self.admin)
        url = reverse('work-detail', args=[work.pk])

        assign = self.client.patch(url, {'assigned_to': self.staff.pk}, format='json')
        assign_inactive = self.client.patch(url, {'assigned_to': inactive.pk}, format='json')

        self.assertEqual(assign.data['assigned_to_name'], 'Priya Nair')
        self.assertEqual(assign_inactive.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(reverse('work-list'), {}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(self.client.put(url, {}, format='json').status_code, 405)
        self.assertEqual({user['name'] for user in self.client.get(reverse('work-assignees')).data}, {'Admin', 'Priya Nair'})

    def test_a_pinned_work_is_listed_first_for_everyone(self):
        older = convert_lead(self.plan, self.admin, phone='9876500001')
        newer = convert_lead(self.plan, self.admin, phone='9876500002')
        self.client.force_authenticate(self.give_staff_work_access())

        pinned = self.client.patch(reverse('work-detail', args=[older.pk]), {'is_pinned': True}, format='json')

        self.assertEqual((pinned.status_code, pinned.data['is_pinned']), (200, True))
        self.client.force_authenticate(self.admin)
        self.assertEqual([row['id'] for row in self.client.get(reverse('work-list')).data['results']], [older.pk, newer.pk])


class WorkActivityTests(WorkTestCase):
    """A Work's activities live in the CRM's one activity log (/api/activities/?work=) and follow the Work module."""

    def setUp(self):
        self.work = convert_lead(self.plan, self.admin, amount=Decimal('185000'))

    def add(self, **fields):
        body = {'work': self.work.pk, 'type': 'FOLLOW_UP', 'description': 'Site feasibility follow-up.', **fields}
        return self.client.post(reverse('activity-list'), body, format='json')

    def listed(self):
        response = self.client.get(reverse('activity-list'), {'work': self.work.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [(row['description'], row['status']) for row in response.data['results']]

    def test_add_edit_complete_and_reopen_a_follow_up(self):
        self.client.force_authenticate(self.give_staff_work_access())

        created = self.add(assigned_to=self.staff.pk, due_date='2026-09-12')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(
            (created.data['status'], created.data['assigned_to_name'], created.data['created_by_name'], created.data['lead']),
            ('PENDING', 'Priya Nair', 'Priya Nair', None),
        )
        url = reverse('activity-detail', args=[created.data['id']])

        edited = self.client.patch(url, {'description': 'Roof survey booked.', 'due_date': '2026-09-15'}, format='json')
        completed = self.client.patch(url, {'status': 'COMPLETED'}, format='json')
        self.assertEqual((edited.data['description'], edited.data['due_date']), ('Roof survey booked.', '2026-09-15'))
        self.assertEqual((completed.data['status'], completed.data['completed_by_name']), ('COMPLETED', 'Priya Nair'))
        self.assertIsNotNone(completed.data['completed_at'])
        # Editing changes the one activity; completing keeps it in the Work's history.
        self.assertEqual(self.listed(), [('Roof survey booked.', 'COMPLETED')])

        reopened = self.client.patch(url, {'status': 'PENDING'}, format='json')
        self.assertEqual((reopened.data['completed_at'], reopened.data['completed_by_name']), (None, None))

    def test_pending_come_first_soonest_due_first_and_the_work_reports_its_activities(self):
        self.client.force_authenticate(self.admin)
        self.add(description='Done.', status='COMPLETED')
        self.add(description='Later.', due_date='2026-10-20')
        self.add(description='Sooner.', due_date='2026-10-05')
        self.add(description='Undated.')

        self.assertEqual(
            self.listed(),
            [('Sooner.', 'PENDING'), ('Later.', 'PENDING'), ('Undated.', 'PENDING'), ('Done.', 'COMPLETED')],
        )
        work = self.client.get(reverse('work-detail', args=[self.work.pk])).data
        self.assertEqual(
            (work['activity_count'], work['pending_activity_count'], work['next_activity_due']), (4, 3, '2026-10-05'),
        )
        listed = self.client.get(reverse('work-list')).data['results'][0]
        self.assertEqual((listed['activity_count'], listed['pending_activity_count']), (4, 3))
        # The stage totals still count each Work's amount once, however many activities it has.
        summary = {row['stage']: row['total_amount'] for row in self.client.get(reverse('work-summary')).data}
        self.assertEqual(summary['LOAN_DOCUMENTS'], '185000.00')

    def test_work_activities_need_the_work_module(self):
        self.client.force_authenticate(self.admin)
        activity = self.add().data['id']
        url = reverse('activity-detail', args=[activity])
        # Staff with the Leads module but not the Work module.
        Role.objects.get(pk=Role.STAFF).permissions.add(*Permission.objects.filter(
            content_type__app_label='leads', codename__in=['view_lead', 'change_lead'],
        ))
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))

        self.assertEqual(self.client.get(reverse('activity-list'), {'work': self.work.pk}).status_code, 403)
        self.assertEqual(self.add().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.patch(url, {'status': 'COMPLETED'}, format='json').status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('activity-list'), {'work': self.work.pk}).status_code, 401)

    def test_the_work_module_alone_gives_no_access_to_lead_activities(self):
        lead = Lead.objects.create(
            plan=self.plan, created_by=self.admin, assigned_to=self.staff, name='Open Lead', phone='9876500009',
            district='Ernakulam', amount=self.plan.amount,
        )
        self.client.force_authenticate(self.admin)
        on_lead = self.client.post(
            reverse('activity-list'), {'lead': lead.pk, 'type': 'NOTE', 'description': 'Lead note.'}, format='json',
        ).data['id']
        self.client.force_authenticate(self.give_staff_work_access())

        self.assertEqual(self.client.get(reverse('activity-list'), {'lead': lead.pk}).status_code, 403)
        self.assertEqual(self.client.get(reverse('activity-detail', args=[on_lead])).status_code, 404)
        # A lead smuggled in beside the Work isn't accepted either.
        self.assertEqual(self.add(lead=lead.pk).status_code, status.HTTP_400_BAD_REQUEST)

    def test_activities_belong_to_one_work_for_good_and_are_never_deleted(self):
        other = convert_lead(self.plan, self.admin, phone='9876500002')
        inactive = User.objects.create_user(email='left@example.com', password=PASSWORD, name='Left', is_active=False)
        self.client.force_authenticate(self.admin)
        activity = self.add().data['id']
        url = reverse('activity-detail', args=[activity])

        self.assertEqual(self.client.patch(url, {'work': other.pk}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('assigned_to', self.add(assigned_to=inactive.pk).data)
        missing = self.client.post(reverse('activity-list'), {'type': 'NOTE', 'description': 'x'}, format='json')
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(reverse('activity-list'), {'work': 'x'}).status_code, 400)
        self.assertEqual(self.listed(), [('Site feasibility follow-up.', 'PENDING')])


class WorkActivitiesPageTests(WorkTestCase):
    """GET /api/activities/works/: every Work's activities on one page, each Work's together."""

    def setUp(self):
        self.first = convert_lead(self.plan, self.admin, name='Arun Kumar', phone='9876500001')
        self.second = convert_lead(self.plan, self.admin, name='Neha Thomas', phone='9876500002')
        self.client.force_authenticate(self.admin)
        # Added interleaved, so the grouping can't come from the order they were added in.
        for work, description, extra in [
            (self.first, 'Collect KSEB documents.', {'due_date': '2026-09-12', 'assigned_to': self.staff.pk}),
            (self.second, 'Structure visit.', {'due_date': '2026-09-16'}),
            (self.first, 'Site feasibility.', {'status': 'COMPLETED'}),
            (self.second, 'Site measurement.', {'status': 'COMPLETED'}),
            (self.first, 'Customer call.', {'due_date': '2026-09-15'}),
        ]:
            body = {'work': work.pk, 'type': 'FOLLOW_UP', 'description': description, **extra}
            self.assertEqual(self.client.post(reverse('activity-list'), body, format='json').status_code, 201)

    def listed(self, **params):
        response = self.client.get(reverse('activity-works'), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [(row['work'], row['description']) for row in response.data['results']]

    def test_every_activity_is_listed_with_each_works_activities_together(self):
        first, second = self.first.pk, self.second.pk
        self.assertEqual(self.listed(), [
            (second, 'Structure visit.'), (second, 'Site measurement.'),
            (first, 'Collect KSEB documents.'), (first, 'Customer call.'), (first, 'Site feasibility.'),
        ])
        self.assertEqual([work for work, _ in self.listed(ordering='work')], [first] * 3 + [second] * 2)
        self.assertEqual(
            [description for _, description in self.listed(ordering='due_date')][:3],
            ['Collect KSEB documents.', 'Customer call.', 'Structure visit.'],
        )
        row = self.client.get(reverse('activity-works')).data['results'][0]
        self.assertEqual(
            row['work_summary'],
            {'id': second, 'customer_name': 'Neha Thomas', 'country_code': '91', 'phone': '9876500002',
             'plan_name': self.plan.name, 'stage': WorkStage.LOAN_DOCUMENTS},
        )

    def test_search_and_filters(self):
        first = self.first.pk
        self.assertEqual({work for work, _ in self.listed(search='arun')}, {first})
        self.assertEqual(len(self.listed(search=f'#{first}')), 3)
        self.assertEqual(self.listed(search='measurement'), [(self.second.pk, 'Site measurement.')])
        self.assertEqual(self.listed(search='priya'), [(first, 'Collect KSEB documents.')])
        self.assertEqual(len(self.listed(status='COMPLETED')), 2)
        self.assertEqual(self.listed(assigned_to=self.staff.pk), [(first, 'Collect KSEB documents.')])
        self.assertEqual(len(self.listed(work=first)), 3)
        self.assertEqual(len(self.listed(due_after='2026-09-13', due_before='2026-09-15')), 1)
        self.assertEqual(self.client.get(reverse('activity-works'), {'status': 'DONE'}).status_code, 400)

    def test_lead_activities_are_never_listed(self):
        lead = Lead.objects.create(
            plan=self.plan, created_by=self.admin, name='Open Lead', phone='9876500009', district='Ernakulam',
            amount=self.plan.amount,
        )
        self.client.post(reverse('activity-list'), {'lead': lead.pk, 'type': 'NOTE', 'description': 'Lead.'}, format='json')
        self.assertNotIn('Lead.', [description for _, description in self.listed()])

    def test_the_page_needs_the_work_and_activities_modules(self):
        staff_role = Role.objects.get(pk=Role.STAFF)
        self.client.force_authenticate(self.give_staff_work_access())
        self.assertEqual(self.client.get(reverse('activity-works')).status_code, status.HTTP_403_FORBIDDEN)
        staff_role.permissions.add(Permission.objects.get(codename='access_activities'))
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get(reverse('activity-works')).status_code, status.HTTP_200_OK)
        staff_role.permissions.remove(Permission.objects.get(codename='access_work'))
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get(reverse('activity-works')).status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('activity-works')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_number_of_queries_does_not_grow_with_the_rows(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get(reverse('activity-works'))
            return len(captured)

        before = queries()
        third = convert_lead(self.plan, self.admin, name='Third', phone='9876500003')
        for _ in range(4):
            self.client.post(
                reverse('activity-list'), {'work': third.pk, 'type': 'NOTE', 'description': 'More.'}, format='json',
            )
        self.assertEqual(queries(), before)

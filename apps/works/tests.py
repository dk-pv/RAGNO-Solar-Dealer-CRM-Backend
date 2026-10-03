from datetime import datetime, timezone
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, LeadConflict, LeadStatus, SolarPlan

from .models import Work, WorkStage

PASSWORD = 'Solar-Panel-2026'


def convert_lead(plan, user, **fields):
    """A Superhot lead moved to Won and converted by `user`: the only way a Work is created."""
    values = {'name': 'Asha Menon', 'phone': '9876543210', 'district': 'Ernakulam', 'amount': plan.amount, **fields}
    lead = Lead.objects.create(plan=plan, created_by=user, status=LeadStatus.SUPERHOT, **values)
    lead.move_to(LeadStatus.WON)
    lead.convert(user)
    return lead.work


class WorkTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Priya Nair')
        # The 5 kW plan (200000) every database starts with (leads migration 0002).
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

        self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': LeadStatus.WON}, format='json')
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

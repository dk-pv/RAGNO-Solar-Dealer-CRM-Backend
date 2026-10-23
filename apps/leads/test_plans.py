"""Solar plans as admins manage them (Settings → Solar Plans, /api/plans/): adding, editing, deactivating and
reactivating, with the backend's checks. A plan's price is the default for new leads only: every lead, Work and offer
keeps its own amount."""

import importlib
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User

from .models import Lead, LeadStatus, SolarPlan

PASSWORD = 'Solar-Panel-2026'


class PlanManagementTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')
        # Even the Leads module, which lists the plans, doesn't let staff manage them.
        Role.objects.get(pk=Role.STAFF).permissions.add(*Permission.objects.filter(
            content_type__app_label='leads', codename__in=['view_lead', 'add_lead', 'change_lead'],
        ))
        cls.five = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def add(self, **fields):
        return self.client.post(reverse('plan-list'), fields, format='json')

    def change(self, plan, **fields):
        return self.client.patch(reverse('plan-detail', args=[plan.pk]), fields, format='json')

    def listed(self):
        return [(plan['name'], plan['capacity'], plan['amount'], plan['is_active']) for plan in self.client.get(reverse('plan-list')).data]

    def converted(self, amount):
        """A lead on the 5 kW plan at `amount`, converted: its Work."""
        lead = Lead.objects.create(
            name='Ravi Kumar', phone='9876500001', plan=self.five, amount=amount, created_by=self.admin,
            status=LeadStatus.SUPERHOT,
        )
        lead.move_to(LeadStatus.WON)
        lead.convert(self.admin)
        return lead.work

    def test_the_five_starting_plans_exist_once_and_rerunning_their_migrations_adds_nothing(self):
        starting = [
            ('3 kW', '3.00', '150000.00', True), ('4 kW', '4.00', '180000.00', True), ('5 kW', '5.00', '200000.00', True),
            ('8 kW', '8.00', '245000.00', True), ('10 kW', '10.00', '289000.00', True),
        ]
        self.assertEqual(self.listed(), starting)
        # An admin's price change survives the seed migrations running again, and they add no second copy of any plan.
        SolarPlan.objects.filter(capacity=3).update(amount=Decimal('155000'))
        importlib.import_module('apps.leads.migrations.0002_default_solar_plans').create_plans(apps, None)
        importlib.import_module('apps.leads.migrations.0006_add_4_kw_plan').create_plan(apps, None)

        self.assertEqual(self.listed(), [('3 kW', '3.00', '155000.00', True), *starting[1:]])

    def test_an_admin_adds_a_plan_and_changes_its_name_capacity_and_price(self):
        created = self.add(name='6 kW', capacity='6', amount='215000')

        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(
            {key: created.data[key] for key in ('name', 'capacity', 'amount', 'is_active')},
            {'name': '6 kW', 'capacity': '6.00', 'amount': '215000.00', 'is_active': True},
        )
        self.assertEqual(self.listed()[3], ('6 kW', '6.00', '215000.00', True))  # in capacity order, after 5 kW
        plan = SolarPlan.objects.get(pk=created.data['id'])
        # Not used by any lead yet, so even its capacity can still change.
        changed = self.change(plan, name='6.5 kW', capacity='6.5', amount='219999.50')
        self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.data)
        plan.refresh_from_db()
        self.assertEqual((plan.name, plan.capacity, plan.amount), ('6.5 kW', Decimal('6.5'), Decimal('219999.50')))
        # Saving a plan with its own name and capacity isn't a duplicate of itself.
        self.assertEqual(self.change(plan, name='6.5 kW', capacity='6.50').status_code, status.HTTP_200_OK)
        self.assertEqual(SolarPlan.objects.count(), 6)

    def test_staff_and_signed_out_users_cant_manage_plans(self):
        detail = reverse('plan-detail', args=[self.five.pk])
        body = {'name': '6 kW', 'capacity': '6', 'amount': '215000'}
        for user, refused in ((self.staff, status.HTTP_403_FORBIDDEN), (None, status.HTTP_401_UNAUTHORIZED)):
            self.client.force_authenticate(user)
            with self.subTest(user=str(user)):
                self.assertEqual(self.client.post(reverse('plan-list'), body, format='json').status_code, refused)
                self.assertEqual(self.client.patch(detail, {'amount': '1'}, format='json').status_code, refused)
                self.assertEqual(self.client.patch(detail, {'is_active': False}, format='json').status_code, refused)
                self.assertEqual(self.client.put(detail, body, format='json').status_code, refused)
                self.assertEqual(self.client.delete(detail).status_code, refused)
        self.five.refresh_from_db()
        self.assertEqual((self.five.amount, self.five.is_active, SolarPlan.objects.count()), (Decimal('200000'), True, 5))
        # Listing them is still theirs (the lead form needs it), with the Leads module.
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(reverse('plan-list')).status_code, status.HTTP_200_OK)

    def test_plans_are_retired_by_deactivating_them_not_deleted(self):
        self.assertEqual(self.client.delete(reverse('plan-detail', args=[self.five.pk])).status_code, 405)
        self.assertTrue(SolarPlan.objects.filter(pk=self.five.pk).exists())

    def test_a_duplicate_capacity_or_name_is_refused(self):
        for fields, field, message in [
            ({'name': 'Five', 'capacity': '5'}, 'capacity', 'A plan with this capacity already exists.'),
            ({'name': 'Five', 'capacity': '5.00'}, 'capacity', 'A plan with this capacity already exists.'),
            ({'name': '5 kW', 'capacity': '7'}, 'name', 'A plan with this name already exists.'),
            ({'name': '5 KW', 'capacity': '7'}, 'name', 'A plan with this name already exists.'),
        ]:
            with self.subTest(fields=fields):
                refused = self.add(amount='199000', **fields)
                self.assertEqual((refused.status_code, refused.data[field]), (status.HTTP_400_BAD_REQUEST, [message]))
        eight = SolarPlan.objects.get(capacity=8)
        self.assertIn('capacity', self.change(eight, capacity='10').data)
        self.assertIn('name', self.change(eight, name='10 kW').data)
        self.assertEqual(SolarPlan.objects.count(), 5)
        self.assertEqual(SolarPlan.objects.get(pk=eight.pk).capacity, Decimal('8'))

    def test_invalid_prices_and_capacities_are_refused(self):
        for amount in ['0', '0.00', '-150000', 'abc', '1.234', '', None]:
            with self.subTest(amount=amount):
                self.assertIn('amount', self.add(name='6 kW', capacity='6', amount=amount).data)
                self.assertIn('amount', self.change(self.five, amount=amount).data)
        for capacity in ['0', '-3', '1000', 'x', None]:
            with self.subTest(capacity=capacity):
                self.assertIn('capacity', self.add(name='6 kW', capacity=capacity, amount='215000').data)
        self.assertIn('name', self.add(name='', capacity='6', amount='215000').data)
        self.assertIn('name', self.add(capacity='6', amount='215000').data)
        self.assertEqual(SolarPlan.objects.count(), 5)
        self.assertEqual(SolarPlan.objects.get(pk=self.five.pk).amount, Decimal('200000'))
        self.assertEqual(self.add(name='6 kW', capacity='6', amount='0.01').status_code, status.HTTP_201_CREATED)

    def test_a_price_change_is_for_new_leads_only(self):
        lead = Lead.objects.create(name='Asha Menon', phone='9876543210', plan=self.five, amount=Decimal('200000'), created_by=self.admin)
        work = self.converted(Decimal('195000'))
        offered = self.client.post(reverse('work-offer', args=[work.pk]), {'offer_amount': '190000'}, format='json')
        self.assertEqual(offered.status_code, status.HTTP_200_OK, offered.data)

        self.assertEqual(self.change(self.five, amount='210000').status_code, status.HTTP_200_OK)

        lead.refresh_from_db()
        work.refresh_from_db()
        self.assertEqual(lead.amount, Decimal('200000'))
        self.assertEqual((work.amount, work.offer_amount, work.effective_amount), (Decimal('195000'), Decimal('190000'), Decimal('190000')))
        self.assertEqual(self.client.get(reverse('lead-detail', args=[lead.pk])).data['amount'], '200000.00')
        self.assertEqual(self.client.get(reverse('work-detail', args=[work.pk])).data['effective_amount'], '190000.00')
        # A new lead on the plan, with no amount of its own, starts at the plan's current price.
        new = self.client.post(reverse('lead-list'), {'name': 'Neha', 'phone': '9876500002', 'plan': self.five.pk}, format='json')
        self.assertEqual((new.status_code, new.data['amount']), (status.HTTP_201_CREATED, '210000.00'))

    def test_a_plan_in_use_keeps_its_capacity(self):
        Lead.objects.create(name='Asha Menon', phone='9876543210', plan=self.five, amount=Decimal('200000'), created_by=self.admin)

        refused = self.change(self.five, capacity='5.5')

        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("can't change", refused.data['capacity'][0])
        self.assertEqual(SolarPlan.objects.get(pk=self.five.pk).capacity, Decimal('5'))
        # Its name and price still can.
        self.assertEqual(self.change(self.five, name='5 kW Standard', amount='205000').status_code, status.HTTP_200_OK)

    def test_an_inactive_plan_cant_be_chosen_for_a_new_lead_but_stays_where_it_is(self):
        lead = Lead.objects.create(name='Asha Menon', phone='9876543210', plan=self.five, amount=Decimal('200000'), created_by=self.admin)
        work = self.converted(Decimal('195000'))

        self.assertEqual(self.change(self.five, is_active=False).data['is_active'], False)

        refused = self.client.post(reverse('lead-list'), {'name': 'Neha', 'phone': '9876500002', 'plan': self.five.pk}, format='json')
        self.assertEqual((refused.status_code, refused.data['plan']), (status.HTTP_400_BAD_REQUEST, ['Select an active plan.']))
        # Leads and Works that have it keep it, with their own amounts, and the lead can still be edited.
        edited = self.client.patch(reverse('lead-detail', args=[lead.pk]), {'notes': 'Called back.'}, format='json')
        self.assertEqual((edited.status_code, edited.data['plan_name'], edited.data['amount']), (200, '5 kW', '200000.00'))
        shown = self.client.get(reverse('work-detail', args=[work.pk])).data
        self.assertEqual((shown['plan_name'], shown['amount']), ('5 kW', '195000.00'))
        # The list still has it, marked inactive, for the screens that show it on old records.
        self.assertIn(('5 kW', '5.00', '200000.00', False), self.listed())
        # Moved off it, the lead can't be moved back while it is inactive.
        four = SolarPlan.objects.get(capacity=4)
        self.assertEqual(self.client.patch(reverse('lead-detail', args=[lead.pk]), {'plan': four.pk}, format='json').status_code, 200)
        moved_back = self.client.patch(reverse('lead-detail', args=[lead.pk]), {'plan': self.five.pk}, format='json')
        self.assertEqual(moved_back.status_code, status.HTTP_400_BAD_REQUEST)
        # Reactivated, it can be chosen again.
        self.assertEqual(self.change(self.five, is_active=True).data['is_active'], True)
        added = self.client.post(reverse('lead-list'), {'name': 'Neha', 'phone': '9876500002', 'plan': self.five.pk}, format='json')
        self.assertEqual(added.status_code, status.HTTP_201_CREATED, added.data)

import csv
import io
from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity
from apps.notifications.models import Notification
from apps.works.models import Work, WorkStage

from .management.commands.seed_demo_leads import phone_for
from .models import Lead, LeadConflict, LeadStatus, SolarPlan

PASSWORD = 'Solar-Panel-2026'
S = LeadStatus


def grant(user, *codenames):
    user.role.permissions.add(*Permission.objects.filter(content_type__app_label='leads', codename__in=codenames))
    # Django caches permissions per instance, so hand back a fresh one.
    return User.objects.get(pk=user.pk)


def make_lead(plan, created_by, **fields):
    values = {'name': 'Asha Menon', 'phone': '9876543210', 'district': 'Ernakulam', 'amount': plan.amount, **fields}
    return Lead.objects.create(plan=plan, created_by=created_by, **values)


class LeadModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.plan = SolarPlan.objects.get(capacity=5)

    def test_pipeline_moves_forward_or_to_won_or_lost_from_any_open_stage(self):
        expected = {
            S.NEW: [S.INITIAL_CONTACT, S.HOT, S.SUPERHOT, S.WON, S.LOST],
            S.INITIAL_CONTACT: [S.HOT, S.SUPERHOT, S.WON, S.LOST],
            S.HOT: [S.SUPERHOT, S.WON, S.LOST],
            S.SUPERHOT: [S.WON, S.LOST],
            S.WON: [],
            S.LOST: [],
        }
        for current, allowed in expected.items():
            self.assertEqual(Lead(status=current).allowed_transitions(), allowed, current)

    def test_move_to_saves_forward_moves_and_refuses_going_back_and_reopening(self):
        lead = make_lead(self.plan, self.admin)
        lead.move_to(S.HOT)
        self.assertEqual(Lead.objects.get(pk=lead.pk).status, S.HOT)

        for target in (S.NEW, S.INITIAL_CONTACT, S.HOT, 'CONFIRMED'):
            with self.assertRaises(LeadConflict):
                lead.move_to(target)
        lead.move_to(S.LOST)
        with self.assertRaises(LeadConflict):
            lead.move_to(S.HOT)
        self.assertEqual(Lead.objects.get(pk=lead.pk).status, S.LOST)

    def test_only_won_leads_convert_and_converting_never_changes_the_status(self):
        for i, current in enumerate((S.NEW, S.INITIAL_CONTACT, S.HOT, S.SUPERHOT, S.LOST)):
            lead = make_lead(self.plan, self.admin, status=current, phone=f'98765000{i:02d}')
            with self.assertRaisesMessage(LeadConflict, 'Only Won leads can be converted.'):
                lead.convert(self.admin)
            self.assertEqual(Lead.objects.get(pk=lead.pk).status, current)
        # Moving a lead to Lost never creates a Work either.
        lost = make_lead(self.plan, self.admin, status=S.SUPERHOT, phone='9876500009')
        lost.move_to(S.LOST)
        self.assertFalse(Work.objects.exists())

        won = make_lead(self.plan, self.admin, status=S.SUPERHOT, phone='9876500010', amount=Decimal('185000'))
        won.move_to(S.WON)
        self.assertFalse(Work.objects.exists())  # reaching Won doesn't convert by itself
        won.convert(self.admin)
        work = Work.objects.get(lead=won)
        self.assertEqual(
            (work.customer_name, work.plan, work.amount, work.created_by, work.stage),
            (won.name, self.plan, Decimal('185000'), self.admin, WorkStage.LOAN_DOCUMENTS),
        )
        self.assertEqual(Lead.objects.get(pk=won.pk).status, S.WON)
        # Once only: converting again is refused, creates nothing and leaves the lead Won.
        with self.assertRaisesMessage(LeadConflict, 'This lead has already been converted.'):
            Lead.objects.get(pk=won.pk).convert(self.admin)
        self.assertEqual(Work.objects.count(), 1)
        self.assertEqual(Lead.objects.get(pk=won.pk).status, S.WON)

    def test_every_database_has_the_four_plans_at_their_starting_prices(self):
        self.assertEqual(
            list(SolarPlan.objects.values_list('name', 'amount', 'is_active')),
            [('3 kW', 150000, True), ('5 kW', 200000, True), ('8 kW', 245000, True), ('10 kW', 289000, True)],
        )

    def test_a_plan_price_change_never_alters_the_amount_saved_on_a_lead(self):
        lead = make_lead(self.plan, self.admin, amount=Decimal('195000'))
        self.plan.amount = Decimal('210000')
        self.plan.save()

        self.assertEqual(Lead.objects.get(pk=lead.pk).amount, Decimal('195000'))


class LeadApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')
        cls.plan = SolarPlan.objects.get(capacity=5)
        cls.retired_plan = SolarPlan.objects.create(name='2 kW', capacity=2, amount=90000, is_active=False)

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def payload(self, **fields):
        return {'name': 'Asha Menon', 'phone': '9876543210', 'district': 'Ernakulam', 'plan': self.plan.pk, **fields}

    def post_lead(self, **fields):
        return self.client.post(reverse('lead-list'), self.payload(**fields), format='json')

    def listed(self, key='id', **params):
        response = self.client.get(reverse('lead-list'), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row[key] for row in response.data['results']]

    def test_every_endpoint_requires_authentication(self):
        lead = make_lead(self.plan, self.admin)
        self.client.force_authenticate(None)

        for url in (
            reverse('lead-list'), reverse('lead-detail', args=[lead.pk]), reverse('plan-list'),
            reverse('lead-assignees'), reverse('lead-export'),
        ):
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED, url)
        self.assertEqual(self.post_lead().status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(reverse('lead-convert', args=[lead.pk])).status_code, 401)

    def test_staff_access_follows_granted_lead_permissions(self):
        lead = make_lead(self.plan, self.admin, assigned_to=self.staff)
        status_url = reverse('lead-change-status', args=[lead.pk])

        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(reverse('lead-list')).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(grant(self.staff, 'view_lead'))
        self.assertEqual(self.client.get(reverse('lead-list')).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(reverse('lead-detail', args=[lead.pk])).data['allowed_transitions'], [])
        self.assertEqual(self.post_lead(phone='9876500001').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(status_url, {'status': 'HOT'}, format='json').status_code, 403)
        self.assertEqual(self.client.post(reverse('lead-convert', args=[lead.pk])).status_code, 403)

        self.client.force_authenticate(grant(self.staff, 'add_lead', 'change_lead'))
        self.assertEqual(self.post_lead(phone='9876500001').status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.client.post(status_url, {'status': 'HOT'}, format='json').status_code, 200)
        # The whole customer list stays with admins.
        self.assertEqual(self.client.get(reverse('lead-export')).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_new_lead_always_starts_as_new_with_its_creator_and_the_plan_price(self):
        response = self.post_lead(status='WON', created_by=self.staff.pk)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        lead = Lead.objects.get(pk=response.data['id'])
        self.assertEqual((lead.status, lead.created_by, lead.amount), (S.NEW, self.admin, Decimal('200000')))
        self.assertEqual(response.data['status'], 'NEW')
        self.assertEqual(response.data['plan_name'], '5 kW')
        self.assertEqual(response.data['allowed_transitions'], ['INITIAL_CONTACT', 'HOT', 'SUPERHOT', 'WON', 'LOST'])

    def test_create_keeps_a_custom_amount_and_normalizes_the_phone_number(self):
        response = self.post_lead(phone='098765 43210', amount='195000.50')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual((response.data['phone'], response.data['amount']), ('9876543210', '195000.50'))
        uae = self.post_lead(country_code='971', phone='50 123 4567')
        self.assertEqual((uae.status_code, uae.data['phone']), (status.HTTP_201_CREATED, '501234567'))

    def test_create_rejects_invalid_data_field_by_field(self):
        inactive = User.objects.create_user(email='gone@example.com', password=PASSWORD, name='Gone', is_active=False)
        cases = [
            ({'district': ''}, 'district'),
            ({'plan': None}, 'plan'),
            ({'plan': self.retired_plan.pk}, 'plan'),
            ({'phone': '98765 4321'}, 'phone'),
            ({'phone': '+91 98765 43210'}, 'phone'),
            ({'phone': 'call me'}, 'phone'),
            ({'country_code': '0'}, 'country_code'),
            ({'country_code': '9715'}, 'country_code'),
            ({'pin_code': '12345'}, 'pin_code'),
            ({'amount': '-1'}, 'amount'),
            ({'assigned_to': inactive.pk}, 'assigned_to'),
            ({'source': 'NEWSPAPER'}, 'source'),
            ({'email': 'not-an-email'}, 'email'),
            ({'name': ''}, 'name'),
        ]
        for fields, field in cases:
            response = self.post_lead(**fields)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, fields)
            self.assertIn(field, response.data, fields)
        self.assertFalse(Lead.objects.exists())

    def test_edit_changes_fields_and_pin_but_never_the_status(self):
        lead = make_lead(self.plan, self.admin)

        response = self.client.patch(
            reverse('lead-detail', args=[lead.pk]),
            {'status': 'WON', 'district': 'Thrissur', 'is_pinned': True},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.district, lead.is_pinned), (S.NEW, 'Thrissur', True))

    def test_changing_the_plan_keeps_the_saved_amount_and_a_retired_plan_can_stay(self):
        lead = make_lead(self.retired_plan, self.admin, amount=Decimal('88000'))
        url = reverse('lead-detail', args=[lead.pk])

        self.assertEqual(self.client.patch(url, {'district': 'Kollam'}, format='json').status_code, 200)
        self.assertEqual(self.client.patch(url, {'plan': self.plan.pk}, format='json').status_code, 200)
        lead.refresh_from_db()
        self.assertEqual((lead.plan, lead.amount), (self.plan, Decimal('88000')))
        # Once moved off, the retired plan can't be chosen again.
        self.assertEqual(self.client.patch(url, {'plan': self.retired_plan.pk}, format='json').status_code, 400)

    def test_unknown_leads_are_not_found(self):
        self.assertEqual(self.client.get('/api/leads/999/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get('/api/leads/abc/').status_code, status.HTTP_404_NOT_FOUND)

    def test_list_is_paginated_with_the_real_total_and_a_page_size_choice(self):
        for i in range(30):
            make_lead(self.plan, self.admin, phone=f'98765{i:05d}')

        first = self.client.get(reverse('lead-list')).data
        self.assertEqual((first['count'], len(first['results'])), (30, 25))
        self.assertEqual(len(self.listed(page=2)), 5)
        self.assertEqual(len(self.listed(page_size=10)), 10)
        self.assertEqual(self.client.get(reverse('lead-list'), {'page': 3}).status_code, status.HTTP_404_NOT_FOUND)

    def test_search_finds_leads_by_name_phone_email_place_and_id_across_all_pages(self):
        asha = make_lead(self.plan, self.admin, name='Asha Menon', email='asha@example.com', area='Kakkanad')
        ravi = make_lead(self.plan, self.admin, name='Ravi Kumar', phone='8123456789', district='Thrissur')
        for i in range(30):
            make_lead(self.plan, self.admin, name=f'Other {i}', phone=f'70000{i:05d}')

        self.assertEqual(self.listed(search='asha'), [asha.pk])
        for phone in ('98765 43210', '+91 98765 43210', '09876543210', '43210'):
            self.assertEqual(self.listed(search=phone), [asha.pk], phone)
        self.assertEqual(self.listed(search='ASHA@EXAMPLE'), [asha.pk])
        self.assertEqual(self.listed(search='kakkanad'), [asha.pk])
        self.assertEqual(self.listed(search='thrissur'), [ravi.pk])
        self.assertEqual(self.listed(search=f'#{ravi.pk}'), [ravi.pk])
        self.assertEqual(self.listed(search='nobody'), [])

    def test_filters_combine_and_unknown_values_are_rejected(self):
        plan3 = SolarPlan.objects.get(capacity=3)
        hot = make_lead(self.plan, self.admin, status=S.HOT, source='REFERRAL', assigned_to=self.staff)
        old = make_lead(plan3, self.admin, phone='9876500002')
        Lead.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        today = timezone.localdate()

        self.assertEqual(self.listed(status='HOT'), [hot.pk])
        self.assertEqual(self.listed(plan=plan3.pk), [old.pk])
        self.assertEqual(self.listed(assigned_to=self.staff.pk), [hot.pk])
        self.assertEqual(self.listed(source='REFERRAL', status='HOT'), [hot.pk])
        self.assertEqual(self.listed(source='REFERRAL', status='NEW'), [])
        self.assertEqual(self.listed(created_before=str(today - timedelta(days=5))), [old.pk])
        self.assertEqual(self.listed(created_after=str(today)), [hot.pk])
        for bad in ({'status': 'CONFIRMED'}, {'status': 'Contacted'}, {'created_after': 'yesterday'}, {'ordering': 'phone'}):
            self.assertEqual(self.client.get(reverse('lead-list'), bad).status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_sorting_is_done_by_the_server_with_pinned_leads_first_and_status_in_pipeline_order(self):
        make_lead(self.plan, self.admin, name='Zara', phone='9876500001', amount=150000, status=S.LOST)
        make_lead(self.plan, self.admin, name='Anu', phone='9876500002', amount=289000, status=S.HOT)
        make_lead(self.plan, self.admin, name='Mini', phone='9876500003', amount=200000, is_pinned=True)
        make_lead(self.plan, self.admin, name='Bindu', phone='9876500004', amount=245000, status=S.SUPERHOT)

        self.assertEqual(self.listed('name', ordering='name'), ['Mini', 'Anu', 'Bindu', 'Zara'])
        self.assertEqual(self.listed('name', ordering='-amount'), ['Mini', 'Anu', 'Bindu', 'Zara'])
        self.assertEqual(self.listed('name', ordering='amount'), ['Mini', 'Zara', 'Bindu', 'Anu'])
        self.assertEqual(self.listed('name', ordering='status'), ['Mini', 'Anu', 'Bindu', 'Zara'])
        self.assertEqual(self.listed('name', ordering='-status'), ['Mini', 'Zara', 'Bindu', 'Anu'])
        # The order spans pages: the third lead by name is on page 3 when a page holds one lead.
        self.assertEqual(self.listed('name', ordering='name', page_size=1, page=3), ['Bindu'])

    def test_listing_uses_the_same_number_of_queries_for_any_number_of_leads(self):
        staff = grant(self.staff, 'view_lead', 'change_lead')
        make_lead(self.plan, self.admin, assigned_to=staff)

        def count_queries():
            self.client.force_authenticate(User.objects.get(pk=staff.pk))
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(reverse('lead-list')).status_code, 200)
            return len(queries)

        few = count_queries()
        for i in range(12):
            make_lead(self.plan, self.admin, phone=f'98765{i:05d}', assigned_to=staff)
        self.assertEqual(count_queries(), few)

    def test_status_changes_follow_the_pipeline_rules(self):
        lead = make_lead(self.plan, self.admin)
        url = reverse('lead-change-status', args=[lead.pk])

        response = self.client.post(url, {'status': 'SUPERHOT'}, format='json')
        self.assertEqual((response.status_code, response.data['status']), (status.HTTP_200_OK, 'SUPERHOT'))
        self.assertEqual(response.data['allowed_transitions'], ['WON', 'LOST'])
        self.assertEqual(self.client.post(url, {'status': 'HOT'}, format='json').status_code, status.HTTP_409_CONFLICT)
        for invalid in ('CONFIRMED', 'Site Visit', 'won', ''):
            self.assertEqual(self.client.post(url, {'status': invalid}, format='json').status_code, 400, invalid)

        self.assertEqual(self.client.post(url, {'status': 'LOST'}, format='json').status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post(url, {'status': 'HOT'}, format='json').status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Lead.objects.get(pk=lead.pk).status, S.LOST)
        self.assertEqual(self.listed(status='LOST'), [lead.pk])

        # Won is an ordinary move from Superhot, and final like Lost.
        won = make_lead(self.plan, self.admin, status=S.SUPERHOT, phone='9876500002')
        url = reverse('lead-change-status', args=[won.pk])
        response = self.client.post(url, {'status': 'WON'}, format='json')
        self.assertEqual((response.status_code, response.data['status'], response.data['allowed_transitions']), (200, 'WON', []))
        self.assertEqual(self.client.post(url, {'status': 'LOST'}, format='json').status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Lead.objects.get(pk=won.pk).status, S.WON)

    def test_the_api_converts_only_won_leads_whatever_a_client_sends(self):
        for i, current in enumerate((S.NEW, S.INITIAL_CONTACT, S.HOT, S.SUPERHOT, S.LOST)):
            lead = make_lead(self.plan, self.admin, status=current, phone=f'98765000{i:02d}')
            self.assertFalse(self.client.get(reverse('lead-detail', args=[lead.pk])).data['can_convert'], current)
            response = self.client.post(reverse('lead-convert', args=[lead.pk]))
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, current)
            self.assertIn('Only Won leads can be converted.', response.data['detail'])
            self.assertEqual(Lead.objects.get(pk=lead.pk).status, current)

        self.assertFalse(Work.objects.exists())

        won = make_lead(self.plan, self.admin, status=S.WON, phone='9876500010')
        self.assertTrue(self.client.get(reverse('lead-detail', args=[won.pk])).data['can_convert'])
        response = self.client.post(reverse('lead-convert', args=[won.pk]))
        work = Work.objects.get(lead=won)
        self.assertEqual(
            (response.status_code, response.data['status'], response.data['work'], response.data['can_convert']),
            (status.HTTP_200_OK, 'WON', work.pk, False),
        )
        self.assertEqual(work.stage, WorkStage.LOAN_DOCUMENTS)  # the Work starts at the Work Pipeline's first stage
        again = self.client.post(reverse('lead-convert', args=[won.pk]))
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('This lead has already been converted.', again.data['detail'])
        self.assertEqual(Work.objects.count(), 1)
        self.assertEqual(Lead.objects.get(pk=won.pk).status, S.WON)
        # A Won lead can't be moved on to Lost, and a converted lead keeps its Work: deleting it is refused.
        lost = self.client.post(reverse('lead-change-status', args=[won.pk]), {'status': 'LOST'}, format='json')
        self.assertEqual(lost.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[won.pk])).status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Work.objects.filter(lead=won).exists())

    def test_export_is_an_admin_only_csv_that_follows_the_filters_and_neutralizes_formulas(self):
        make_lead(self.plan, self.admin, name='=HYPERLINK("http://example.com")', status=S.HOT)
        make_lead(self.plan, self.admin, name='Ravi Kumar', phone='9876500002')

        response = self.client.get(reverse('lead-export'), {'status': 'HOT'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual(rows[0][:4], ['Lead ID', 'Customer', 'Country code', 'Phone'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1:4], ['\'=HYPERLINK("http://example.com")', '91', '9876543210'])
        self.assertEqual(rows[1][11], 'Hot')

    def test_assignees_are_active_users_with_only_their_names(self):
        User.objects.create_user(email='gone@example.com', password=PASSWORD, name='Gone', is_active=False)

        response = self.client.get(reverse('lead-assignees'))

        self.assertEqual(response.data, [{'id': self.admin.pk, 'name': 'Admin'}, {'id': self.staff.pk, 'name': 'Staff'}])
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(reverse('lead-assignees')).status_code, status.HTTP_403_FORBIDDEN)
        # Staff can't reassign leads, so the only assignee they're offered is themselves.
        self.client.force_authenticate(grant(self.staff, 'view_lead'))
        self.assertEqual(self.client.get(reverse('lead-assignees')).data, [{'id': self.staff.pk, 'name': 'Staff'}])

    def test_plans_list_every_plan_with_its_current_price_for_any_signed_in_user(self):
        self.client.force_authenticate(self.staff)

        response = self.client.get(reverse('plan-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(plan['name'], plan['amount'], plan['is_active']) for plan in response.data],
            [
                ('2 kW', '90000.00', False), ('3 kW', '150000.00', True), ('5 kW', '200000.00', True),
                ('8 kW', '245000.00', True), ('10 kW', '289000.00', True),
            ],
        )


class LeadAccessTests(APITestCase):
    """Admins work with every lead; staff with the Leads module work only with the leads assigned to them."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff_a = User.objects.create_user(email='a@example.com', password=PASSWORD, name='Staff A')
        cls.staff_b = User.objects.create_user(email='b@example.com', password=PASSWORD, name='Staff B')
        # The Leads module, as an admin gives it to the STAFF role in Settings -> Roles & Access.
        grant(cls.staff_a, 'view_lead', 'add_lead', 'change_lead')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.a_lead = make_lead(self.plan, self.admin, name='A Lead', assigned_to=self.staff_a, status=S.SUPERHOT)
        self.b_lead = make_lead(self.plan, self.admin, name='B Lead', phone='9876500002', assigned_to=self.staff_b)
        self.unassigned = make_lead(self.plan, self.admin, name='Pool Lead', phone='9876500003')

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def names(self):
        return sorted(row['name'] for row in self.client.get(reverse('lead-list')).data['results'])

    def calls(self, lead):
        """Every lead action, each as (label, response)."""
        detail = reverse('lead-detail', args=[lead.pk])
        return [
            ('view', self.client.get(detail)),
            ('edit', self.client.patch(detail, {'district': 'Kollam'}, format='json')),
            ('pin', self.client.patch(detail, {'is_pinned': True}, format='json')),
            ('status', self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': 'LOST'}, format='json')),
            ('convert', self.client.post(reverse('lead-convert', args=[lead.pk]))),
            ('delete', self.client.delete(detail)),
        ]

    def walk_to_won_then_convert(self, lead, user):
        """New -> Initial Contact -> Hot -> Superhot -> Won through the status API, with conversion refused before Won,
        then converted by `user`: one Work, and the lead stays Won."""
        self.as_user(user)
        detail = reverse('lead-detail', args=[lead.pk])
        convert_url = reverse('lead-convert', args=[lead.pk])
        for target in (S.INITIAL_CONTACT, S.HOT, S.SUPERHOT, S.WON):
            current = self.client.get(detail).data
            self.assertFalse(current['can_convert'], current['status'])
            refused = self.client.post(convert_url)
            self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT, current['status'])
            self.assertIn('Only Won leads can be converted.', refused.data['detail'])
            moved = self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': target}, format='json')
            self.assertEqual((moved.status_code, moved.data['status']), (200, target))
            self.assertEqual(Lead.objects.get(pk=lead.pk).status, target)
        self.assertFalse(Work.objects.filter(lead=lead).exists())
        self.assertTrue(self.client.get(detail).data['can_convert'])
        converted = self.client.post(convert_url)
        work = Work.objects.get(lead=lead)
        self.assertEqual((converted.status_code, converted.data['status'], converted.data['work']), (200, S.WON, work.pk))
        # The Work takes the customer, plan, amount and assignee as they are at conversion.
        self.assertEqual(
            (work.customer_name, work.phone, work.plan, work.amount, work.assigned_to, work.created_by),
            (lead.name, lead.phone, lead.plan, lead.amount, lead.assigned_to, user),
        )
        self.assertFalse(self.client.get(detail).data['can_convert'])
        again = self.client.post(convert_url)
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('This lead has already been converted.', again.data['detail'])
        self.assertEqual(Work.objects.filter(lead=lead).count(), 1)
        self.assertEqual(Lead.objects.get(pk=lead.pk).status, S.WON)

    def test_admin_moves_a_lead_assigned_to_someone_else_to_won_and_converts_it(self):
        self.walk_to_won_then_convert(self.b_lead, self.admin)

    def test_assigned_staff_move_their_lead_to_won_and_convert_it(self):
        lead = make_lead(self.plan, self.admin, phone='9876500004', assigned_to=self.staff_a)
        self.walk_to_won_then_convert(lead, self.staff_a)

    def test_the_status_api_checks_the_user_the_lead_and_the_status(self):
        url = reverse('lead-change-status', args=[self.b_lead.pk])
        self.as_user(self.admin)
        for invalid in ('CONFIRMED', 'CONTACTED', 'SITE_VISIT', 'Initial Contact', 'won', ''):
            self.assertEqual(self.client.post(url, {'status': invalid}, format='json').status_code, 400, invalid)
        missing = reverse('lead-change-status', args=[999999])
        self.assertEqual(self.client.post(missing, {'status': 'HOT'}, format='json').status_code, 404)
        # Another staff member's lead is out of reach, and an unauthenticated request is refused outright.
        self.as_user(self.staff_a)
        self.assertEqual(self.client.post(url, {'status': 'WON'}, format='json').status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(url, {'status': 'WON'}, format='json').status_code, 401)
        self.assertEqual(Lead.objects.get(pk=self.b_lead.pk).status, S.NEW)
        # Won straight from New: any open lead can be marked Won, then converted.
        self.as_user(self.admin)
        won = self.client.post(url, {'status': 'WON'}, format='json')
        self.assertEqual((won.status_code, won.data['status'], won.data['can_convert']), (200, 'WON', True))

    def test_admin_works_with_every_lead_and_assignment(self):
        self.as_user(self.admin)
        self.assertEqual(self.names(), ['A Lead', 'B Lead', 'Pool Lead'])
        detail = self.client.get(reverse('lead-detail', args=[self.b_lead.pk])).data
        self.assertEqual((detail['can_edit'], detail['can_delete'], detail['can_assign']), (True, True, True))

        url = reverse('lead-detail', args=[self.unassigned.pk])
        response = self.client.patch(
            url, {'assigned_to': self.staff_a.pk, 'district': 'Kollam', 'is_pinned': True}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(Lead.objects.get(pk=self.unassigned.pk).assigned_to, self.staff_a)
        status_url = reverse('lead-change-status', args=[self.b_lead.pk])
        self.assertEqual(self.client.post(status_url, {'status': 'HOT'}, format='json').status_code, 200)
        self.assertEqual(self.client.get(reverse('lead-export')).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[self.b_lead.pk])).status_code, 204)
        self.assertFalse(Lead.objects.filter(pk=self.b_lead.pk).exists())

    def test_assigned_staff_manage_their_own_leads(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.names(), ['A Lead'])
        detail = self.client.get(reverse('lead-detail', args=[self.a_lead.pk])).data
        self.assertEqual((detail['can_edit'], detail['can_delete'], detail['can_assign']), (True, False, False))
        self.assertEqual(detail['allowed_transitions'], ['WON', 'LOST'])

        url = reverse('lead-detail', args=[self.a_lead.pk])
        self.assertEqual(self.client.patch(url, {'district': 'Kollam', 'is_pinned': True}, format='json').status_code, 200)
        self.assertEqual(self.client.patch(url, {'is_pinned': False}, format='json').status_code, 200)
        status_url = reverse('lead-change-status', args=[self.a_lead.pk])
        self.assertEqual(self.client.post(status_url, {'status': 'LOST'}, format='json').status_code, 200)
        self.assertEqual(Lead.objects.get(pk=self.a_lead.pk).status, S.LOST)

    def test_staff_cannot_reach_leads_of_other_staff_or_unassigned_leads(self):
        self.as_user(self.staff_a)
        for lead in (self.b_lead, self.unassigned):
            for label, response in self.calls(lead):
                # Delete is refused before any lead is looked up (staff have no delete permission at all); everything
                # else answers as if the lead didn't exist.
                expected = status.HTTP_403_FORBIDDEN if label == 'delete' else status.HTTP_404_NOT_FOUND
                self.assertEqual(response.status_code, expected, (lead.name, label))
        b_lead = Lead.objects.get(pk=self.b_lead.pk)
        self.assertEqual((b_lead.district, b_lead.is_pinned, b_lead.status), ('Ernakulam', False, S.NEW))
        self.assertEqual(self.client.get(reverse('lead-list'), {'search': 'B Lead'}).data['count'], 0)
        self.assertEqual(self.client.get(reverse('lead-export')).status_code, status.HTTP_403_FORBIDDEN)
        # Not even another person's Won lead.
        won = make_lead(self.plan, self.admin, phone='9876500005', assigned_to=self.staff_b, status=S.WON)
        self.assertEqual(self.client.post(reverse('lead-convert', args=[won.pk])).status_code, 404)
        self.assertFalse(Work.objects.filter(lead=won).exists())

    def test_staff_cannot_assign_or_reassign_leads(self):
        self.as_user(self.staff_a)
        url = reverse('lead-detail', args=[self.a_lead.pk])
        moved = self.client.patch(url, {'assigned_to': self.staff_b.pk}, format='json')
        self.assertEqual(moved.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(moved.data['assigned_to'][0], 'Only an admin can assign a lead to someone else.')
        self.assertEqual(self.client.patch(url, {'assigned_to': None}, format='json').status_code, 400)
        self.assertEqual(Lead.objects.get(pk=self.a_lead.pk).assigned_to, self.staff_a)
        # Keeping the current assignee (as the edit form sends it) is fine.
        self.assertEqual(self.client.patch(url, {'assigned_to': self.staff_a.pk}, format='json').status_code, 200)

        payload = {'name': 'New Lead', 'phone': '9876500009', 'district': 'Thrissur', 'plan': self.plan.pk}
        handed = self.client.post(reverse('lead-list'), {**payload, 'assigned_to': self.staff_b.pk}, format='json')
        self.assertEqual(handed.status_code, status.HTTP_400_BAD_REQUEST)
        created = self.client.post(reverse('lead-list'), payload, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        lead = Lead.objects.get(pk=created.data['id'])
        self.assertEqual((lead.assigned_to, lead.created_by), (self.staff_a, self.staff_a))
        self.assertEqual(self.names(), ['A Lead', 'New Lead'])

    def test_deleting_a_lead_needs_the_delete_permission_which_the_leads_module_does_not_give(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[self.a_lead.pk])).status_code, 403)
        self.assertTrue(Lead.objects.filter(pk=self.a_lead.pk).exists())
        self.as_user(self.admin)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[self.a_lead.pk])).status_code, 204)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[self.a_lead.pk])).status_code, 404)

    def test_reassigning_moves_the_lead_between_staff(self):
        self.as_user(self.admin)
        url = reverse('lead-detail', args=[self.a_lead.pk])
        self.assertEqual(self.client.patch(url, {'assigned_to': self.staff_b.pk}, format='json').status_code, 200)
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        self.as_user(self.staff_b)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_staff_without_the_leads_module_cannot_open_even_their_own_leads(self):
        Role.objects.get(name=Role.STAFF).permissions.clear()
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(reverse('lead-list')).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(reverse('lead-detail', args=[self.a_lead.pk])).status_code, 403)

    def test_the_summary_counts_and_totals_each_status_for_the_leads_the_user_works_with(self):
        make_lead(self.plan, self.admin, name='Custom', phone='9876500007', amount=Decimal('150000.50'))
        self.as_user(self.admin)
        summary = {row['status']: row for row in self.client.get(reverse('lead-summary')).data}
        self.assertEqual(list(summary), S.values)
        self.assertEqual(
            (summary['NEW']['label'], summary['NEW']['count'], summary['NEW']['total_amount']), ('New', 3, '550000.50'),
        )
        self.assertEqual((summary['SUPERHOT']['count'], summary['SUPERHOT']['total_amount']), (1, '200000.00'))
        self.assertEqual((summary['WON']['count'], summary['WON']['total_amount']), (0, '0.00'))
        # The list's search and filters apply, and are validated as the list's are.
        searched = {row['status']: row['count'] for row in self.client.get(reverse('lead-summary'), {'search': 'Pool'}).data}
        self.assertEqual((searched['NEW'], searched['SUPERHOT']), (1, 0))
        only = {row['status']: row['count'] for row in self.client.get(reverse('lead-summary'), {'status': 'SUPERHOT'}).data}
        self.assertEqual((only['NEW'], only['SUPERHOT']), (0, 1))
        self.assertEqual(self.client.get(reverse('lead-summary'), {'status': 'CONFIRMED'}).status_code, 400)
        # Staff get the numbers of their own leads only; without the Leads module, or signed out, nothing.
        self.as_user(self.staff_a)
        own = {row['status']: row for row in self.client.get(reverse('lead-summary')).data}
        self.assertEqual((own['NEW']['count'], own['SUPERHOT']['count'], own['SUPERHOT']['total_amount']), (0, 1, '200000.00'))
        Role.objects.get(name=Role.STAFF).permissions.clear()
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(reverse('lead-summary')).status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('lead-summary')).status_code, status.HTTP_401_UNAUTHORIZED)

@override_settings(DEBUG=True)
class SeedDemoLeadsTests(TestCase):
    def seed(self):
        output = io.StringIO()
        call_command('seed_demo_leads', stdout=output)
        return output.getvalue()

    def add_team(self):
        User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        User.objects.create_user(email='priya@example.com', password=PASSWORD, name='Priya Nair')
        User.objects.create_user(email='arjun@example.com', password=PASSWORD, name='Arjun Das')

    def test_seed_adds_fifty_valid_leads_across_the_pipeline_and_is_safe_to_run_again(self):
        self.add_team()

        first = self.seed()
        second = self.seed()

        self.assertIn('50 added, 0 already present', first)
        self.assertIn('0 added, 50 already present', second)
        self.assertEqual(Lead.objects.count(), 50)
        self.assertEqual(
            Counter(Lead.objects.values_list('status', flat=True)),
            {S.NEW: 10, S.INITIAL_CONTACT: 9, S.HOT: 9, S.SUPERHOT: 8, S.WON: 7, S.LOST: 7},
        )
        # Each Won demo lead is converted once: its Work is created on the first run and never again.
        self.assertIn('Demo Works: 7 converted now, 7 Won leads with their Work.', first)
        self.assertIn('Demo Works: 0 converted now, 7 Won leads with their Work.', second)
        self.assertNotIn('could not be converted', first + second)
        self.assertEqual(Work.objects.count(), 7)
        self.assertEqual(set(Work.objects.values_list('lead__status', flat=True)), {S.WON})
        # Follow-ups on demo leads, to do and done, added once.
        self.assertIn('Demo follow-ups: 14 added, 14 in total (9 pending, 5 completed).', first)
        self.assertIn('Demo follow-ups: 0 added, 14 in total (9 pending, 5 completed).', second)
        self.assertEqual(Activity.objects.count(), 14)
        self.assertEqual(
            list(SolarPlan.objects.values_list('name', 'amount')),
            [('3 kW', 150000), ('5 kW', 200000), ('8 kW', 245000), ('10 kW', 289000)],
        )
        self.assertEqual(
            Counter(Lead.objects.values_list('plan__name', flat=True)), {'3 kW': 13, '5 kW': 18, '8 kW': 11, '10 kW': 8},
        )
        self.assertEqual(Lead.objects.get(name='Ann Mary Jose').amount, Decimal('195000'))
        self.assertEqual(Lead.objects.get(name='Arjun Nair').amount, Decimal('200000'))
        self.assertEqual(
            set(Lead.objects.exclude(assigned_to=None).values_list('assigned_to__name', flat=True)),
            {'Priya Nair', 'Arjun Das'},
        )
        self.assertEqual(Lead.objects.filter(assigned_to=None).count(), 5)
        self.assertGreater(len({lead.created_at.date() for lead in Lead.objects.all()}), 30)
        self.assertTrue(all(lead.created_at <= timezone.now() for lead in Lead.objects.all()))
        for lead in Lead.objects.all():
            lead.full_clean()
            self.assertEqual(len(lead.phone), 10)

    def test_seed_keeps_existing_plan_prices_and_never_touches_a_lead_that_is_not_its_own(self):
        self.add_team()
        SolarPlan.objects.filter(capacity=5).update(amount=210000)
        admin = User.objects.get(email='admin@example.com')
        real = make_lead(SolarPlan.objects.get(capacity=5), admin, name='Real Customer', phone=phone_for(0))

        self.seed()

        self.assertEqual(SolarPlan.objects.get(capacity=5).amount, Decimal('210000'))
        self.assertEqual(Lead.objects.get(name='Anjali Thomas').amount, Decimal('210000'))
        self.assertEqual(Lead.objects.count(), 50)
        self.assertEqual(Lead.objects.get(pk=real.pk).name, 'Real Customer')
        self.assertFalse(Lead.objects.filter(name='Arjun Nair').exists())

    def test_seed_refuses_outside_development_and_without_a_user(self):
        with override_settings(DEBUG=False), self.assertRaisesMessage(CommandError, 'DJANGO_DEBUG=True'):
            self.seed()
        with self.assertRaisesMessage(CommandError, 'createsuperuser'):
            self.seed()
        self.assertFalse(Lead.objects.exists())


class BulkLeadActionTests(APITestCase):
    """The list's bulk actions: each selected lead goes through the same rules as its single-record action, and the
    response says which went through and, for each that didn't, why."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.admin_2 = User.objects.create_user(email='admin2@example.com', password=PASSWORD, name='Admin Two', role=Role.ADMIN)
        cls.staff_a = User.objects.create_user(email='a@example.com', password=PASSWORD, name='Staff A')
        cls.staff_b = User.objects.create_user(email='b@example.com', password=PASSWORD, name='Staff B')
        grant(cls.staff_a, 'view_lead', 'add_lead', 'change_lead')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.new = make_lead(self.plan, self.admin, name='New Lead', phone='9876500001', assigned_to=self.staff_a)
        self.hot = make_lead(self.plan, self.admin, name='Hot Lead', phone='9876500002', status=S.HOT, assigned_to=self.staff_a)
        self.won = make_lead(self.plan, self.admin, name='Won Lead', phone='9876500003', status=S.WON, assigned_to=self.staff_a)
        self.lost = make_lead(self.plan, self.admin, name='Lost Lead', phone='9876500004', status=S.LOST, assigned_to=self.staff_a)
        self.converted = make_lead(self.plan, self.admin, name='Converted Lead', phone='9876500005', status=S.WON, assigned_to=self.staff_a)
        self.converted.convert(self.admin)
        Activity.objects.create(lead=self.new, title='Call', type='PHONE_CALL', assigned_to=self.staff_a, created_by=self.admin)
        self.b_lead = make_lead(self.plan, self.admin, name='B Lead', phone='9876500006', assigned_to=self.staff_b)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def bulk(self, name, **body):
        return self.client.post(reverse(f'lead-bulk-{name}'), body, format='json')

    def failures(self, response):
        return {row['id']: row['reason'] for row in response.data['failed']}

    def test_bulk_status_moves_each_lead_by_the_pipeline_rules_and_reports_the_rest(self):
        self.as_user(self.admin)
        response = self.bulk('status', ids=[self.new.pk, self.hot.pk, self.won.pk, self.lost.pk, 999999, self.new.pk], status='SUPERHOT')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['succeeded'], [self.new.pk, self.hot.pk])
        self.assertEqual(self.failures(response), {
            self.won.pk: "A Won lead can't move to Superhot.",
            self.lost.pk: "A Lost lead can't move to Superhot.",
            999999: "This record no longer exists, or you don't have access to it.",
        })
        self.assertEqual([row['name'] for row in response.data['failed']], ['Won Lead', 'Lost Lead', None])
        self.assertEqual(Lead.objects.get(pk=self.new.pk).status, S.SUPERHOT)
        self.assertEqual(Lead.objects.get(pk=self.hot.pk).status, S.SUPERHOT)
        self.assertEqual(Lead.objects.get(pk=self.won.pk).status, S.WON)
        # Won through the bulk action creates no Work: conversion stays a separate, explicit step.
        self.assertEqual(self.bulk('status', ids=[self.new.pk], status='WON').data['succeeded'], [self.new.pk])
        self.assertFalse(Work.objects.filter(lead=self.new).exists())

        for bad in ({'ids': [self.new.pk], 'status': 'CONFIRMED'}, {'ids': [], 'status': 'HOT'}, {'status': 'HOT'},
                    {'ids': ['x'], 'status': 'HOT'}, {'ids': list(range(1, 102)), 'status': 'HOT'}, {'ids': [0], 'status': 'HOT'}):
            self.assertEqual(self.bulk('status', **bad).status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_bulk_convert_converts_only_won_leads_once_through_the_same_rules_and_tells_the_admins(self):
        self.as_user(self.staff_a)
        response = self.bulk('convert', ids=[self.won.pk, self.hot.pk, self.converted.pk, self.lost.pk, self.b_lead.pk])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['succeeded'], [self.won.pk])
        self.assertEqual(self.failures(response), {
            self.hot.pk: 'Only Won leads can be converted.',
            self.converted.pk: 'This lead has already been converted.',
            self.lost.pk: 'Only Won leads can be converted.',
            # Another person's lead is out of reach for staff, as it is for the single action.
            self.b_lead.pk: "This record no longer exists, or you don't have access to it.",
        })
        work = Work.objects.get(lead=self.won)
        self.assertEqual((work.customer_name, work.amount, work.created_by), ('Won Lead', self.plan.amount, self.staff_a))
        self.assertEqual(Lead.objects.get(pk=self.won.pk).status, S.WON)
        self.assertEqual(Work.objects.count(), 2)
        for admin in (self.admin, self.admin_2):
            [notice] = Notification.objects.filter(recipient=admin)
            self.assertEqual((notice.kind, notice.lead_id, notice.work_id), ('LEAD_CONVERTED', self.won.pk, work.pk))
        # Again: already converted, and still one Work.
        again = self.bulk('convert', ids=[self.won.pk])
        self.assertEqual((again.data['succeeded'], self.failures(again)), ([], {self.won.pk: 'This lead has already been converted.'}))
        self.assertEqual(Work.objects.filter(lead=self.won).count(), 1)

    def test_bulk_delete_is_for_admins_and_keeps_leads_that_have_a_work(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.bulk('delete', ids=[self.new.pk]).status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Lead.objects.filter(pk=self.new.pk).exists())

        self.as_user(self.admin)
        response = self.bulk('delete', ids=[self.converted.pk, self.new.pk, 999999, self.lost.pk])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['succeeded'], [self.new.pk, self.lost.pk])
        self.assertEqual(self.failures(response), {
            self.converted.pk: "This lead has records that depend on it, such as its Work, so it can't be deleted.",
            999999: "This record no longer exists, or you don't have access to it.",
        })
        self.assertEqual(set(Lead.objects.values_list('name', flat=True)), {'Hot Lead', 'Won Lead', 'Converted Lead', 'B Lead'})
        self.assertFalse(Activity.objects.filter(lead_id=self.new.pk).exists())
        self.assertEqual(Work.objects.count(), 1)
        self.assertEqual(set(User.objects.values_list('email', flat=True)), {'admin@example.com', 'admin2@example.com', 'a@example.com', 'b@example.com'})

    def test_bulk_actions_follow_the_lead_permissions(self):
        Role.objects.get(name=Role.STAFF).permissions.clear()
        self.as_user(self.staff_a)
        for name in ('status', 'convert', 'delete'):
            body = {'ids': [self.new.pk], **({'status': 'HOT'} if name == 'status' else {})}
            self.assertEqual(self.bulk(name, **body).status_code, status.HTTP_403_FORBIDDEN, name)
        self.client.force_authenticate(None)
        self.assertEqual(self.bulk('status', ids=[self.new.pk], status='HOT').status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(Lead.objects.get(pk=self.new.pk).status, S.NEW)

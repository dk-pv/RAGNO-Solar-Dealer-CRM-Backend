"""Leads that need only a name and a phone number, the dealer name, and leads assigned to several people (with the
one-assignee fields earlier API clients send and read)."""

import csv
import io
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity
from apps.notifications.models import Notification, NotificationKind
from apps.works.models import Work

from .models import Lead, LeadStatus, SolarPlan

PASSWORD = 'Solar-Panel-2026'


class LeadTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.asha = User.objects.create_user(email='asha@example.com', password=PASSWORD, name='Asha Nair')
        cls.binu = User.objects.create_user(email='binu@example.com', password=PASSWORD, name='Binu Nair')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.as_user(self.admin)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def post(self, **fields):
        return self.client.post(reverse('lead-list'), fields, format='json')

    def patch(self, lead, **fields):
        return self.client.patch(reverse('lead-detail', args=[lead.pk]), fields, format='json')

    def add(self, **fields):
        response = self.post(**fields)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return Lead.objects.get(pk=response.data['id'])

    def listed(self, **params):
        response = self.client.get(reverse('lead-list'), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row['name'] for row in response.data['results']], response.data['count']


class OptionalFieldTests(LeadTestCase):
    def test_a_lead_needs_only_a_name_and_a_phone_number_and_starts_new(self):
        response = self.post(name='Ravi Kumar', phone='9876543210')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        data = response.data
        self.assertEqual(
            (data['status'], data['plan'], data['plan_name'], data['amount'], data['district'], data['dealer_name'],
             data['assignees'], data['assigned_to'], data['assigned_to_name'], data['can_convert']),
            ('NEW', None, None, None, '', '', [], None, None, False),
        )
        self.assertEqual(data['allowed_transitions'], ['INITIAL_CONTACT', 'HOT', 'SUPERHOT', 'WON', 'LOST'])

    def test_the_name_and_the_phone_number_stay_required(self):
        for fields, field in [
            ({'phone': '9876543210'}, 'name'), ({'name': 'Ravi'}, 'phone'),
            ({'name': '', 'phone': '9876543210'}, 'name'), ({'name': 'Ravi', 'phone': ''}, 'phone'),
        ]:
            response = self.post(**fields)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, fields)
            self.assertIn(field, response.data, fields)
        self.assertFalse(Lead.objects.exists())

    def test_every_other_field_can_be_left_out_empty_or_null_and_emptied_again(self):
        lead = self.add(
            name='Ravi Kumar', phone='9876543210', email='', state='', district='', area='', pin_code='', dealer_name='',
            plan=None, amount=None, source='', assignee_ids=[], next_follow_up=None, notes='',
        )
        self.assertEqual((lead.plan, lead.amount, lead.district, lead.dealer_name), (None, None, '', ''))

        planned = self.add(name='Mini', phone='9876500002', plan=self.plan.pk, district='Ernakulam')
        response = self.patch(planned, plan=None, amount=None, district='')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data['plan'], response.data['amount'], response.data['district']), (None, None, ''))

    def test_a_chosen_plan_brings_its_price_from_the_database_and_no_amount_is_made_up_without_one(self):
        # Whatever an admin has set: the price is the database's, never one written in code.
        SolarPlan.objects.filter(pk=self.plan.pk).update(amount=Decimal('212345'))
        cases = [
            ({'plan': self.plan.pk}, '212345.00'),
            ({'plan': self.plan.pk, 'amount': None}, '212345.00'),
            ({'plan': self.plan.pk, 'amount': '199000'}, '199000.00'),
            ({'amount': '150000'}, '150000.00'),
            ({}, None),
        ]
        for index, (fields, amount) in enumerate(cases):
            response = self.post(name=f'Lead {index}', phone=f'98765000{index:02d}', **fields)
            self.assertEqual((response.status_code, response.data['amount']), (201, amount), fields)

        # A plan chosen later brings its price then, and a later price change never alters the lead's amount.
        planless = Lead.objects.get(name='Lead 4')
        self.assertEqual(self.patch(planless, district='Kollam').data['amount'], None)
        self.assertEqual(self.patch(planless, plan=self.plan.pk).data['amount'], '212345.00')
        SolarPlan.objects.filter(pk=self.plan.pk).update(amount=Decimal('250000'))
        self.assertEqual(Lead.objects.get(pk=planless.pk).amount, Decimal('212345'))


class DealerNameTests(LeadTestCase):
    def test_the_dealer_name_is_saved_shown_edited_searched_and_exported(self):
        created = self.post(name='Ravi Kumar', phone='9876543210', dealer_name='Sunrise Solar Traders')
        self.assertEqual((created.status_code, created.data['dealer_name']), (201, 'Sunrise Solar Traders'))
        lead = Lead.objects.get(pk=created.data['id'])
        self.add(name='Mini', phone='9876500002')

        self.assertEqual(self.client.get(reverse('lead-detail', args=[lead.pk])).data['dealer_name'], 'Sunrise Solar Traders')
        self.assertEqual(self.listed(search='sunrise'), (['Ravi Kumar'], 1))
        self.assertEqual(self.patch(lead, dealer_name='Kerala Green Energy').data['dealer_name'], 'Kerala Green Energy')
        self.assertEqual(self.listed(search='GREEN ENERGY'), (['Ravi Kumar'], 1))
        self.assertEqual(self.listed(search='sunrise'), ([], 0))
        self.assertEqual(self.patch(lead, dealer_name='x' * 151).status_code, status.HTTP_400_BAD_REQUEST)

        export = self.client.get(reverse('lead-export'), {'search': 'Ravi'})
        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        self.assertEqual(rows[1][rows[0].index('Dealer')], 'Kerala Green Energy')


class LeadAssigneeTests(LeadTestCase):
    def test_a_lead_can_have_several_assignees_and_is_still_one_row_whatever_the_filter_or_order(self):
        created = self.post(name='Ravi Kumar', phone='9876543210', assignee_ids=[self.binu.pk, self.asha.pk, self.binu.pk])
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(
            created.data['assignees'], [{'id': self.asha.pk, 'name': 'Asha Nair'}, {'id': self.binu.pk, 'name': 'Binu Nair'}],
        )
        # The one-assignee fields of earlier clients: the first assignee by name, and every name.
        self.assertEqual((created.data['assigned_to'], created.data['assigned_to_name']), (self.asha.pk, 'Asha Nair, Binu Nair'))
        self.add(name='Mini', phone='9876500002', assignee_ids=[self.binu.pk])
        self.add(name='Zara', phone='9876500003')

        self.assertEqual(self.listed(assigned_to=self.asha.pk), (['Ravi Kumar'], 1))
        self.assertEqual(self.listed(assigned_to=self.binu.pk, ordering='name'), (['Mini', 'Ravi Kumar'], 2))
        self.assertEqual(self.listed(ordering='assigned_to__name'), (['Ravi Kumar', 'Mini', 'Zara'], 3))
        self.assertEqual(self.listed(ordering='-assigned_to__name'), (['Mini', 'Ravi Kumar', 'Zara'], 3))
        counts = {row['status']: row['count'] for row in self.client.get(reverse('lead-summary'), {'assigned_to': self.binu.pk}).data}
        self.assertEqual(counts['NEW'], 2)

    def test_editing_replaces_the_assignees_and_other_edits_keep_them(self):
        lead = self.add(name='Ravi', phone='9876543210', assignee_ids=[self.asha.pk])

        self.assertEqual(self.patch(lead, assignee_ids=[self.asha.pk, self.binu.pk]).data['assigned_to_name'], 'Asha Nair, Binu Nair')
        self.assertEqual(self.patch(lead, assignee_ids=[self.binu.pk]).data['assignees'], [{'id': self.binu.pk, 'name': 'Binu Nair'}])
        self.assertEqual(self.patch(lead, district='Kollam').data['assigned_to_name'], 'Binu Nair')
        self.assertEqual(self.patch(lead, assignee_ids=[]).data['assignees'], [])
        self.assertEqual(self.patch(lead, assignee_ids=[999999]).status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_one_assignee_field_of_earlier_clients_still_works(self):
        created = self.post(name='Ravi', phone='9876543210', assigned_to=self.binu.pk)
        self.assertEqual((created.status_code, created.data['assignees']), (201, [{'id': self.binu.pk, 'name': 'Binu Nair'}]))
        lead = Lead.objects.get(pk=created.data['id'])
        self.patch(lead, assignee_ids=[self.asha.pk, self.binu.pk])

        # Writing it makes that one user the only assignee; null leaves none.
        self.assertEqual(self.patch(lead, assigned_to=self.binu.pk).data['assignees'], [{'id': self.binu.pk, 'name': 'Binu Nair'}])
        self.assertEqual(self.patch(lead, assigned_to=None).data['assignees'], [])
        both = self.patch(lead, assigned_to=self.asha.pk, assignee_ids=[self.binu.pk])
        self.assertEqual(both.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assigned_to', both.data)

    def test_only_active_users_can_be_newly_assigned_and_a_deactivated_one_may_stay(self):
        gone = User.objects.create_user(email='gone@example.com', password=PASSWORD, name='Gone', is_active=False)
        refused = self.post(name='Ravi', phone='9876543210', assignee_ids=[self.asha.pk, gone.pk])
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assignee_ids', refused.data)
        self.assertFalse(Lead.objects.exists())

        lead = self.add(name='Ravi', phone='9876543210', assignee_ids=[self.asha.pk])
        User.objects.filter(pk=self.asha.pk).update(is_active=False)
        self.assertEqual(self.patch(lead, assignee_ids=[self.asha.pk, self.binu.pk]).status_code, status.HTTP_200_OK)

    def test_staff_see_the_leads_they_share_and_cannot_change_who_works_them(self):
        # The Leads module, given to the STAFF role directly (the Roles API offers staff only Activities).
        Role.objects.get(pk=Role.STAFF).permissions.add(*Permission.objects.filter(
            content_type__app_label='leads', codename__in=['view_lead', 'add_lead', 'change_lead'],
        ))
        carol = User.objects.create_user(email='carol@example.com', password=PASSWORD, name='Carol')
        shared = self.add(name='Shared', phone='9876543210', assignee_ids=[self.asha.pk, self.binu.pk])

        for user in (self.asha, self.binu):
            self.as_user(user)
            self.assertEqual(self.listed(), (['Shared'], 1))
        self.as_user(carol)
        self.assertEqual(self.listed(), ([], 0))
        self.assertEqual(self.client.get(reverse('lead-detail', args=[shared.pk])).status_code, status.HTTP_404_NOT_FOUND)

        self.as_user(self.asha)
        for ids in ([self.asha.pk], [self.asha.pk, self.binu.pk, carol.pk], []):
            refused = self.patch(shared, assignee_ids=ids)
            self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST, ids)
            self.assertEqual(refused.data['assignee_ids'][0], 'Only an admin can assign a lead to someone else.')
        # Sending them unchanged, as the edit form does, is fine.
        self.assertEqual(self.patch(shared, assignee_ids=[self.binu.pk, self.asha.pk], district='Kollam').status_code, 200)
        self.assertEqual(set(Lead.objects.get(pk=shared.pk).assignees.all()), {self.asha, self.binu})
        # A lead staff add is theirs alone.
        self.assertEqual(self.post(name='Mine', phone='9876500009', assignee_ids=[self.binu.pk]).status_code, 400)
        mine = self.post(name='Mine', phone='9876500009')
        self.assertEqual(mine.data['assignees'], [{'id': self.asha.pk, 'name': 'Asha Nair'}])

    def test_listing_takes_the_same_number_of_queries_however_many_assignees(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(reverse('lead-list')).status_code, status.HTTP_200_OK)
            return len(queries)

        self.add(name='One', phone='9876500001', assignee_ids=[self.asha.pk])
        few = count()
        for index in range(5):
            self.add(name=f'Lead {index}', phone=f'98765100{index:02d}', assignee_ids=[self.asha.pk, self.binu.pk])
        self.assertEqual(count(), few)

    def test_the_initial_follow_up_can_have_several_assignees_and_each_is_told_once(self):
        follow_up = {'title': 'Call', 'type': 'PHONE_CALL', 'due_date': '2026-10-21'}
        response = self.post(
            name='Ravi', phone='9876543210', assignee_ids=[self.asha.pk],
            initial_follow_up={**follow_up, 'assignee_ids': [self.asha.pk, self.binu.pk, self.admin.pk]},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(set(Activity.objects.get().assignees.all()), {self.asha, self.binu, self.admin})
        told = Notification.objects.filter(kind=NotificationKind.ACTIVITY_ASSIGNED)
        # Not the admin who assigned it.
        self.assertEqual(sorted(told.values_list('recipient__name', flat=True)), ['Asha Nair', 'Binu Nair'])
        missing = self.post(name='Mini', phone='9876500002', initial_follow_up={**follow_up, 'assignee_ids': []})
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assignee_ids', missing.data['initial_follow_up'])
        self.assertEqual(Lead.objects.count(), 1)


class ConversionRegressionTests(LeadTestCase):
    def test_a_planless_lead_moves_to_won_without_a_work_and_converts_once_it_has_a_plan(self):
        lead = self.add(name='Ravi', phone='9876543210', assignee_ids=[self.binu.pk, self.asha.pk])
        self.assertEqual(lead.status, LeadStatus.NEW)
        for target in ('INITIAL_CONTACT', 'HOT', 'SUPERHOT', 'WON'):
            moved = self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': target}, format='json')
            self.assertEqual(moved.status_code, status.HTTP_200_OK, target)
        self.assertFalse(Work.objects.exists())  # reaching Won never converts by itself

        self.assertFalse(self.client.get(reverse('lead-detail', args=[lead.pk])).data['can_convert'])
        refused = self.client.post(reverse('lead-convert', args=[lead.pk]))
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('solar plan', refused.data['detail'])
        self.assertFalse(Work.objects.exists())

        self.patch(lead, plan=self.plan.pk)
        self.assertTrue(self.client.get(reverse('lead-detail', args=[lead.pk])).data['can_convert'])
        converted = self.client.post(reverse('lead-convert', args=[lead.pk]))
        self.assertEqual((converted.status_code, converted.data['status']), (status.HTTP_200_OK, 'WON'))
        work = Work.objects.get()
        # A Work has one assignee: the lead's first by name.
        self.assertEqual((work.plan, work.amount, work.assigned_to), (self.plan, self.plan.amount, self.asha))
        self.assertEqual(self.client.post(reverse('lead-convert', args=[lead.pk])).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Work.objects.count(), 1)
        self.assertEqual(Lead.objects.get(pk=lead.pk).status, LeadStatus.WON)

    def test_lost_is_final_and_never_converts(self):
        lead = self.add(name='Ravi', phone='9876543210', plan=self.plan.pk)
        self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': 'LOST'}, format='json')

        self.assertEqual(self.client.post(reverse('lead-convert', args=[lead.pk])).status_code, status.HTTP_409_CONFLICT)
        reopened = self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': 'WON'}, format='json')
        self.assertEqual(reopened.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(Work.objects.exists())

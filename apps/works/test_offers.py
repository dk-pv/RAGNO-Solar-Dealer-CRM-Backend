"""A special price for one Work (POST /api/works/{id}/offer/): an admin's decision, for that Work alone. Its standard
amount, its plan's price and every other Work stay as they are; what the customer pays (effective_amount) is the offer,
in the lists, totals and reports too."""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, SolarPlan

from .models import Work, WorkStage
from .tests import PASSWORD, convert_lead
from .views import OFFER_CHANGED


class OfferTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.other_admin = User.objects.create_user(email='admin2@example.com', password=PASSWORD, name='Admin Two', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')
        # Staff who can open and move Works (the Work module), so the refusal is about the offer alone.
        Role.objects.get(pk=Role.STAFF).permissions.add(Permission.objects.get(codename='access_work'))
        cls.plan = SolarPlan.objects.get(capacity=5)  # 200000
        # Two customers on the same plan at the same standard price.
        cls.a = convert_lead(cls.plan, cls.admin, name='Customer A', phone='9876500001')
        cls.b = convert_lead(cls.plan, cls.admin, name='Customer B', phone='9876500002')

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def offer(self, work, offer_amount, **fields):
        return self.client.post(reverse('work-offer', args=[work.pk]), {'offer_amount': offer_amount, **fields}, format='json')

    def shown(self, work):
        data = self.client.get(reverse('work-detail', args=[work.pk])).data
        return data['amount'], data['offer_amount'], data['effective_amount']

    def test_an_admin_gives_one_work_an_offer_and_nothing_else_changes(self):
        response = self.offer(self.b, '185000', offer_note='Negotiated at the site visit.')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            (response.data['amount'], response.data['offer_amount'], response.data['effective_amount'], response.data['offer_note']),
            ('200000.00', '185000.00', '185000.00', 'Negotiated at the site visit.'),
        )
        # Stored apart: the standard amount stays, and the effective amount holds after fetching the Work again.
        b = Work.objects.get(pk=self.b.pk)
        self.assertEqual((b.amount, b.offer_amount, b.effective_amount), (Decimal('200000'), Decimal('185000'), Decimal('185000')))
        self.assertEqual(self.shown(self.b), ('200000.00', '185000.00', '185000.00'))
        # The plan's price, the other customer's Work and both leads are untouched; nothing new was created.
        self.assertEqual(SolarPlan.objects.get(pk=self.plan.pk).amount, Decimal('200000'))
        self.assertEqual(self.shown(self.a), ('200000.00', None, '200000.00'))
        self.assertEqual(list(Lead.objects.order_by('pk').values_list('amount', flat=True)), [Decimal('200000')] * 2)
        self.assertEqual((Work.objects.count(), Lead.objects.count()), (2, 2))
        rows = {row['id']: row['effective_amount'] for row in self.client.get(reverse('work-list')).data['results']}
        self.assertEqual(rows, {self.a.pk: '200000.00', self.b.pk: '185000.00'})

    def test_works_without_an_offer_keep_their_standard_amount(self):
        data = self.client.get(reverse('work-detail', args=[self.a.pk])).data

        self.assertEqual((data['amount'], data['offer_amount'], data['offer_note'], data['effective_amount']), ('200000.00', None, '', '200000.00'))

    def test_an_offer_equal_to_the_standard_amount_is_allowed(self):
        self.assertEqual(self.offer(self.b, '200000').data['effective_amount'], '200000.00')
        self.assertEqual(Work.objects.get(pk=self.b.pk).offer_amount, Decimal('200000'))

    def test_invalid_offers_are_refused(self):
        for value in ['0', '0.00', '-1', '200000.01', '250000', '999999999999', 'abc', '185000.555']:
            with self.subTest(value=value):
                refused = self.offer(self.b, value)
                self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('offer_amount', refused.data)
        self.assertEqual(self.offer(self.b, '200000.01').data['offer_amount'], ["The offer price can't be more than the Work's standard amount."])
        self.assertEqual(self.offer(self.b, '0').data['offer_amount'], ['Enter an offer price greater than zero.'])
        missing = self.client.post(reverse('work-offer', args=[self.b.pk]), {}, format='json')
        self.assertIn('offer_amount', missing.data)
        self.assertIn('offer_note', self.offer(self.b, '185000', offer_note='x' * 1001).data)
        self.assertEqual(self.shown(self.b), ('200000.00', None, '200000.00'))

    def test_staff_cant_apply_change_or_remove_an_offer(self):
        self.offer(self.b, '185000')
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))

        for work, value in ((self.a, '150000'), (self.b, '150000'), (self.b, None)):
            with self.subTest(work=work.customer_name, value=value):
                self.assertEqual(self.offer(work, value).status_code, status.HTTP_403_FORBIDDEN)
        # Editing the Work, which the Work module allows, can't carry an offer either: those fields are read-only.
        edited = self.client.patch(reverse('work-detail', args=[self.a.pk]), {'offer_amount': '1', 'amount': '1'}, format='json')
        self.assertEqual(edited.status_code, status.HTTP_200_OK)
        self.assertEqual(self.shown(self.a), ('200000.00', None, '200000.00'))
        self.assertEqual(self.shown(self.b), ('200000.00', '185000.00', '185000.00'))
        # Signed out: no answer at all.
        self.client.force_authenticate(None)
        self.assertEqual(self.offer(self.a, '150000').status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_unknown_work_is_not_found(self):
        self.assertEqual(self.client.post('/api/works/999999/offer/', {'offer_amount': '1'}, format='json').status_code, 404)

    def test_removing_the_offer_brings_back_the_standard_amount(self):
        self.offer(self.b, '185000', offer_note='Festival offer.')

        removed = self.offer(self.b, None, offer_note='ignored')

        self.assertEqual(removed.status_code, status.HTTP_200_OK, removed.data)
        self.assertEqual((removed.data['offer_amount'], removed.data['offer_note'], removed.data['effective_amount']), (None, '', '200000.00'))
        # An empty price is no price, as DRF reads a nullable number: it removes an offer too.
        self.offer(self.b, '185000')
        self.assertEqual(self.offer(self.b, '').data['offer_amount'], None)

    def test_a_repeated_offer_changes_nothing_and_a_concurrent_change_is_refused(self):
        first = self.offer(self.b, '185000', expected_offer_amount=None)
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        # The same request again (a double click, a retry) is harmless: nothing changes, not even the Work's time.
        again = self.offer(self.b, '185000.00', expected_offer_amount=None)
        self.assertEqual((again.status_code, again.data['updated_at']), (status.HTTP_200_OK, first.data['updated_at']))
        # Another admin who saw no offer can't silently replace this one.
        self.client.force_authenticate(self.other_admin)
        refused = self.offer(self.b, '180000', expected_offer_amount=None)
        self.assertEqual((refused.status_code, refused.data['detail']), (status.HTTP_409_CONFLICT, OFFER_CHANGED))
        self.assertEqual(Work.objects.get(pk=self.b.pk).offer_amount, Decimal('185000'))
        # Seeing the current offer, they can change it.
        changed = self.offer(self.b, '180000', expected_offer_amount='185000.00')
        self.assertEqual((changed.status_code, changed.data['effective_amount']), (status.HTTP_200_OK, '180000.00'))

    def test_the_database_refuses_an_offer_outside_the_standard_amount(self):
        for value in (Decimal('200000.01'), Decimal('0'), Decimal('-5')):
            with self.subTest(value=value), self.assertRaises(IntegrityError), transaction.atomic():
                Work.objects.filter(pk=self.b.pk).update(offer_amount=value)

    def test_who_applied_an_offer_goes_to_the_server_log(self):
        with self.assertLogs('apps.works.views', 'WARNING') as logs, self.captureOnCommitCallbacks(execute=True):
            self.offer(self.b, '185000')

        self.assertIn(f'Work {self.b.pk}: offer set to 185000.00 by user {self.admin.pk}', logs.output[0])

    def test_totals_sorting_and_reports_use_what_each_customer_pays(self):
        c = convert_lead(self.plan, self.admin, name='Customer C', phone='9876500003', amount=Decimal('180000'))
        self.offer(self.b, '150000')
        Work.objects.filter(pk=c.pk).update(stage=WorkStage.COMPLETED)

        summary = {row['stage']: row['total_amount'] for row in self.client.get(reverse('work-summary')).data}
        self.assertEqual((summary['LOAN_DOCUMENTS'], summary['COMPLETED']), ('350000.00', '180000.00'))
        ordered = [row['customer_name'] for row in self.client.get(reverse('work-list'), {'ordering': 'amount'}).data['results']]
        self.assertEqual(ordered, ['Customer B', 'Customer C', 'Customer A'])
        ordered = [row['customer_name'] for row in self.client.get(reverse('work-list'), {'ordering': '-amount'}).data['results']]
        self.assertEqual(ordered, ['Customer A', 'Customer C', 'Customer B'])

        report = self.client.get(reverse('report-works-summary')).data
        self.assertEqual(
            report['totals'], {'created': 3, 'amount': '530000.00', 'completed': 1, 'completed_amount': '180000.00'},
        )
        rows = self.client.get(reverse('report-works'), {'ordering': 'amount'}).data['results']
        self.assertEqual(
            [(row['customer_name'], row['amount'], row['offer_applied']) for row in rows],
            [('Customer B', '150000.00', True), ('Customer C', '180000.00', False), ('Customer A', '200000.00', False)],
        )

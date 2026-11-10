"""Work Type: an optional Loan Work / Cash Work on leads, carried into the Work on conversion, separate from the stage."""

import csv
import io

from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.works.models import Work, WorkStage

from .models import Lead, LeadStatus, SolarPlan, WorkType

PASSWORD = 'Solar-Panel-2026'
NEW_LEAD = {'name': 'Asha Menon', 'phone': '9876543210'}


class WorkTypeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def add_lead(self, **fields):
        return self.client.post(reverse('lead-list'), {**NEW_LEAD, **fields}, format='json')

    def won_lead(self, **fields):
        lead = Lead.objects.create(
            name='Ravi', phone='9876500001', plan=self.plan, amount=self.plan.amount, created_by=self.admin,
            status=LeadStatus.SUPERHOT, **fields,
        )
        lead.move_to(LeadStatus.WON)
        return lead

    def test_a_lead_is_added_with_loan_work_with_cash_work_or_without_a_work_type(self):
        for value in ('LOAN', 'CASH', ''):
            with self.subTest(value=value):
                response = self.add_lead(work_type=value, phone=f'98765{len(value)}0000')
                self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
                self.assertEqual(response.data['work_type'], value)
                self.assertEqual(Lead.objects.get(pk=response.data['id']).work_type, value)
        # Left out altogether: none, and the lead still starts as New.
        response = self.add_lead(phone='9876599999')
        self.assertEqual((response.data['work_type'], response.data['status']), ('', LeadStatus.NEW))

    def test_an_unknown_work_type_is_refused_by_the_api_and_the_database(self):
        response = self.add_lead(work_type='EMI')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('work_type', response.data)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Lead.objects.create(**NEW_LEAD, created_by=self.admin, work_type='EMI')

    def test_editing_a_lead_sets_changes_and_clears_its_work_type(self):
        lead_id = self.add_lead().data['id']
        url = reverse('lead-detail', args=[lead_id])
        for value in ('CASH', 'LOAN', ''):
            with self.subTest(value=value):
                response = self.client.patch(url, {'work_type': value}, format='json')
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(Lead.objects.get(pk=lead_id).work_type, value)

    def test_existing_leads_and_works_have_no_work_type_and_keep_their_stage(self):
        lead = self.won_lead()
        lead.convert(self.admin)
        self.assertEqual(Lead.objects.get(pk=lead.pk).work_type, '')
        self.assertEqual((lead.work.work_type, lead.work.stage), ('', WorkStage.LOAN_DOCUMENTS))

    def test_converting_carries_the_leads_work_type_into_the_work_and_leaves_none_empty(self):
        for value in (WorkType.LOAN, WorkType.CASH, ''):
            with self.subTest(value=value):
                lead = self.won_lead(work_type=value)
                response = self.client.post(reverse('lead-convert', args=[lead.pk]))
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                work = Work.objects.get(lead=lead)
                self.assertEqual(work.work_type, value)
                # The work type is not a stage: the Work starts at the first stage whatever its type.
                self.assertEqual(work.stage, WorkStage.LOAN_DOCUMENTS)

    def test_conversion_is_still_once_only_and_never_changes_the_lead(self):
        lead = self.won_lead(work_type=WorkType.CASH)
        self.client.post(reverse('lead-convert', args=[lead.pk]))
        second = self.client.post(reverse('lead-convert', args=[lead.pk]))
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Work.objects.filter(lead=lead).count(), 1)
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.work_type), (LeadStatus.WON, WorkType.CASH))

    def test_a_works_work_type_is_shown_and_can_be_set_later_without_touching_its_stage_or_amount(self):
        lead = self.won_lead()
        lead.convert(self.admin)
        url = reverse('work-detail', args=[lead.work.pk])
        self.assertEqual(self.client.get(url).data['work_type'], '')

        response = self.client.patch(url, {'work_type': 'LOAN'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        work = Work.objects.get(pk=lead.work.pk)
        self.assertEqual((work.work_type, work.stage, work.amount), ('LOAN', WorkStage.LOAN_DOCUMENTS, self.plan.amount))
        self.assertEqual(self.client.patch(url, {'work_type': 'EMI'}, format='json').status_code, 400)

    def test_lists_filter_by_work_type_or_by_none(self):
        loan = self.add_lead(work_type='LOAN', phone='9876500011').data['id']
        cash = self.add_lead(work_type='CASH', phone='9876500012').data['id']
        unset = self.add_lead(phone='9876500013').data['id']

        def ids(work_type):
            response = self.client.get(reverse('lead-list'), {'work_type': work_type})
            return {lead['id'] for lead in response.data['results']}

        self.assertEqual((ids('LOAN'), ids('CASH'), ids('none')), ({loan}, {cash}, {unset}))
        self.assertEqual(self.client.get(reverse('lead-list'), {'work_type': 'EMI'}).status_code, 400)

        for value in ('LOAN', ''):
            self.won_lead(work_type=value).convert(self.admin)
        works = self.client.get(reverse('work-list'), {'work_type': 'LOAN'}).data['results']
        self.assertEqual([work['work_type'] for work in works], ['LOAN'])
        self.assertEqual(len(self.client.get(reverse('work-list'), {'work_type': 'none'}).data['results']), 1)

    def test_the_export_has_a_work_type_column(self):
        self.add_lead(work_type='CASH')
        rows = list(csv.reader(io.StringIO(self.client.get(reverse('lead-export')).content.decode('utf-8-sig'))))
        column = rows[0].index('Work type')
        self.assertEqual(rows[1][column], 'Cash Work')

"""Admin-managed pipeline stages: the system stages, admins' own stages in both pipelines, the Lead Pipeline's rules with
them, and safe deletion (records move first, never into another pipeline or an outcome)."""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.notifications.models import Notification, NotificationKind
from apps.works.models import Work, WorkStage

from .models import Lead, LeadConflict, LeadStatus, Pipeline, PipelineStage, SolarPlan

PASSWORD = 'Solar-Panel-2026'
S = LeadStatus
LEAD_KEYS = ['NEW', 'INITIAL_CONTACT', 'HOT', 'SUPERHOT', 'WON', 'LOST']
WORK_KEYS = [
    'LOAN_DOCUMENTS', 'FEASIBILITY', 'STRUCTURE', 'ELECTRICAL', 'KSEB_DOCUMENTATION', 'SUBSIDY_DOCUMENTATION', 'COMPLETED',
]


class StageTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.other_admin = User.objects.create_user(email='boss@example.com', password=PASSWORD, name='Boss', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Priya Nair')
        cls.plan = SolarPlan.objects.get(capacity=5)

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def keys(self, pipeline):
        return list(PipelineStage.objects.filter(pipeline=pipeline).values_list('key', flat=True))

    def add_stage(self, pipeline, name):
        response = self.client.post(reverse('pipeline-stage-list'), {'pipeline': pipeline, 'name': name}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return PipelineStage.objects.get(pipeline=pipeline, name=name)

    def move(self, stage, direction):
        return self.client.post(reverse('pipeline-stage-move', args=[stage.pk]), {'direction': direction}, format='json')

    def delete(self, stage, move_to=None):
        url = reverse('pipeline-stage-detail', args=[stage.pk])
        return self.client.delete(f'{url}?move_to={move_to}' if move_to else url)

    def lead(self, status=S.NEW, amount=None, **fields):
        return Lead.objects.create(
            name='Asha Menon', phone='9876543210', plan=self.plan, amount=amount or self.plan.amount,
            created_by=self.admin, status=status, **fields,
        )

    def work(self, stage=WorkStage.LOAN_DOCUMENTS):
        lead = self.lead(S.SUPERHOT)
        lead.move_to(S.WON)
        lead.convert(self.admin)
        Work.objects.filter(pk=lead.work.pk).update(stage=stage)
        return Work.objects.get(pk=lead.work.pk)

    def set_status(self, lead, status):
        return self.client.post(reverse('lead-change-status', args=[lead.pk]), {'status': status}, format='json')


class SystemStageTests(StageTestCase):
    def test_the_system_stages_are_seeded_in_order_with_the_keys_records_already_use(self):
        self.assertEqual(self.keys(Pipeline.LEAD), LEAD_KEYS)
        self.assertEqual(self.keys(Pipeline.WORK), WORK_KEYS)
        self.assertTrue(all(stage.is_system and stage.is_active for stage in PipelineStage.objects.all()))
        names = dict(PipelineStage.objects.values_list('key', 'name'))
        self.assertEqual((names['INITIAL_CONTACT'], names['LOAN_DOCUMENTS']), ('Initial Contact', 'Loan Work / Documents'))

    def test_every_key_fits_the_columns_that_store_it_and_the_longest_can_be_set(self):
        # Postgres refuses a value wider than its column and SQLite never checks: SUBSIDY_DOCUMENTATION is 21 characters.
        width = PipelineStage._meta.get_field('key').max_length
        lead_keys = [*LEAD_KEYS, self.add_stage(Pipeline.LEAD, 'Site Visit').key]
        work_keys = [*WORK_KEYS, self.add_stage(Pipeline.WORK, 'Inspection').key]
        for keys, column in ((lead_keys, Lead._meta.get_field('status')), (work_keys, Work._meta.get_field('stage'))):
            for key in keys:
                self.assertLessEqual(len(key), min(width, column.max_length), key)

        longest, work = max(work_keys, key=len), self.work()
        bulk = self.client.post(reverse('work-bulk-stage'), {'ids': [work.pk], 'stage': longest}, format='json')
        self.assertEqual(bulk.status_code, status.HTTP_200_OK, bulk.data)
        self.assertEqual(bulk.data['succeeded'], [work.pk])
        listed = self.client.get(reverse('work-list'), {'stage': longest})
        self.assertEqual([row['id'] for row in listed.data['results']], [work.pk])
        Work.objects.filter(pk=work.pk).update(stage=WorkStage.LOAN_DOCUMENTS)
        patch = self.client.patch(reverse('work-detail', args=[work.pk]), {'stage': longest}, format='json')
        self.assertEqual(patch.status_code, status.HTTP_200_OK, patch.data)
        self.assertEqual(Work.objects.get(pk=work.pk).stage, 'SUBSIDY_DOCUMENTATION')

    def test_every_signed_in_user_reads_the_stages_but_only_admins_see_record_counts(self):
        self.lead(S.HOT)
        self.client.force_authenticate(self.staff)
        response = self.client.get(reverse('pipeline-stage-list'), {'pipeline': 'LEAD'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([stage['key'] for stage in response.data], LEAD_KEYS)
        self.assertTrue(all(stage['record_count'] is None for stage in response.data))

        self.client.force_authenticate(self.admin)
        rows = {stage['key']: stage for stage in self.client.get(reverse('pipeline-stage-list')).data}
        self.assertEqual((rows['HOT']['record_count'], rows['NEW']['record_count']), (1, 0))
        self.assertEqual((rows['NEW']['is_first'], rows['WON']['is_last'], rows['COMPLETED']['is_last']), (True, True, True))
        self.assertEqual(self.client.get(reverse('pipeline-stage-list'), {'pipeline': 'X'}).status_code, 400)

        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('pipeline-stage-list')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_system_stages_are_never_renamed_moved_or_deleted(self):
        for key in ('NEW', 'HOT', 'WON', 'LOST', 'LOAN_DOCUMENTS', 'COMPLETED'):
            stage = PipelineStage.objects.get(key=key)
            with self.subTest(key):
                url = reverse('pipeline-stage-detail', args=[stage.pk])
                self.assertEqual(self.client.patch(url, {'name': 'Other'}, format='json').status_code, 403)
                self.assertEqual(self.move(stage, 'up').status_code, 403)
                self.assertEqual(self.delete(stage).status_code, 403)
                self.assertEqual(self.delete(stage, move_to='HOT').status_code, 403)
        self.assertEqual(PipelineStage.objects.filter(is_system=True).count(), 13)
        self.assertEqual(PipelineStage.objects.get(key='NEW').name, 'New')


class StageManagementTests(StageTestCase):
    def test_an_admin_adds_a_stage_to_each_pipeline_just_before_its_last_stages(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        inspection = self.add_stage(Pipeline.WORK, 'Inspection')

        self.assertEqual(self.keys(Pipeline.LEAD), [*LEAD_KEYS[:4], visit.key, 'WON', 'LOST'])
        self.assertEqual(self.keys(Pipeline.WORK), [*WORK_KEYS[:6], inspection.key, 'COMPLETED'])
        self.assertTrue(visit.key.startswith('CUSTOM_') and not visit.is_system)
        self.assertEqual(list(PipelineStage.objects.filter(pipeline=Pipeline.LEAD).values_list('order', flat=True)), list(range(7)))
        # The answer is the pipeline's stages, ready for Settings to show.
        response = self.client.post(reverse('pipeline-stage-list'), {'pipeline': 'WORK', 'name': 'Handover'}, format='json')
        self.assertEqual([stage['name'] for stage in response.data][-3:], ['Inspection', 'Handover', 'Completed'])

    def test_staff_cannot_add_rename_move_or_delete_stages_even_through_the_api(self):
        stage = self.add_stage(Pipeline.LEAD, 'Site Visit')
        staff = self.staff
        # Even with every lead permission and the Work module.
        Role.objects.get(pk=Role.STAFF).permissions.add(
            *Permission.objects.filter(codename__in=['view_lead', 'add_lead', 'change_lead', 'delete_lead', 'access_work']),
        )
        self.client.force_authenticate(User.objects.get(pk=staff.pk))
        url = reverse('pipeline-stage-detail', args=[stage.pk])
        attempts = [
            self.client.post(reverse('pipeline-stage-list'), {'pipeline': 'LEAD', 'name': 'Mine'}, format='json'),
            self.client.patch(url, {'name': 'Renamed'}, format='json'),
            self.move(stage, 'up'),
            self.delete(stage),
            self.delete(stage, move_to='HOT'),
        ]
        self.assertEqual([response.status_code for response in attempts], [403] * 5)
        stage.refresh_from_db()
        self.assertEqual((stage.name, PipelineStage.objects.count()), ('Site Visit', 14))

    def test_duplicate_names_are_refused_within_a_pipeline_in_any_case_but_allowed_across_pipelines(self):
        self.add_stage(Pipeline.LEAD, 'Site Visit')
        for name in ('site visit', ' SITE VISIT ', 'won', 'Lost'):
            with self.subTest(name):
                response = self.client.post(reverse('pipeline-stage-list'), {'pipeline': 'LEAD', 'name': name}, format='json')
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('name', response.data)
        self.add_stage(Pipeline.WORK, 'Site Visit')
        other = self.add_stage(Pipeline.LEAD, 'Quoted')
        rename = self.client.patch(reverse('pipeline-stage-detail', args=[other.pk]), {'name': 'SITE visit'}, format='json')
        self.assertEqual(rename.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(reverse('pipeline-stage-list'), {'pipeline': 'LEAD', 'name': '  '}, format='json').status_code, 400)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PipelineStage.objects.create(pipeline=Pipeline.LEAD, key='CUSTOM_DUP', name='SITE VISIT')

    def test_renaming_a_custom_stage_keeps_its_key_and_its_records(self):
        stage = self.add_stage(Pipeline.LEAD, 'Site Visit')
        lead = self.lead(stage.key)

        response = self.client.patch(reverse('pipeline-stage-detail', args=[stage.pk]), {'name': 'Survey done'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        stage.refresh_from_db()
        lead.refresh_from_db()
        self.assertEqual((stage.name, lead.status, lead.get_status_display()), ('Survey done', stage.key, 'Survey done'))
        moved = self.client.patch(reverse('pipeline-stage-detail', args=[stage.pk]), {'pipeline': 'WORK'}, format='json')
        self.assertEqual(moved.status_code, status.HTTP_400_BAD_REQUEST)

    def test_custom_stages_move_up_and_down_but_never_before_the_first_or_after_the_last_stages(self):
        stage = self.add_stage(Pipeline.LEAD, 'Site Visit')
        self.assertEqual(self.move(stage, 'down').status_code, status.HTTP_400_BAD_REQUEST)  # Won stays after it
        for expected in (['NEW', 'INITIAL_CONTACT', 'HOT', stage.key, 'SUPERHOT'], ['NEW', 'INITIAL_CONTACT', stage.key, 'HOT'],
                         ['NEW', stage.key, 'INITIAL_CONTACT']):
            response = self.move(stage, 'up')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(self.keys(Pipeline.LEAD)[:len(expected)], expected)
        self.assertEqual(self.move(stage, 'up').status_code, status.HTTP_400_BAD_REQUEST)  # New stays first
        self.assertEqual(self.move(stage, 'sideways').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.keys(Pipeline.LEAD)[-2:], ['WON', 'LOST'])

        work_stage = self.add_stage(Pipeline.WORK, 'Inspection')
        self.assertEqual(self.move(work_stage, 'down').status_code, status.HTTP_400_BAD_REQUEST)  # Completed stays last
        self.move(work_stage, 'up')
        self.assertEqual(self.keys(Pipeline.WORK)[-3:], [work_stage.key, 'SUBSIDY_DOCUMENTATION', 'COMPLETED'])


class PipelineRuleTests(StageTestCase):
    def test_a_lead_stage_is_refused_for_a_work_and_a_work_stage_for_a_lead(self):
        lead_stage = self.add_stage(Pipeline.LEAD, 'Site Visit')
        work_stage = self.add_stage(Pipeline.WORK, 'Inspection')
        lead, work = self.lead(S.NEW), self.work()

        for key in ('FEASIBILITY', 'COMPLETED', work_stage.key):
            with self.subTest(lead_status=key):
                self.assertEqual(self.set_status(lead, key).status_code, status.HTTP_400_BAD_REQUEST)
                bulk = self.client.post(reverse('lead-bulk-status'), {'ids': [lead.pk], 'status': key}, format='json')
                self.assertEqual(bulk.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(self.client.get(reverse('lead-list'), {'status': key}).status_code, 400)
        for key in ('NEW', 'WON', lead_stage.key):
            with self.subTest(work_stage=key):
                patch = self.client.patch(reverse('work-detail', args=[work.pk]), {'stage': key}, format='json')
                self.assertEqual(patch.status_code, status.HTTP_400_BAD_REQUEST)
                bulk = self.client.post(reverse('work-bulk-stage'), {'ids': [work.pk], 'stage': key}, format='json')
                self.assertEqual(bulk.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(self.client.get(reverse('work-list'), {'stage': key}).status_code, 400)
        lead.refresh_from_db()
        work.refresh_from_db()
        self.assertEqual((lead.status, work.stage), (S.NEW, WorkStage.LOAN_DOCUMENTS))

    def test_leads_move_forward_through_custom_stages_in_the_board_order_and_never_back(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        self.move(visit, 'up')  # between Hot and Superhot
        hot = self.lead(S.HOT)

        response = self.client.get(reverse('lead-detail', args=[hot.pk]))
        self.assertEqual(response.data['allowed_transitions'], [visit.key, 'SUPERHOT', 'WON', 'LOST'])
        self.assertEqual(self.set_status(hot, visit.key).status_code, status.HTTP_200_OK)
        self.assertEqual(self.set_status(hot, S.HOT).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.set_status(hot, S.NEW).status_code, status.HTTP_409_CONFLICT)
        hot.refresh_from_db()
        self.assertEqual(hot.allowed_transitions(), ['SUPERHOT', 'WON', 'LOST'])
        self.assertEqual(self.set_status(hot, S.LOST).status_code, status.HTTP_200_OK)
        hot.refresh_from_db()
        self.assertEqual(hot.allowed_transitions(), [])  # Lost is final, custom stages or not

    def test_conversion_rules_are_unchanged_with_custom_stages(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        lead = self.lead(visit.key)
        refused = self.client.post(reverse('lead-convert', args=[lead.pk]))
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(self.client.get(reverse('lead-detail', args=[lead.pk])).data['can_convert'])

        self.assertEqual(self.set_status(lead, S.WON).status_code, status.HTTP_200_OK)
        self.assertFalse(Work.objects.filter(lead=lead).exists())  # reaching Won never creates the Work
        self.assertEqual(self.client.post(reverse('lead-convert', args=[lead.pk])).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post(reverse('lead-convert', args=[lead.pk])).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Work.objects.filter(lead=lead).count(), 1)

    def test_works_move_to_custom_stages_in_either_direction_and_completion_still_notifies(self):
        inspection = self.add_stage(Pipeline.WORK, 'Inspection')
        work = self.work(WorkStage.FEASIBILITY)
        url = reverse('work-detail', args=[work.pk])

        self.assertEqual(self.client.patch(url, {'stage': inspection.key}, format='json').status_code, 200)
        self.assertEqual(self.client.patch(url, {'stage': 'LOAN_DOCUMENTS'}, format='json').status_code, 200)
        bulk = self.client.post(reverse('work-bulk-stage'), {'ids': [work.pk], 'stage': inspection.key}, format='json')
        self.assertEqual(bulk.data['succeeded'], [work.pk])
        self.assertEqual(Work.objects.get(pk=work.pk).stage, inspection.key)

        completed = Notification.objects.filter(work=work, recipient=self.other_admin, kind=NotificationKind.WORK_COMPLETED)
        self.assertEqual(completed.count(), 0)
        self.client.patch(url, {'stage': 'COMPLETED'}, format='json')
        self.assertEqual(completed.count(), 1)

    def test_a_stage_deleted_meanwhile_refuses_the_move_rather_than_leave_a_record_in_no_stage(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        lead = self.lead(S.NEW)
        key = visit.key
        visit.delete()
        with self.assertRaises(LeadConflict):
            lead.move_to(key)
        lead.refresh_from_db()
        self.assertEqual(lead.status, S.NEW)


class StageDeletionTests(StageTestCase):
    def test_an_empty_custom_stage_is_deleted_and_the_rest_renumbered(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        self.move(visit, 'up')

        response = self.delete(visit)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([stage['key'] for stage in response.data], LEAD_KEYS)
        self.assertEqual(list(PipelineStage.objects.filter(pipeline=Pipeline.LEAD).values_list('order', flat=True)), list(range(6)))

    def test_a_stage_with_records_needs_a_destination_and_says_how_many(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        leads = [self.lead(visit.key), self.lead(visit.key)]

        response = self.delete(visit)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['record_count'], 2)
        self.assertIn('2 leads', response.data['detail'])
        self.assertTrue(PipelineStage.objects.filter(pk=visit.pk).exists())
        self.assertEqual({Lead.objects.get(pk=lead.pk).status for lead in leads}, {visit.key})

    def test_records_move_to_the_chosen_stage_and_then_the_stage_is_deleted(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        leads = [self.lead(visit.key), self.lead(visit.key)]
        other = self.lead(S.HOT)
        before = Lead.objects.get(pk=leads[0].pk).updated_at

        with self.assertLogs('apps.leads.views', 'WARNING') as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.delete(visit, move_to='INITIAL_CONTACT')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(PipelineStage.objects.filter(pk=visit.pk).exists())
        moved = [Lead.objects.get(pk=lead.pk) for lead in leads]
        self.assertEqual({lead.status for lead in moved}, {S.INITIAL_CONTACT})
        self.assertGreater(moved[0].updated_at, before)
        self.assertEqual(Lead.objects.get(pk=other.pk).status, S.HOT)  # only the stage's own leads move
        self.assertIn('2 records moved to INITIAL_CONTACT', logs.output[0])

        inspection = self.add_stage(Pipeline.WORK, 'Inspection')
        work = self.work(inspection.key)
        self.assertEqual(self.delete(inspection, move_to='STRUCTURE').status_code, status.HTTP_200_OK)
        self.assertEqual(Work.objects.get(pk=work.pk).stage, WorkStage.STRUCTURE)

    def test_the_destination_must_be_another_open_stage_of_the_same_pipeline(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        inspection = self.add_stage(Pipeline.WORK, 'Inspection')
        lead, work = self.lead(visit.key), self.work(inspection.key)

        for move_to in ('WON', 'LOST', 'FEASIBILITY', inspection.key, visit.key, 'UNKNOWN'):
            with self.subTest(lead_destination=move_to):
                response = self.delete(visit, move_to=move_to)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('move_to', response.data)
        for move_to in ('COMPLETED', 'NEW', visit.key):
            with self.subTest(work_destination=move_to):
                self.assertEqual(self.delete(inspection, move_to=move_to).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PipelineStage.objects.filter(pk__in=[visit.pk, inspection.pk]).count(), 2)
        self.assertEqual((Lead.objects.get(pk=lead.pk).status, Work.objects.get(pk=work.pk).stage), (visit.key, inspection.key))


class StageCountTests(StageTestCase):
    def test_summaries_count_and_total_every_stage_in_board_order(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        self.move(visit, 'up')
        self.lead(visit.key, amount=Decimal('100000'))
        self.lead(visit.key, amount=Decimal('50000.50'))
        self.lead(S.HOT, amount=Decimal('1'))

        rows = self.client.get(reverse('lead-summary')).data
        self.assertEqual([row['status'] for row in rows], ['NEW', 'INITIAL_CONTACT', 'HOT', visit.key, 'SUPERHOT', 'WON', 'LOST'])
        custom = rows[3]
        self.assertEqual((custom['label'], custom['count'], custom['total_amount']), ('Site Visit', 2, '150000.50'))
        filtered = self.client.get(reverse('lead-summary'), {'status': visit.key}).data
        self.assertEqual(sum(row['count'] for row in filtered), 2)

        inspection = self.add_stage(Pipeline.WORK, 'Inspection')
        self.work(inspection.key)
        work_rows = self.client.get(reverse('work-summary')).data
        self.assertEqual([row['stage'] for row in work_rows][-2:], [inspection.key, 'COMPLETED'])
        self.assertEqual((work_rows[-2]['count'], work_rows[-2]['total_amount']), (1, '200000.00'))

    def test_sorting_by_status_or_stage_follows_the_board_order(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        self.move(visit, 'up')
        self.move(visit, 'up')  # between Initial Contact and Hot
        for status_ in (S.HOT, visit.key, S.NEW):
            self.lead(status_)
        rows = self.client.get(reverse('lead-list'), {'ordering': 'status'}).data['results']
        self.assertEqual([row['status'] for row in rows], [S.NEW, visit.key, S.HOT])

        inspection = self.add_stage(Pipeline.WORK, 'Inspection')
        for stage in (WorkStage.COMPLETED, inspection.key, WorkStage.LOAN_DOCUMENTS):
            self.work(stage)
        rows = self.client.get(reverse('work-list'), {'ordering': 'stage'}).data['results']
        self.assertEqual([row['stage'] for row in rows], [WorkStage.LOAN_DOCUMENTS, inspection.key, WorkStage.COMPLETED])

    def test_dashboard_and_reports_count_a_custom_stage_as_open_and_name_it(self):
        visit = self.add_stage(Pipeline.LEAD, 'Site Visit')
        self.lead(visit.key, next_follow_up='2026-11-01')
        self.lead(S.LOST)

        dashboard = self.client.get(reverse('dashboard-summary')).data['leads']
        self.assertEqual((dashboard['active'], dashboard['lost']), (1, 1))
        self.assertIn({'status': visit.key, 'label': 'Site Visit', 'count': 1}, dashboard['by_status'])

        report = self.client.get(reverse('report-leads-summary')).data
        self.assertEqual(report['totals']['open'], 1)
        self.assertIn({'value': visit.key, 'label': 'Site Visit', 'count': 1}, report['by_status'])
        rows = self.client.get(reverse('report-leads'), {'status': visit.key}).data['results']
        self.assertEqual([row['status_display'] for row in rows], ['Site Visit'])
        recent = self.client.get(reverse('dashboard-recent')).data['leads']
        self.assertIn('Site Visit', [row['status_display'] for row in recent])
        self.assertEqual(self.client.get(reverse('report-leads'), {'status': 'COMPLETED'}).status_code, 400)

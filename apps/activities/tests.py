from datetime import date

from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.notifications.models import Notification

from .models import Activity, ActivityStatus

PASSWORD = 'Solar-Panel-2026'


def grant(role, *perms):
    for perm in perms:
        app_label, codename = perm.split('.')
        role.permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))


class FollowUpTestCase(APITestCase):
    """An admin; Staff A and Staff B with the Leads and Activities modules; a lead for each and an unassigned one."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff_a = User.objects.create_user(email='a@example.com', password=PASSWORD, name='Staff A')
        cls.staff_b = User.objects.create_user(email='b@example.com', password=PASSWORD, name='Staff B')
        # Written to the role directly: Settings -> Roles & Access gives staff only Activities now, and these tests show
        # the activity rules hold even for staff who also have the Leads module (test_staff_access.py: Activities only).
        grant(Role.objects.get(name=Role.STAFF), 'leads.view_lead', 'leads.add_lead', 'leads.change_lead',
              'accounts.access_activities')
        cls.plan = SolarPlan.objects.get(capacity=5)
        lead = {'district': 'Ernakulam', 'plan': cls.plan, 'amount': cls.plan.amount, 'created_by': cls.admin}
        cls.a_lead = Lead.objects.create(name='Asha Menon', phone='9876500001', assigned_to=cls.staff_a, **lead)
        cls.b_lead = Lead.objects.create(name='Ravi Kumar', phone='9876500002', assigned_to=cls.staff_b, **lead)
        cls.pool = Lead.objects.create(name='Pool Lead', phone='9876500003', **lead)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def add(self, lead, **fields):
        body = {
            'lead': lead.pk, 'title': 'Call about the quotation', 'type': 'PHONE_CALL',
            'assigned_to': (lead.assigned_to or self.admin).pk, 'due_date': '2026-10-03',
            'description': 'Customer asked for a callback.', **fields,
        }
        return self.client.post(reverse('activity-list'), body, format='json')

    def page(self, **params):
        response = self.client.get(reverse('activity-list'), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def titles(self, **params):
        return [row['title'] for row in self.page(**params)['results']]

    def patch(self, activity_id, **body):
        return self.client.patch(reverse('activity-detail', args=[activity_id]), body, format='json')

    def complete(self, activity_id, **body):
        return self.client.post(reverse('activity-complete', args=[activity_id]), body, format='json')


class FollowUpFieldsTests(FollowUpTestCase):
    def test_a_follow_up_has_a_heading_type_staff_due_date_and_notes_and_starts_pending(self):
        self.as_user(self.admin)
        created = self.add(self.b_lead, status='COMPLETED')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        data = created.data
        self.assertEqual(
            (data['title'], data['type_display'], data['assigned_to'], data['assigned_to_name'], data['due_date'],
             data['description'], data['status'], data['created_by_name']),
            ('Call about the quotation', 'Phone call', self.staff_b.pk, 'Staff B', '2026-10-03',
             'Customer asked for a callback.', 'PENDING', 'Admin'),
        )
        stored = Activity.objects.get(pk=data['id'])
        self.assertEqual((stored.title, stored.assigned_to, stored.status), ('Call about the quotation', self.staff_b, 'PENDING'))

    def test_heading_staff_and_due_date_are_required_and_notes_are_not(self):
        self.as_user(self.admin)
        for field in ('title', 'assigned_to', 'due_date', 'type'):
            body = {'lead': self.a_lead.pk, 'title': 'Call', 'type': 'PHONE_CALL', 'assigned_to': self.staff_a.pk,
                    'due_date': '2026-10-03'}
            del body[field]
            response = self.client.post(reverse('activity-list'), body, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, field)
            self.assertIn(field, response.data)
        for bad in ({'title': '   '}, {'due_date': None}, {'due_date': '03-10-2026'}, {'type': 'CONFIRMED'},
                    {'assigned_to': 999999}, {'description': None}):
            self.assertEqual(self.add(self.a_lead, **bad).status_code, status.HTTP_400_BAD_REQUEST, bad)
        self.assertEqual(self.add(self.a_lead, description='').status_code, status.HTTP_201_CREATED)
        self.assertFalse(Activity.objects.filter(title='').exists())

    def test_a_follow_up_can_be_assigned_apart_from_the_lead_and_never_changes_the_lead(self):
        self.as_user(self.admin)
        created = self.add(self.a_lead, assigned_to=self.staff_b.pk)
        self.assertEqual(created.data['assigned_to_name'], 'Staff B')
        self.assertEqual(Lead.objects.get(pk=self.a_lead.pk).assigned_to, self.staff_a)
        inactive = User.objects.create_user(email='gone@example.com', password=PASSWORD, name='Gone', is_active=False)
        self.assertEqual(self.add(self.a_lead, assigned_to=inactive.pk).status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_can_not_add_or_edit_follow_ups_even_their_own(self):
        self.as_user(self.staff_a)
        for assignee in (self.staff_a, self.staff_b):
            self.assertEqual(self.add(self.a_lead, assigned_to=assignee.pk).status_code, 403, assignee.name)
        self.assertFalse(Activity.objects.exists())
        # Editing, and so reassigning, is an admin's too, even for the follow-up's own staff.
        mine = Activity.objects.create(
            lead=self.a_lead, title='Call Asha', type='PHONE_CALL', assigned_to=self.staff_a, due_date=date(2026, 10, 4),
            created_by=self.admin,
        )
        for body in ({'title': 'Call Asha (edited)'}, {'assigned_to': self.staff_b.pk}, {'assigned_to': self.staff_a.pk}):
            self.assertEqual(self.patch(mine.pk, **body).status_code, status.HTTP_403_FORBIDDEN, body)
        stored = Activity.objects.get(pk=mine.pk)
        self.assertEqual((stored.title, stored.assigned_to), ('Call Asha', self.staff_a))
        self.as_user(self.admin)
        moved = self.patch(mine.pk, title='Call Asha (edited)', assigned_to=self.staff_b.pk)
        self.assertEqual((moved.status_code, moved.data['assigned_to']), (200, self.staff_b.pk))

    def test_pending_to_completed_persists_and_completed_is_final(self):
        self.as_user(self.admin)
        created = self.add(self.a_lead, assigned_to=self.staff_a.pk).data
        # Its staff completes it.
        self.as_user(self.staff_a)
        done = self.complete(created['id'])
        self.assertEqual((done.status_code, done.data['status']), (200, 'COMPLETED'))
        self.assertEqual(Activity.objects.get(pk=created['id']).status, ActivityStatus.COMPLETED)
        self.as_user(self.admin)
        reopened = self.patch(created['id'], status='PENDING')
        self.assertEqual(reopened.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reopened.data['status'][0], 'A completed follow-up stays completed.')
        self.assertEqual(self.patch(created['id'], status='DONE').status_code, status.HTTP_400_BAD_REQUEST)
        # A completed follow-up can still be edited (by an admin).
        edited = self.patch(created['id'], title='Called; quotation sent', description='Sent by email.')
        self.assertEqual((edited.status_code, edited.data['status']), (200, 'COMPLETED'))

    def test_follow_ups_added_before_headings_staff_and_due_dates_still_load_and_can_be_completed(self):
        legacy = Activity.objects.create(lead=self.a_lead, type='NOTE', description='Old note.', created_by=self.admin)
        # Assigned to no one, so only admins see it, even on Staff A's lead.
        self.as_user(self.staff_a)
        self.assertEqual(self.page(lead=self.a_lead.pk)['count'], 0)
        self.as_user(self.admin)
        row = self.page(lead=self.a_lead.pk)['results'][0]
        self.assertEqual((row['title'], row['assigned_to'], row['due_date'], row['status']), ('', None, None, 'PENDING'))
        self.assertEqual(self.patch(legacy.pk, status='COMPLETED').status_code, status.HTTP_200_OK)
        # Editing its details asks for the missing ones.
        self.assertEqual(self.patch(legacy.pk, title='').status_code, status.HTTP_400_BAD_REQUEST)
        filled = self.patch(legacy.pk, title='Old note', assigned_to=self.staff_a.pk, due_date='2026-09-20')
        self.assertEqual(filled.status_code, status.HTTP_200_OK, filled.data)
        # Now it is Staff A's, she sees it.
        self.as_user(self.staff_a)
        self.assertEqual(self.titles(lead=self.a_lead.pk), ['Old note'])


class FollowUpAccessTests(FollowUpTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        follow = {'type': 'PHONE_CALL', 'created_by': cls.admin}
        cls.on_a = Activity.objects.create(
            lead=cls.a_lead, title='Call Asha', assigned_to=cls.staff_a, due_date=date(2026, 10, 5),
            description='About the quote.', **follow,
        )
        cls.on_a_done = Activity.objects.create(
            lead=cls.a_lead, title='Send brochure', assigned_to=cls.staff_a, due_date=date(2026, 9, 28),
            status=ActivityStatus.COMPLETED, completed_at=timezone.now(), completed_by=cls.staff_a, **follow,
        )
        cls.on_b = Activity.objects.create(
            lead=cls.b_lead, title='Proposal discussion', assigned_to=cls.staff_b, due_date=date(2026, 10, 2), **follow,
        )
        # On Staff B's lead, but Staff A does it.
        cls.b_for_a = Activity.objects.create(
            lead=cls.b_lead, title='Site visit with Asha', type='SITE_VISIT', assigned_to=cls.staff_a,
            due_date=date(2026, 10, 7), created_by=cls.admin,
        )
        cls.on_pool = Activity.objects.create(
            lead=cls.pool, title='Roof survey', type='SITE_VISIT', assigned_to=cls.admin, due_date=date(2026, 10, 9),
            created_by=cls.admin,
        )

    def test_admin_sees_and_manages_every_follow_up(self):
        self.as_user(self.admin)
        self.assertEqual(self.page()['count'], 5)
        self.assertEqual(self.patch(self.on_b.pk, status='COMPLETED').status_code, status.HTTP_200_OK)
        row = next(r for r in self.page()['results'] if r['id'] == self.on_pool.pk)
        self.assertEqual((row['lead_name'], row['assigned_to_name'], row['can_edit'], row['can_delete'], row['can_open_lead']),
                         ('Pool Lead', 'Admin', True, True, True))

    def test_staff_see_and_complete_only_the_follow_ups_assigned_to_them_whoever_the_lead_belongs_to(self):
        self.as_user(self.staff_a)
        self.assertEqual(sorted(self.titles()), ['Call Asha', 'Send brochure', 'Site visit with Asha'])
        rows = {row['id']: row for row in self.page()['results']}
        # Staff A does it, so completes it; editing and deleting are an admin's, and the lead is Staff B's: no opening it.
        row = rows[self.b_for_a.pk]
        self.assertEqual(
            (row['can_edit'], row['can_delete'], row['can_open_lead'], row['can_update_status']), (False, False, False, True),
        )
        self.assertTrue(rows[self.on_a.pk]['can_open_lead'])  # her own lead, which the Leads module opens
        self.assertEqual(self.complete(self.b_for_a.pk).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.b_for_a.pk, description='Done with the team.').status_code, 403)
        self.assertEqual(self.client.delete(reverse('activity-detail', args=[self.b_for_a.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('lead-detail', args=[self.b_lead.pk])).status_code, 404)
        # Staff B's own follow-up and the admin's stay out of reach, whatever is asked for.
        for other in (self.on_b, self.on_pool):
            for refused in (self.client.get(reverse('activity-detail', args=[other.pk])), self.complete(other.pk)):
                self.assertEqual(
                    (refused.status_code, refused.data['detail']), (403, 'You can only update activities assigned to you.'),
                )
            self.assertEqual(self.patch(other.pk, status='COMPLETED').status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(self.client.delete(reverse('activity-detail', args=[other.pk])).status_code, 403)
        self.assertEqual(self.page(assigned_to=self.staff_b.pk)['count'], 0)
        self.assertEqual(self.titles(lead=self.b_lead.pk), ['Site visit with Asha'])
        self.assertEqual(self.page(search='Proposal')['count'], 0)
        self.assertEqual(self.page(search='Pool')['count'], 0)
        # Staff B's lead matches her search only by what she's shown of it (name, phone, ID), never by its email or
        # place; her own lead, which the Leads module lets her open, matches by those too.
        for shown in ('ravi', '98765 00002', f'#{self.b_lead.pk}'):
            self.assertEqual(self.titles(search=shown), ['Site visit with Asha'], shown)
        self.assertEqual(sorted(self.titles(search='ernakulam')), ['Call Asha', 'Send brochure'])
        # Staff B, whose lead it is, sees only her own follow-up on it, not the one Staff A does.
        self.as_user(self.staff_b)
        self.assertEqual(self.titles(), ['Proposal discussion'])
        self.assertEqual(Activity.objects.get(pk=self.on_b.pk).status, ActivityStatus.PENDING)
        self.assertEqual(Activity.objects.get(pk=self.on_pool.pk).status, ActivityStatus.PENDING)

    def test_follow_ups_are_added_by_admins_and_never_move_to_another_lead(self):
        self.as_user(self.staff_a)
        for lead in (self.a_lead, self.b_lead):
            self.assertEqual(self.add(lead, assigned_to=self.staff_a.pk).status_code, 403, lead.name)
        self.assertEqual(self.patch(self.on_a.pk, lead=self.b_lead.pk).status_code, status.HTTP_403_FORBIDDEN)
        self.as_user(self.admin)
        moved = self.patch(self.on_a.pk, lead=self.b_lead.pk)
        self.assertEqual(moved.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('lead', moved.data)
        self.assertEqual(Activity.objects.get(pk=self.on_a.pk).lead, self.a_lead)
        self.assertEqual(Activity.objects.count(), 5)

    def test_the_page_and_completing_need_the_activities_module_and_a_leads_own_list_the_leads_module(self):
        role = Role.objects.get(name=Role.STAFF)
        role.permissions.remove(Permission.objects.get(content_type__app_label='accounts', codename='access_activities'))
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(reverse('activity-list')).status_code, status.HTTP_403_FORBIDDEN)
        for blank in ('', ' '):
            self.assertEqual(self.client.get(reverse('activity-list'), {'lead': blank}).status_code, 403, repr(blank))
        self.assertEqual(self.client.get(f"{reverse('activity-list')}?lead={self.a_lead.pk}&lead=").status_code, 403)
        self.assertEqual(self.client.get(reverse('activity-detail', args=[self.on_a.pk])).status_code, 403)
        self.assertEqual(self.complete(self.on_a.pk).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Activity.objects.get(pk=self.on_a.pk).status, ActivityStatus.PENDING)
        # A lead's own list (on its page) needs the Leads module only, and holds only her own follow-ups; completing them
        # isn't offered.
        self.assertEqual(
            [(row['title'], row['can_update_status']) for row in self.page(lead=self.a_lead.pk)['results']],
            [('Send brochure', False), ('Call Asha', False)],
        )
        role.permissions.remove(Permission.objects.get(content_type__app_label='leads', codename='view_lead'))
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(reverse('activity-list'), {'lead': self.a_lead.pk}).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('activity-list')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filters_search_sort_and_pages(self):
        self.as_user(self.admin)
        self.assertEqual(self.titles(status='COMPLETED'), ['Send brochure'])
        self.assertEqual(sorted(self.titles(type='SITE_VISIT')), ['Roof survey', 'Site visit with Asha'])
        # The follow-up's own staff, not the lead's.
        self.assertEqual(sorted(self.titles(assigned_to=self.staff_a.pk)), ['Call Asha', 'Send brochure', 'Site visit with Asha'])
        self.assertEqual(
            self.titles(due_after='2026-10-03', due_before='2026-10-07', ordering='due_date'), ['Call Asha', 'Site visit with Asha'],
        )
        # Search: heading, notes, and the lead's name, phone (with or without the country code) and ID.
        self.assertEqual(self.titles(search='proposal'), ['Proposal discussion'])
        self.assertEqual(self.titles(search='about the quote'), ['Call Asha'])
        self.assertEqual(sorted(self.titles(search='ravi')), ['Proposal discussion', 'Site visit with Asha'])
        self.assertEqual(sorted(self.titles(search='+91 98765 00002')), ['Proposal discussion', 'Site visit with Asha'])
        self.assertEqual(self.titles(search=f'#{self.pool.pk}'), ['Roof survey'])
        self.assertEqual(
            self.titles(ordering='due_date'),
            ['Send brochure', 'Proposal discussion', 'Call Asha', 'Site visit with Asha', 'Roof survey'],
        )
        self.assertEqual(self.titles(ordering='status')[-1], 'Send brochure')
        self.assertEqual(self.titles(ordering='-status')[0], 'Send brochure')
        paged = self.page(ordering='due_date', page_size=2, page=2)
        self.assertEqual((paged['count'], [r['title'] for r in paged['results']]), (5, ['Call Asha', 'Site visit with Asha']))
        for bad in ({'status': 'DONE'}, {'type': 'CONFIRMED'}, {'ordering': 'lead'}, {'due_after': 'soon'}, {'assigned_to': 'x'}):
            self.assertEqual(self.client.get(reverse('activity-list'), bad).status_code, 400, bad)

    def test_the_page_costs_the_same_queries_however_many_follow_ups_it_lists(self):
        self.as_user(self.admin)

        def count_queries():
            with CaptureQueriesContext(connection) as queries:
                self.page()
            return len(queries)

        few = count_queries()
        for i in range(12):
            Activity.objects.create(
                lead=self.b_lead, title=f'Note {i}', type='NOTE', assigned_to=self.staff_b, due_date=date(2026, 10, 1),
                created_by=self.staff_b,
            )
        self.assertEqual(count_queries(), few)

    def test_deleting_a_lead_removes_its_follow_ups(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[self.a_lead.pk])).status_code, 204)
        self.assertFalse(Activity.objects.filter(lead_id=self.a_lead.pk).exists())


class InitialFollowUpTests(FollowUpTestCase):
    """Add Lead can add the lead's first follow-up: both are saved together, or neither."""

    def lead_body(self, **fields):
        return {'name': 'Rahul Kumar', 'phone': '9876500009', 'district': 'Thrissur', 'plan': self.plan.pk, **fields}

    def follow_up(self, **fields):
        return {'title': 'Call for site requirement', 'type': 'PHONE_CALL', 'assigned_to': self.staff_a.pk,
                'due_date': '2026-10-03', 'description': 'Customer requested a callback.', **fields}

    def test_a_new_lead_with_an_initial_follow_up_creates_both_and_the_follow_up_is_pending(self):
        self.as_user(self.admin)
        created = self.client.post(
            reverse('lead-list'), self.lead_body(assigned_to=self.staff_b.pk, initial_follow_up=self.follow_up()),
            format='json',
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertNotIn('initial_follow_up', created.data)
        lead = Lead.objects.get(pk=created.data['id'])
        follow_up = lead.activities.get()
        self.assertEqual(
            (follow_up.title, follow_up.type, follow_up.assigned_to, str(follow_up.due_date), follow_up.status,
             follow_up.created_by),
            ('Call for site requirement', 'PHONE_CALL', self.staff_a, '2026-10-03', 'PENDING', self.admin),
        )
        # The follow-up's staff never changes the lead's.
        self.assertEqual(lead.assigned_to, self.staff_b)

    def test_a_new_lead_without_a_follow_up_creates_no_follow_up(self):
        self.as_user(self.admin)
        created = self.client.post(reverse('lead-list'), self.lead_body(), format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertFalse(Activity.objects.exists())

    def test_an_invalid_initial_follow_up_saves_nothing(self):
        self.as_user(self.admin)
        for bad in ({'title': ''}, {'due_date': None}, {'assigned_to': 999999}, {'type': 'CONFIRMED'}):
            response = self.client.post(
                reverse('lead-list'), self.lead_body(initial_follow_up=self.follow_up(**bad)), format='json',
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, bad)
            self.assertIn('initial_follow_up', response.data)
        self.assertFalse(Lead.objects.filter(name='Rahul Kumar').exists())
        self.assertFalse(Activity.objects.exists())

    def test_staff_add_a_lead_with_a_follow_up_for_themselves_only(self):
        self.as_user(self.staff_a)
        refused = self.client.post(
            reverse('lead-list'), self.lead_body(initial_follow_up=self.follow_up(assigned_to=self.staff_b.pk)),
            format='json',
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Lead.objects.filter(name='Rahul Kumar').exists())
        created = self.client.post(reverse('lead-list'), self.lead_body(initial_follow_up=self.follow_up()), format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        follow_up = Activity.objects.get(lead_id=created.data['id'])
        self.assertEqual((follow_up.assigned_to, follow_up.created_by), (self.staff_a, self.staff_a))
        self.assertEqual(self.titles(lead=created.data['id']), ['Call for site requirement'])

    def test_an_existing_lead_takes_follow_ups_from_its_page_not_from_an_edit(self):
        self.as_user(self.admin)
        response = self.client.patch(
            reverse('lead-detail', args=[self.a_lead.pk]), {'initial_follow_up': self.follow_up()}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Activity.objects.exists())


class LeadAndWorkActivitiesTests(FollowUpTestCase):
    """Lead follow-ups and Work activities share the one activity log, each kind with its own rules and audience."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        won = Lead.objects.create(
            name='Won Lead', phone='9876500009', district='Ernakulam', plan=cls.plan, amount=cls.plan.amount,
            created_by=cls.admin, status=LeadStatus.WON,
        )
        won.convert(cls.admin)
        cls.work = won.work
        cls.on_work = Activity.objects.create(
            work=cls.work, type='SITE_VISIT', description='Roof survey.', assigned_to=cls.staff_a, created_by=cls.admin,
        )
        cls.on_lead = Activity.objects.create(
            lead=cls.a_lead, title='Call Asha', type='PHONE_CALL', assigned_to=cls.staff_a, due_date=date(2026, 10, 5),
            created_by=cls.admin,
        )

    def test_completing_a_follow_up_records_when_and_by_whom_and_it_stays_completed(self):
        self.as_user(self.staff_a)
        done = self.complete(self.on_lead.pk)
        self.assertEqual((done.status_code, done.data['completed_by_name']), (200, 'Staff A'))
        self.assertIsNotNone(done.data['completed_at'])
        stored = Activity.objects.get(pk=self.on_lead.pk)
        self.assertEqual((stored.status, stored.completed_by), (ActivityStatus.COMPLETED, self.staff_a))
        self.assertEqual(self.complete(self.on_lead.pk).status_code, status.HTTP_400_BAD_REQUEST)
        self.as_user(self.admin)
        self.assertEqual(self.patch(self.on_lead.pk, status='PENDING').status_code, status.HTTP_400_BAD_REQUEST)

    def test_each_list_holds_only_its_own_kind(self):
        self.as_user(self.admin)
        self.assertEqual(self.titles(), ['Call Asha'])
        self.assertEqual(self.page(lead=self.a_lead.pk)['count'], 1)
        on_work = self.page(work=self.work.pk)['results']
        self.assertEqual([row['description'] for row in on_work], ['Roof survey.'])
        row = on_work[0]
        self.assertEqual(
            (row['lead'], row['lead_name'], row['work_summary']['customer_name'], row['can_edit'], row['can_delete'],
             row['can_open_lead']),
            (None, None, 'Won Lead', True, False, False),
        )

    def test_staff_see_and_complete_their_own_work_activities_without_the_work_module(self):
        self.as_user(self.staff_a)
        # The Lead Activities page holds lead follow-ups only; hers on a Work are on the Work Activities page.
        self.assertEqual(self.titles(), ['Call Asha'])
        works = self.client.get(reverse('activity-works'))
        self.assertEqual([row['description'] for row in works.data['results']], ['Roof survey.'])
        self.assertEqual(self.client.get(reverse('activity-detail', args=[self.on_work.pk])).status_code, 200)
        self.assertEqual(self.complete(self.on_work.pk).status_code, status.HTTP_200_OK)
        self.assertEqual(Activity.objects.get(pk=self.on_work.pk).status, ActivityStatus.COMPLETED)
        # The Work itself, and its own list of activities (on its page), are for the Work module.
        self.assertEqual(self.client.get(reverse('activity-list'), {'work': self.work.pk}).status_code, 403)
        self.assertEqual(self.client.get(reverse('work-detail', args=[self.work.pk])).status_code, 403)

    def test_a_work_activity_follows_the_work_rules_not_the_follow_up_rules(self):
        self.as_user(self.admin)
        # No heading, staff or due date needed; notes are.
        added = self.client.post(
            reverse('activity-list'), {'work': self.work.pk, 'type': 'NOTE', 'description': 'Panels delivered.'}, format='json',
        )
        self.assertEqual((added.status_code, added.data['status'], added.data['title']), (201, 'PENDING', ''))
        no_notes = self.client.post(reverse('activity-list'), {'work': self.work.pk, 'type': 'NOTE'}, format='json')
        self.assertIn('description', no_notes.data)
        both = self.client.post(
            reverse('activity-list'), {'work': self.work.pk, 'lead': self.a_lead.pk, 'type': 'NOTE', 'description': 'x'},
            format='json',
        )
        self.assertEqual(both.status_code, status.HTTP_400_BAD_REQUEST)


class ActivityOrderAndStatusRuleTests(FollowUpTestCase):
    """On the Activities pages staff see only their own activities, in the order asked for; only the assignee or an
    admin completes an activity, and only an admin edits, reassigns or reopens one."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        grant(Role.objects.get(name=Role.STAFF), 'accounts.access_work')
        follow = {'type': 'PHONE_CALL', 'created_by': cls.admin}
        # Added in this order, so "newest first" alone would list them bottom-up.
        cls.unassigned_on_a = Activity.objects.create(lead=cls.a_lead, title='Legacy note', due_date=date(2026, 10, 1), **follow)
        cls.a_by_b = Activity.objects.create(lead=cls.a_lead, title='Survey by B', assigned_to=cls.staff_b, due_date=date(2026, 10, 2), **follow)
        cls.on_a = Activity.objects.create(lead=cls.a_lead, title='Call Asha', assigned_to=cls.staff_a, due_date=date(2026, 10, 9), **follow)
        cls.on_b = Activity.objects.create(lead=cls.b_lead, title='Proposal', assigned_to=cls.staff_b, due_date=date(2026, 10, 4), **follow)
        cls.b_for_a = Activity.objects.create(lead=cls.b_lead, title='Visit with Asha', assigned_to=cls.staff_a, due_date=date(2026, 10, 3), **follow)
        won = Lead.objects.create(
            name='Won Lead', phone='9876500009', district='Ernakulam', plan=cls.plan, amount=cls.plan.amount,
            created_by=cls.admin, status=LeadStatus.WON,
        )
        won.convert(cls.admin)
        cls.work = won.work
        cls.work_unassigned = Activity.objects.create(work=cls.work, type='NOTE', description='Unassigned.', created_by=cls.admin)
        cls.work_for_b = Activity.objects.create(work=cls.work, type='NOTE', description='For B.', assigned_to=cls.staff_b, created_by=cls.admin)
        cls.work_for_a = Activity.objects.create(work=cls.work, type='NOTE', description='For A.', assigned_to=cls.staff_a, created_by=cls.admin)

    def test_staff_see_only_their_own_follow_ups_in_the_order_asked_for(self):
        self.as_user(self.staff_a)
        # Hers alone (newest first): never another person's or an unassigned one, even on her own lead.
        self.assertEqual(self.titles(), ['Visit with Asha', 'Call Asha'])
        self.assertEqual(self.titles(ordering='due_date'), ['Visit with Asha', 'Call Asha'])
        self.assertEqual(self.titles(ordering='-due_date'), ['Call Asha', 'Visit with Asha'])
        # One lead's follow-ups too.
        self.assertEqual(self.titles(lead=self.a_lead.pk, ordering='-due_date'), ['Call Asha'])
        self.as_user(self.staff_b)
        self.assertEqual(self.titles(), ['Proposal', 'Survey by B'])
        # An admin's page is everyone's work in the chosen order.
        self.as_user(self.admin)
        self.assertEqual(self.titles(), ['Visit with Asha', 'Proposal', 'Call Asha', 'Survey by B', 'Legacy note'])

    def test_staff_see_only_their_own_work_activities_on_the_work_activities_page(self):
        def descriptions():
            response = self.client.get(reverse('activity-works'))
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            return [row['description'] for row in response.data['results']]

        self.as_user(self.staff_a)
        self.assertEqual(descriptions(), ['For A.'])
        self.as_user(self.staff_b)
        self.assertEqual(descriptions(), ['For B.'])
        self.as_user(self.admin)
        self.assertEqual(descriptions(), ['For A.', 'For B.', 'Unassigned.'])  # the Work's own order: newest first

    def test_only_the_assignee_or_an_admin_completes_a_follow_up_and_only_an_admin_edits_one(self):
        # Staff B's follow-up on Staff A's lead isn't on Staff A's list, and opening or completing it is refused.
        self.as_user(self.staff_a)
        self.assertNotIn(self.a_by_b.pk, [row['id'] for row in self.page()['results']])
        for refused in (self.client.get(reverse('activity-detail', args=[self.a_by_b.pk])), self.complete(self.a_by_b.pk)):
            self.assertEqual((refused.status_code, refused.data['detail']), (403, 'You can only update activities assigned to you.'))
        # So is completing it by editing it, taking it over (to complete it next), or both in one request; nor can an
        # unassigned one be taken, or her own be edited. Nothing of it is saved.
        for activity, body in [
            (self.a_by_b, {'status': 'COMPLETED'}), (self.a_by_b, {'assigned_to': self.staff_a.pk}),
            (self.a_by_b, {'assigned_to': self.staff_a.pk, 'status': 'COMPLETED'}), (self.a_by_b, {'title': 'Survey (roof)'}),
            (self.unassigned_on_a, {'assigned_to': self.staff_a.pk}), (self.on_a, {'title': 'Call Asha (edited)'}),
        ]:
            self.assertEqual(self.patch(activity.pk, **body).status_code, status.HTTP_403_FORBIDDEN, (activity.title, body))
        a_by_b = Activity.objects.get(pk=self.a_by_b.pk)
        self.assertEqual((a_by_b.title, a_by_b.assigned_to, a_by_b.status), ('Survey by B', self.staff_b, ActivityStatus.PENDING))
        self.assertIsNone(Activity.objects.get(pk=self.unassigned_on_a.pk).assigned_to)
        self.assertEqual(Activity.objects.get(pk=self.on_a.pk).title, 'Call Asha')
        # An admin reassigns anything.
        self.as_user(self.admin)
        self.assertEqual(self.patch(self.a_by_b.pk, assigned_to=self.staff_a.pk).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.a_by_b.pk, assigned_to=self.staff_b.pk).status_code, status.HTTP_200_OK)
        # Her own she completes, with the complete action.
        self.as_user(self.staff_a)
        mine = next(r for r in self.page()['results'] if r['id'] == self.on_a.pk)
        self.assertEqual((mine['can_edit'], mine['can_update_status']), (False, True))
        self.assertEqual(self.complete(self.on_a.pk).status_code, status.HTTP_200_OK)
        # The assignee completes it; so does an admin, even one assigned to no one.
        self.as_user(self.staff_b)
        self.assertEqual(self.complete(self.a_by_b.pk).status_code, status.HTTP_200_OK)
        self.as_user(self.admin)
        self.assertEqual(self.patch(self.on_b.pk, status='COMPLETED').status_code, status.HTTP_200_OK)
        self.assertEqual(self.complete(self.unassigned_on_a.pk).status_code, status.HTTP_200_OK)
        self.assertEqual(Notification.objects.filter(kind='ACTIVITY_STATUS', recipient=self.staff_b).count(), 1)

    def test_the_same_rules_hold_for_work_activities(self):
        self.as_user(self.staff_a)
        # A Work's own list (with the Work module) holds only hers too.
        rows = {row['id']: row for row in self.client.get(reverse('activity-list'), {'work': self.work.pk}).data['results']}
        self.assertEqual(list(rows), [self.work_for_a.pk])
        self.assertEqual((rows[self.work_for_a.pk]['can_edit'], rows[self.work_for_a.pk]['can_update_status']), (False, True))
        for other in (self.work_for_b, self.work_unassigned):
            refused = self.complete(other.pk)
            self.assertEqual((refused.status_code, refused.data['detail']), (403, 'You can only update activities assigned to you.'))
            # Taking it, editing it or completing it by editing it: an admin's.
            for body in ({'assigned_to': self.staff_a.pk}, {'description': 'Edited.'}, {'status': 'COMPLETED'}):
                self.assertEqual(self.patch(other.pk, **body).status_code, status.HTTP_403_FORBIDDEN, body)
        # Releasing her own is an admin's too.
        self.assertEqual(self.patch(self.work_for_a.pk, assigned_to=None).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.complete(self.work_for_a.pk).status_code, status.HTTP_200_OK)
        # Reopening is an admin's, even for its assignee.
        self.assertEqual(self.patch(self.work_for_a.pk, status='PENDING').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            list(Activity.objects.filter(work=self.work).order_by('pk').values_list('description', 'assigned_to', 'status')),
            [('Unassigned.', None, 'PENDING'), ('For B.', self.staff_b.pk, 'PENDING'), ('For A.', self.staff_a.pk, 'COMPLETED')],
        )
        self.as_user(self.staff_b)
        self.assertEqual(self.complete(self.work_for_b.pk).status_code, status.HTTP_200_OK)
        self.as_user(self.admin)
        # The edit dialog sends the status along unchanged: that is no status change.
        self.assertEqual(self.patch(self.work_for_b.pk, status='COMPLETED', description='For B, edited.').status_code, 200)
        self.assertEqual(self.patch(self.work_for_b.pk, status='PENDING').status_code, status.HTTP_200_OK)  # reopened
        self.assertEqual(self.patch(self.work_unassigned.pk, assigned_to=self.staff_a.pk).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.work_unassigned.pk, assigned_to=None).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.work_for_b.pk, status='COMPLETED').status_code, status.HTTP_200_OK)

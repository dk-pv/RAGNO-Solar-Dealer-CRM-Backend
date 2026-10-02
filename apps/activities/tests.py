from datetime import date

from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, SolarPlan

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
        # As an admin gives them to the STAFF role in Settings -> Roles & Access.
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

    def test_staff_assign_follow_ups_only_to_themselves(self):
        self.as_user(self.staff_a)
        refused = self.add(self.a_lead, assigned_to=self.staff_b.pk)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refused.data['assigned_to'][0], 'Only an admin can assign a follow-up to someone else.')
        created = self.add(self.a_lead, assigned_to=self.staff_a.pk)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(self.patch(created.data['id'], assigned_to=self.staff_b.pk).status_code, 400)
        # A follow-up an admin assigned to someone else keeps that person when staff edit it.
        by_admin = Activity.objects.create(
            lead=self.a_lead, title='Admin task', type='NOTE', assigned_to=self.admin, due_date=date(2026, 10, 4),
            created_by=self.admin,
        )
        kept = self.patch(by_admin.pk, title='Admin task (edited)', assigned_to=self.admin.pk)
        self.assertEqual((kept.status_code, kept.data['assigned_to']), (200, self.admin.pk))

    def test_pending_to_completed_persists_and_completed_is_final(self):
        self.as_user(self.staff_a)
        created = self.add(self.a_lead, assigned_to=self.staff_a.pk).data
        done = self.patch(created['id'], status='COMPLETED')
        self.assertEqual((done.status_code, done.data['status']), (200, 'COMPLETED'))
        self.assertEqual(Activity.objects.get(pk=created['id']).status, ActivityStatus.COMPLETED)
        reopened = self.patch(created['id'], status='PENDING')
        self.assertEqual(reopened.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reopened.data['status'][0], 'A completed follow-up stays completed.')
        self.assertEqual(self.patch(created['id'], status='DONE').status_code, status.HTTP_400_BAD_REQUEST)
        # A completed follow-up can still be edited.
        edited = self.patch(created['id'], title='Called; quotation sent', description='Sent by email.')
        self.assertEqual((edited.status_code, edited.data['status']), (200, 'COMPLETED'))

    def test_follow_ups_added_before_headings_staff_and_due_dates_still_load_and_can_be_completed(self):
        legacy = Activity.objects.create(lead=self.a_lead, type='NOTE', description='Old note.', created_by=self.admin)
        self.as_user(self.staff_a)
        row = self.page(lead=self.a_lead.pk)['results'][0]
        self.assertEqual((row['title'], row['assigned_to'], row['due_date'], row['status']), ('', None, None, 'PENDING'))
        self.assertEqual(self.patch(legacy.pk, status='COMPLETED').status_code, status.HTTP_200_OK)
        # Editing its details asks for the missing ones.
        self.assertEqual(self.patch(legacy.pk, title='').status_code, status.HTTP_400_BAD_REQUEST)
        filled = self.patch(legacy.pk, title='Old note', assigned_to=self.staff_a.pk, due_date='2026-09-20')
        self.assertEqual(filled.status_code, status.HTTP_200_OK, filled.data)


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
            status=ActivityStatus.COMPLETED, **follow,
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

    def test_staff_see_follow_ups_on_their_leads_and_those_assigned_to_them_only(self):
        self.as_user(self.staff_a)
        self.assertEqual(sorted(self.titles()), ['Call Asha', 'Send brochure', 'Site visit with Asha'])
        row = next(r for r in self.page()['results'] if r['id'] == self.b_for_a.pk)
        # Staff A does it, so can complete and edit it, but the lead is Staff B's: no opening or deleting it.
        self.assertEqual((row['can_edit'], row['can_delete'], row['can_open_lead']), (True, False, False))
        self.assertEqual(self.patch(self.b_for_a.pk, status='COMPLETED').status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.b_for_a.pk, description='Done with the team.').status_code, 200)
        self.assertEqual(self.client.delete(reverse('activity-detail', args=[self.b_for_a.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('lead-detail', args=[self.b_lead.pk])).status_code, 404)
        # Staff B's own follow-up and the unassigned lead's stay out of reach, whatever is asked for.
        for other in (self.on_b, self.on_pool):
            self.assertEqual(self.patch(other.pk, status='COMPLETED').status_code, status.HTTP_404_NOT_FOUND)
            self.assertEqual(self.client.delete(reverse('activity-detail', args=[other.pk])).status_code, 404)
        self.assertEqual(self.page(assigned_to=self.staff_b.pk)['count'], 0)
        self.assertEqual(self.titles(lead=self.b_lead.pk), ['Site visit with Asha'])
        self.assertEqual(self.page(search='Proposal')['count'], 0)
        self.assertEqual(self.page(search='Pool')['count'], 0)
        # Staff B's lead matches her search only by what she's shown of it (name, phone, ID), never by its email or
        # place; her own lead matches by those too.
        for shown in ('ravi', '98765 00002', f'#{self.b_lead.pk}'):
            self.assertEqual(self.titles(search=shown), ['Site visit with Asha'], shown)
        self.assertEqual(sorted(self.titles(search='ernakulam')), ['Call Asha', 'Send brochure'])
        # Staff B, whose lead it is, sees both follow-ups on it, including the one Staff A does.
        self.as_user(self.staff_b)
        self.assertEqual(sorted(self.titles()), ['Proposal discussion', 'Site visit with Asha'])
        self.assertEqual(Activity.objects.get(pk=self.on_b.pk).status, ActivityStatus.PENDING)

    def test_follow_ups_are_added_only_to_the_users_own_leads(self):
        self.as_user(self.staff_a)
        refused = self.add(self.b_lead, assigned_to=self.staff_a.pk)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('lead', refused.data)
        moved = self.patch(self.on_a.pk, lead=self.b_lead.pk)
        self.assertEqual(moved.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reading_needs_view_writing_needs_change_and_the_page_needs_the_activities_module(self):
        role = Role.objects.get(name=Role.STAFF)
        role.permissions.remove(Permission.objects.get(content_type__app_label='accounts', codename='access_activities'))
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(reverse('activity-list')).status_code, status.HTTP_403_FORBIDDEN)
        for blank in ('', ' '):
            self.assertEqual(self.client.get(reverse('activity-list'), {'lead': blank}).status_code, 403, repr(blank))
        self.assertEqual(self.client.get(f"{reverse('activity-list')}?lead={self.a_lead.pk}&lead=").status_code, 403)
        self.assertEqual(self.page(lead=self.a_lead.pk)['count'], 2)
        role.permissions.remove(Permission.objects.get(content_type__app_label='leads', codename='change_lead'))
        self.as_user(self.staff_a)
        self.assertEqual(self.patch(self.on_a.pk, status='COMPLETED').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.add(self.a_lead, assigned_to=self.staff_a.pk).status_code, status.HTTP_403_FORBIDDEN)
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

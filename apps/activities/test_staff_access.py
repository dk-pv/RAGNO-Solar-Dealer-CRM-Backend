from datetime import date

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, User
from apps.accounts.permissions import MODULES
from apps.leads.models import Lead, LeadStatus, SolarPlan
from apps.notifications.models import Notification, NotificationKind
from apps.works.models import Work, WorkStage

from .models import Activity, ActivityStatus

PASSWORD = 'Solar-Panel-2026'
NOT_YOURS = 'You can only update activities assigned to you.'
NOTE = 'Customer confirmed site visit for tomorrow.'


def pks(*activities):
    return sorted(activity.pk for activity in activities)


class StaffAccessTestCase(APITestCase):
    """Two admins; Staff A and Staff B, whose role holds exactly the Activities module (all Roles & Access can give
    staff). Each kind of activity, a lead's follow-up and a Work's activity, has one assigned to Staff A, to Staff B, to
    the admin and to no one; Staff A has a second of each besides (a completed follow-up, an activity on another Work)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.admin_2 = User.objects.create_user(
            email='admin2@example.com', password=PASSWORD, name='Second Admin', role=Role.ADMIN,
        )
        cls.staff_a = User.objects.create_user(email='a@example.com', password=PASSWORD, name='Staff A')
        cls.staff_b = User.objects.create_user(email='b@example.com', password=PASSWORD, name='Staff B')
        Role.objects.get(pk=Role.STAFF).permissions.set([Permission.objects.get(codename='access_activities')])
        cls.plan = SolarPlan.objects.get(capacity=5)
        lead = {'district': 'Ernakulam', 'plan': cls.plan, 'amount': cls.plan.amount, 'created_by': cls.admin}
        # Staff A's lead has the details the leads list searches by; her own searches never reach them.
        cls.a_lead = Lead.objects.create(
            name='Asha Menon', phone='9876500001', email='asha.menon@example.com', area='Kakkanad', pin_code='682030',
            assigned_to=cls.staff_a, **lead,
        )
        cls.b_lead = Lead.objects.create(name='Ravi Kumar', phone='9876500002', assigned_to=cls.staff_b, **lead)
        cls.pool = Lead.objects.create(name='Pool Lead', phone='9876500003', **lead)
        # Hers and Won but not converted yet: what a convert request would turn into a Work.
        cls.a_won = Lead.objects.create(
            name='Won Lead', phone='9876500004', status=LeadStatus.WON, assigned_to=cls.staff_a, **lead,
        )
        # A Work only comes from converting a Won lead.
        works = []
        for name, phone in (('Neha Thomas', '9876500005'), ('Arun Kumar', '9876500006')):
            won = Lead.objects.create(name=name, phone=phone, status=LeadStatus.WON, **lead)
            won.convert(cls.admin)
            works.append(won.work)
        cls.work, cls.other_work = works

        follow = {'type': 'PHONE_CALL', 'created_by': cls.admin}
        cls.for_a = Activity.objects.create(
            lead=cls.a_lead, title='Call Asha', assigned_to=cls.staff_a, due_date=date(2026, 10, 5),
            description='About the quotation.', **follow,
        )
        cls.done_for_a = Activity.objects.create(
            lead=cls.a_lead, title='Send brochure', assigned_to=cls.staff_a, due_date=date(2026, 9, 28),
            status=ActivityStatus.COMPLETED, completed_at=timezone.now(), completed_by=cls.staff_a, **follow,
        )
        # On Staff B's lead, but Staff A does it; and the other way round.
        cls.on_b_lead_for_a = Activity.objects.create(
            lead=cls.b_lead, title='Site visit with Ravi', type='SITE_VISIT', assigned_to=cls.staff_a,
            due_date=date(2026, 10, 7), created_by=cls.admin,
        )
        cls.on_a_lead_for_b = Activity.objects.create(
            lead=cls.a_lead, title='Survey by B', type='SITE_VISIT', assigned_to=cls.staff_b, due_date=date(2026, 10, 2),
            created_by=cls.admin,
        )
        cls.for_b = Activity.objects.create(
            lead=cls.b_lead, title='Proposal discussion', assigned_to=cls.staff_b, due_date=date(2026, 10, 4), **follow,
        )
        cls.for_admin = Activity.objects.create(
            lead=cls.pool, title='Roof survey', type='SITE_VISIT', assigned_to=cls.admin, due_date=date(2026, 10, 9),
            created_by=cls.admin,
        )
        # Assigned to no one, as follow-ups added before staff were required (the API requires them now).
        cls.unassigned = Activity.objects.create(lead=cls.a_lead, title='Legacy note', type='NOTE', created_by=cls.admin)
        note = {'type': 'NOTE', 'created_by': cls.admin}
        cls.work_for_a = Activity.objects.create(
            work=cls.work, description='Collect KSEB documents.', assigned_to=cls.staff_a, due_date=date(2026, 10, 6),
            **note,
        )
        cls.other_work_for_a = Activity.objects.create(
            work=cls.other_work, type='SITE_VISIT', description='Structure visit.', assigned_to=cls.staff_a,
            due_date=date(2026, 10, 8), created_by=cls.admin,
        )
        cls.work_for_b = Activity.objects.create(
            work=cls.work, description='Panel delivery check.', assigned_to=cls.staff_b, due_date=date(2026, 10, 3),
            **note,
        )
        cls.work_for_admin = Activity.objects.create(
            work=cls.work, description='Subsidy papers.', assigned_to=cls.admin, **note,
        )
        cls.work_unassigned = Activity.objects.create(work=cls.work, description='Meter photo.', **note)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def page(self, name, **params):
        """A page of the Lead Activities ('activity-list') or the Work Activities ('activity-works') list."""
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def titles(self, **params):
        return [row['title'] for row in self.page('activity-list', **params)['results']]

    def descriptions(self, **params):
        return [row['description'] for row in self.page('activity-works', **params)['results']]

    def complete(self, activity, **body):
        return self.client.post(reverse('activity-complete', args=[activity.pk]), body, format='json')

    def patch(self, activity, **body):
        return self.client.patch(reverse('activity-detail', args=[activity.pk]), body, format='json')

    def stored(self):
        """Every activity as stored, to show a refused request changed nothing."""
        return list(Activity.objects.order_by('pk').values_list(
            'lead', 'work', 'title', 'type', 'assigned_to', 'due_date', 'description', 'status', 'completed_at',
            'completed_by', 'completion_note', 'updated_at',
        ))


class SignInTests(StaffAccessTestCase):
    def setUp(self):
        # Sign-in attempts are rate-limited per address and counted in the cache, which outlives a test.
        cache.clear()

    def sign_in(self, email):
        response = self.client.post(reverse('auth-login'), {'email': email, 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {response.data["access"]}')

    def test_signed_out_requests_are_refused(self):
        before = self.stored()
        detail = reverse('activity-detail', args=[self.for_a.pk])
        for url in (
            reverse('activity-list'), reverse('activity-works'), detail, reverse('plan-list'), reverse('lead-list'),
            reverse('work-list'), reverse('dashboard-summary'), reverse('report-activities'), reverse('user-list'),
            reverse('role-list'), reverse('auth-me'), reverse('notification-list'),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)
        add = {'lead': self.a_lead.pk, 'title': 'Call', 'type': 'NOTE', 'assigned_to': self.staff_a.pk, 'due_date': '2026-10-10'}
        for response in (
            self.complete(self.for_a, completion_note=NOTE), self.patch(self.for_a, status='COMPLETED'),
            self.client.post(reverse('activity-list'), add, format='json'), self.client.delete(detail),
        ):
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.stored(), before)

    def test_staff_sign_in_and_hold_the_activities_module_alone(self):
        self.sign_in('A@Example.com')

        me = self.client.get(reverse('auth-me'))

        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(
            (me.data['id'], me.data['name'], me.data['email'], me.data['role'], me.data['modules']),
            (self.staff_a.pk, 'Staff A', 'a@example.com', Role.STAFF, ['activities']),
        )
        self.assertEqual(self.page('activity-list')['count'], 3)
        self.assertEqual(self.page('activity-works')['count'], 2)
        self.assertEqual(self.client.get(reverse('lead-list')).status_code, status.HTTP_403_FORBIDDEN)

    def test_admins_sign_in_with_every_module(self):
        self.sign_in('admin@example.com')

        me = self.client.get(reverse('auth-me')).data

        self.assertEqual((me['role'], me['modules']), (Role.ADMIN, list(MODULES)))
        self.assertEqual(self.client.get(reverse('lead-list')).status_code, status.HTTP_200_OK)
        self.assertEqual(self.page('activity-list')['count'], 7)

    def test_a_deactivated_staff_members_token_stops_working(self):
        self.sign_in('a@example.com')
        User.objects.filter(pk=self.staff_a.pk).update(is_active=False)

        self.assertEqual(self.client.get(reverse('activity-list')).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.complete(self.for_a).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(Activity.objects.get(pk=self.for_a.pk).status, ActivityStatus.PENDING)


class StaffModuleAccessTests(StaffAccessTestCase):
    """Staff with only the Activities module reach the two Activities pages, their own account and their notifications.
    Every other module's API refuses them, even for their own lead or a Work they have an activity on."""

    def records(self):
        """Everything the refused requests asked to change."""
        return (
            list(Lead.objects.order_by('pk').values_list('name', 'status', 'district', 'assigned_to', 'is_pinned')),
            list(Work.objects.order_by('pk').values_list('lead', 'stage', 'assigned_to')),
            list(User.objects.order_by('pk').values_list('email', 'name', 'role', 'is_active')),
            sorted(Role.objects.get(pk=Role.STAFF).permissions.values_list('codename', flat=True)),
            list(Department.objects.values_list('name', flat=True)),
            self.stored(),
            Notification.objects.count(),
        )

    def test_every_other_modules_api_refuses_staff(self):
        lead, won, work, me = self.a_lead.pk, self.a_won.pk, self.work.pk, self.staff_a.pk
        new_lead = {'name': 'New Lead', 'phone': '9876500009', 'district': 'Thrissur', 'plan': self.plan.pk}
        new_user = {'name': 'Ravi', 'email': 'ravi@example.com', 'password': PASSWORD, 'confirm_password': PASSWORD,
                    'role': Role.ADMIN}
        # The paths as the browser calls them, so a renamed route can't quietly drop out of the table.
        requests = [
            # Leads, even her own: listing, opening, changing, converting and exporting them, and their plans.
            ('get', '/api/leads/', None),
            ('get', f'/api/leads/{lead}/', None),
            ('get', '/api/leads/summary/', None),
            ('get', '/api/leads/assignees/', None),
            ('get', '/api/leads/export/', None),
            ('post', '/api/leads/', new_lead),
            ('post', '/api/leads/', {**new_lead, 'initial_follow_up': {
                'title': 'Call', 'type': 'PHONE_CALL', 'assigned_to': me, 'due_date': '2026-10-10'}}),
            ('patch', f'/api/leads/{lead}/', {'district': 'Kollam', 'is_pinned': True}),
            ('delete', f'/api/leads/{lead}/', None),
            ('post', f'/api/leads/{lead}/status/', {'status': LeadStatus.HOT}),
            ('post', f'/api/leads/{won}/convert/', None),
            ('post', '/api/leads/bulk-status/', {'ids': [lead], 'status': LeadStatus.HOT}),
            ('post', '/api/leads/bulk-convert/', {'ids': [won]}),
            ('post', '/api/leads/bulk-delete/', {'ids': [lead]}),
            ('get', '/api/plans/', None),
            # Works, even one with her activity on it, and its documents.
            ('get', '/api/works/', None),
            ('get', f'/api/works/{work}/', None),
            ('get', '/api/works/summary/', None),
            ('get', '/api/works/assignees/', None),
            ('patch', f'/api/works/{work}/', {'stage': WorkStage.COMPLETED, 'assigned_to': me}),
            ('post', '/api/works/bulk-stage/', {'ids': [work], 'stage': WorkStage.COMPLETED}),
            ('post', '/api/works/bulk-delete/', {'ids': [work]}),
            ('get', f'/api/works/{work}/documents/', None),
            ('get', f'/api/works/{work}/documents/bank_passbook/file/', None),
            ('delete', f'/api/works/{work}/documents/bank_passbook/', None),
            # Dashboard
            ('get', '/api/dashboard/summary/', None),
            ('get', '/api/dashboard/follow-ups/?bucket=today', None),
            ('get', '/api/dashboard/recent/', None),
            ('get', '/api/dashboard/timeline/', None),
            # Reports, the activities report and the history included, and their exports.
            *(('get', f'/api/reports/{report}/', None) for report in (
                'leads', 'leads/summary', 'works', 'works/summary', 'activities', 'activities/summary', 'staff', 'history',
            )),
            ('get', '/api/reports/activities/?export=csv', None),
            ('get', '/api/reports/history/?export=csv', None),
            # Settings: users (her own account included), roles and departments.
            ('get', '/api/users/', None),
            ('get', '/api/users/modules/', None),
            ('get', f'/api/users/{me}/', None),
            ('post', '/api/users/', new_user),
            ('patch', f'/api/users/{me}/', {'role': Role.ADMIN}),
            ('get', '/api/roles/', None),
            ('get', '/api/roles/STAFF/', None),
            ('patch', '/api/roles/STAFF/', {'modules': ['activities', 'leads']}),
            ('get', '/api/departments/', None),
            ('post', '/api/departments/', {'name': 'Sales'}),
            # CRM reset
            ('get', '/api/maintenance/reset-crm-data/', None),
            ('post', '/api/maintenance/reset-crm-data/', {'confirm': 'RESET CRM DATA'}),
        ]
        before = self.records()
        self.as_user(self.staff_a)

        for method, url, body in requests:
            with self.subTest(method=method.upper(), url=url):
                if method == 'get':
                    response = self.client.get(url)
                else:
                    response = getattr(self.client, method)(url, body, format='json')
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # A document upload is multipart.
        upload = self.client.post(
            f'/api/works/{work}/documents/bank_passbook/', {'file': SimpleUploadedFile('scan.pdf', b'%PDF-1.4\n')},
            format='multipart',
        )
        self.assertEqual(upload.status_code, status.HTTP_403_FORBIDDEN)
        # None of it happened.
        self.assertEqual(self.records(), before)

    def test_staff_reach_both_activities_pages_their_account_and_their_notifications(self):
        self.as_user(self.staff_a)
        for name in ('activity-list', 'activity-works', 'auth-me', 'notification-list', 'notification-unread-count'):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(reverse('auth-me')).data['modules'], ['activities'])


class StaffRoleTests(StaffAccessTestCase):
    """Settings -> Roles & Access gives the STAFF role the Activities module or nothing: every other module stays with
    admins, alone or asked for beside Activities."""

    def set_modules(self, role, modules, method='patch'):
        return getattr(self.client, method)(reverse('role-detail', args=[role]), {'modules': modules}, format='json')

    def staff_permissions(self):
        return set(Role.objects.get(pk=Role.STAFF).permissions.values_list('codename', flat=True))

    def test_an_admin_gives_staff_the_activities_module_or_none(self):
        self.as_user(self.admin)

        cleared = self.set_modules(Role.STAFF, [])

        self.assertEqual((cleared.status_code, cleared.data['modules']), (status.HTTP_200_OK, []))
        self.assertEqual(self.staff_permissions(), set())
        self.assertFalse(User.objects.get(pk=self.staff_a.pk).has_perm('accounts.access_activities'))
        for method in ('patch', 'put'):
            with self.subTest(method=method):
                given = self.set_modules(Role.STAFF, ['activities'], method)
                self.assertEqual((given.status_code, given.data['modules']), (status.HTTP_200_OK, ['activities']))
                self.assertEqual(self.staff_permissions(), {'access_activities'})
        self.assertEqual(
            {role['name']: role['modules'] for role in self.client.get(reverse('role-list')).data},
            {Role.ADMIN: list(MODULES), Role.STAFF: ['activities']},
        )
        self.as_user(self.staff_b)
        self.assertEqual(self.client.get(reverse('auth-me')).data['modules'], ['activities'])

    def test_every_other_module_is_refused_alone_or_beside_activities_and_the_role_keeps_its_access(self):
        others = [key for key in MODULES if key != 'activities']
        self.assertEqual(others, ['dashboard', 'leads', 'work', 'reports', 'settings'])
        attempts = [
            *([key] for key in others), *(['activities', key] for key in others), *([key, 'activities'] for key in others),
            others, list(MODULES),
        ]
        self.as_user(self.admin)

        for modules in attempts:
            for method in ('patch', 'put'):
                with self.subTest(modules=modules, method=method):
                    response = self.set_modules(Role.STAFF, modules, method)
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertEqual(response.data, {'modules': ['Staff can only be given the Activities module.']})
                    self.assertEqual(self.staff_permissions(), {'access_activities'})
        staff = User.objects.get(pk=self.staff_a.pk)
        for key in others:
            for permission in MODULES[key]['permissions']:
                self.assertFalse(staff.has_perm(permission), permission)

    def test_the_admin_roles_access_still_cant_change(self):
        self.as_user(self.admin)

        for modules in ([], ['activities'], ['leads'], list(MODULES)):
            with self.subTest(modules=modules):
                response = self.set_modules(Role.ADMIN, modules)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data, {'modules': ["Admins always have every module, so their access can't change."]})
        self.assertEqual(self.client.get(reverse('auth-me')).data['modules'], list(MODULES))

    def test_the_module_list_still_offers_every_module(self):
        self.as_user(self.admin)

        response = self.client.get(reverse('user-modules'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(module['key'], module['label']) for module in response.data],
            [('dashboard', 'Dashboard'), ('leads', 'Leads'), ('work', 'Work'), ('activities', 'Activities'),
             ('reports', 'Reports'), ('settings', 'Settings')],
        )


class ActivityVisibilityTests(StaffAccessTestCase):
    """Every list holds, and every activity opens for, only what the user may see: all of them for an admin; for staff
    only those assigned to them, before any search, filter, sort, count or page."""

    def listed(self, name, **params):
        data = self.page(name, page_size=100, **params)
        return data['count'], sorted(row['id'] for row in data['results'])

    def titles_of(self, data):
        return [row['title'] for row in data['results']]

    def test_staff_lists_hold_exactly_their_own_activities_and_an_admins_hold_everyones(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.listed('activity-list'), (3, pks(self.for_a, self.done_for_a, self.on_b_lead_for_a)))
        self.assertEqual(self.listed('activity-works'), (2, pks(self.work_for_a, self.other_work_for_a)))
        self.as_user(self.staff_b)
        self.assertEqual(self.listed('activity-list'), (2, pks(self.on_a_lead_for_b, self.for_b)))
        self.assertEqual(self.listed('activity-works'), (1, pks(self.work_for_b)))
        self.as_user(self.admin)
        follow_ups = sorted(Activity.objects.filter(lead__isnull=False).values_list('pk', flat=True))
        on_works = sorted(Activity.objects.filter(work__isnull=False).values_list('pk', flat=True))
        self.assertEqual(self.listed('activity-list'), (7, follow_ups))
        self.assertEqual(self.listed('activity-works'), (5, on_works))

    def test_staff_open_their_own_activities_and_are_refused_everyone_elses(self):
        self.as_user(self.staff_a)
        for mine in (self.for_a, self.done_for_a, self.on_b_lead_for_a, self.work_for_a, self.other_work_for_a):
            with self.subTest(activity=str(mine)):
                response = self.client.get(reverse('activity-detail', args=[mine.pk]))
                self.assertEqual((response.status_code, response.data['id']), (status.HTTP_200_OK, mine.pk))
        for other in (self.on_a_lead_for_b, self.for_b, self.for_admin, self.unassigned, self.work_for_b,
                      self.work_for_admin, self.work_unassigned):
            with self.subTest(activity=str(other)):
                response = self.client.get(reverse('activity-detail', args=[other.pk]))
                self.assertEqual((response.status_code, response.data), (status.HTTP_403_FORBIDDEN, {'detail': NOT_YOURS}))
        self.assertEqual(self.client.get(reverse('activity-detail', args=[999999])).status_code, status.HTTP_404_NOT_FOUND)
        self.as_user(self.admin)
        for activity in (self.for_b, self.unassigned, self.work_unassigned):
            self.assertEqual(self.client.get(reverse('activity-detail', args=[activity.pk])).status_code, 200)

    def test_staff_searches_find_only_their_own_activities(self):
        self.as_user(self.staff_a)
        # Words only other people's activities, or their leads, hold find nothing.
        for term in ('Proposal', 'Roof survey', 'Legacy', 'Survey by B', 'Pool Lead', '98765 00003', f'#{self.pool.pk}'):
            self.assertEqual(self.page('activity-list', search=term)['count'], 0, term)
        # What her rows show of a lead (its name, phone or ID) finds her follow-ups on it.
        for term in ('Asha Menon', '98765 00001', '+91 98765 00001', f'#{self.a_lead.pk}'):
            self.assertEqual(sorted(self.titles(search=term)), ['Call Asha', 'Send brochure'], term)
        self.assertEqual(self.titles(search='Ravi Kumar'), ['Site visit with Ravi'])
        # Her own lead's email, area and PIN code, which the leads list searches, find nothing for her: she can't open
        # leads. An admin, who can, finds every follow-up on that lead by them.
        for term in ('asha.menon@example.com', 'Kakkanad', '682030'):
            with self.subTest(term=term):
                self.as_user(self.staff_a)
                self.assertEqual(self.page('activity-list', search=term)['count'], 0)
                self.as_user(self.admin)
                self.assertEqual(sorted(self.titles(search=term)), ['Call Asha', 'Legacy note', 'Send brochure', 'Survey by B'])
        # The Work Activities page: by the customer, the Work's ID, the notes or the assignee.
        self.as_user(self.staff_a)
        for term in ('Neha Thomas', str(self.work.pk), f'#{self.work.pk}', 'KSEB'):
            self.assertEqual(self.descriptions(search=term), ['Collect KSEB documents.'], term)
        self.assertEqual(self.descriptions(search=f'#{self.other_work.pk}'), ['Structure visit.'])
        self.assertEqual(sorted(self.descriptions(search='Staff A')), ['Collect KSEB documents.', 'Structure visit.'])
        for term in ('Staff B', 'Admin', 'Panel delivery', 'Subsidy', 'Meter photo'):
            self.assertEqual(self.page('activity-works', search=term)['count'], 0, term)

    def test_staff_filters_sorting_and_paging_cover_only_their_own_activities(self):
        self.as_user(self.staff_a)
        # Another person's activities, asked for by name, aren't there.
        for user in (self.staff_b, self.admin, self.admin_2):
            for name in ('activity-list', 'activity-works'):
                self.assertEqual(self.page(name, assigned_to=user.pk)['count'], 0, (name, user.name))
        self.assertEqual(self.page('activity-list', assigned_to=self.staff_a.pk)['count'], 3)
        self.assertEqual(self.titles(status='COMPLETED'), ['Send brochure'])
        self.assertEqual(sorted(self.titles(status='PENDING')), ['Call Asha', 'Site visit with Ravi'])
        self.assertEqual(self.titles(type='SITE_VISIT'), ['Site visit with Ravi'])
        self.assertEqual(self.titles(due_after='2026-10-01', due_before='2026-10-06'), ['Call Asha'])
        self.assertEqual(self.titles(ordering='due_date'), ['Send brochure', 'Call Asha', 'Site visit with Ravi'])
        self.assertEqual(self.titles(ordering='-due_date'), ['Site visit with Ravi', 'Call Asha', 'Send brochure'])
        self.assertEqual(self.titles(ordering='status'), ['Site visit with Ravi', 'Call Asha', 'Send brochure'])
        self.assertEqual(self.titles(ordering='-status'), ['Send brochure', 'Site visit with Ravi', 'Call Asha'])
        self.assertEqual(self.page('activity-works', status='COMPLETED')['count'], 0)
        self.assertEqual(self.descriptions(type='SITE_VISIT'), ['Structure visit.'])
        self.assertEqual(self.descriptions(work=self.work.pk), ['Collect KSEB documents.'])
        self.assertEqual(self.descriptions(due_after='2026-10-07'), ['Structure visit.'])
        self.assertEqual(self.descriptions(due_before='2026-10-06'), ['Collect KSEB documents.'])
        self.assertEqual(self.descriptions(ordering='work'), ['Collect KSEB documents.', 'Structure visit.'])
        self.assertEqual(self.descriptions(ordering='-work'), ['Structure visit.', 'Collect KSEB documents.'])
        self.assertEqual(self.descriptions(ordering='customer_name'), ['Structure visit.', 'Collect KSEB documents.'])
        self.assertEqual(self.descriptions(ordering='due_date'), ['Collect KSEB documents.', 'Structure visit.'])
        # Pages count and link only her own.
        first = self.page('activity-list', ordering='due_date', page_size=1)
        self.assertEqual((first['count'], self.titles_of(first), first['previous']), (3, ['Send brochure'], None))
        second = self.client.get(first['next']).data
        self.assertEqual(self.titles_of(second), ['Call Asha'])
        self.assertIsNotNone(second['previous'])
        last = self.client.get(second['next']).data
        self.assertEqual((self.titles_of(last), last['next']), (['Site visit with Ravi'], None))
        self.assertEqual(self.client.get(reverse('activity-list'), {'page_size': 1, 'page': 4}).status_code, 404)
        on_works = self.page('activity-works', page_size=1)
        self.assertEqual((on_works['count'], len(on_works['results']), on_works['previous']), (2, 1, None))
        self.assertIsNone(self.client.get(on_works['next']).data['next'])

    def test_a_leads_or_a_works_own_list_needs_its_module_which_activities_alone_doesnt_give(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.client.get(reverse('activity-list'), {'lead': self.a_lead.pk}).status_code, 403)
        self.assertEqual(self.client.get(reverse('activity-list'), {'work': self.work.pk}).status_code, 403)
        self.as_user(self.admin)
        self.assertEqual(self.page('activity-list', lead=self.a_lead.pk)['count'], 4)
        self.assertEqual(self.page('activity-list', work=self.work.pk)['count'], 4)

    def test_each_row_says_what_its_viewer_may_do_with_it(self):
        self.as_user(self.staff_a)
        rows = {row['id']: row for name in ('activity-list', 'activity-works') for row in self.page(name)['results']}
        for activity in (self.for_a, self.on_b_lead_for_a, self.work_for_a, self.other_work_for_a):
            with self.subTest(activity=str(activity)):
                row = rows[activity.pk]
                # Completing it is hers; editing, deleting and opening the lead (no Leads module, even for her own lead)
                # aren't.
                self.assertEqual(
                    (row['can_edit'], row['can_delete'], row['can_open_lead'], row['can_update_status'],
                     row['completion_note']),
                    (False, False, False, True, ''),
                )
        self.as_user(self.admin)
        rows = {row['id']: row for name in ('activity-list', 'activity-works') for row in self.page(name)['results']}
        flags = ('can_edit', 'can_delete', 'can_open_lead', 'can_update_status')
        self.assertEqual([rows[self.for_b.pk][flag] for flag in flags], [True, True, True, True])
        self.assertEqual([rows[self.work_unassigned.pk][flag] for flag in flags], [True, False, False, True])


class CompletionTests(StaffAccessTestCase):
    """Staff complete the activities assigned to them, with an optional note, through the complete action; nothing else
    about any activity is theirs to change."""

    def notices(self, user=None):
        sent = Notification.objects.filter(kind=NotificationKind.ACTIVITY_STATUS)
        return sent.filter(recipient=user) if user else sent

    def test_staff_complete_their_own_follow_up_and_work_activity_recording_when_and_by_whom(self):
        self.as_user(self.staff_a)
        listed = self.page('activity-list')['results'][0]
        for activity in (self.for_a, self.work_for_a):
            with self.subTest(activity=str(activity)):
                done = self.complete(activity)
                self.assertEqual(done.status_code, status.HTTP_200_OK, done.data)
                self.assertEqual(
                    (done.data['id'], done.data['status'], done.data['completed_by_name'], done.data['completion_note']),
                    (activity.pk, 'COMPLETED', 'Staff A', ''),
                )
                self.assertIsNotNone(done.data['completed_at'])
                # The activity as the lists show it.
                self.assertEqual(set(done.data), set(listed))
                stored = Activity.objects.get(pk=activity.pk)
                self.assertEqual((stored.status, stored.completed_by), (ActivityStatus.COMPLETED, self.staff_a))
                self.assertIsNotNone(stored.completed_at)

    def test_the_completion_note_is_optional_and_trimmed(self):
        self.as_user(self.staff_a)
        for label, body, saved in (
            ('no body', None, ''), ('no note', {}, ''), ('blank', {'completion_note': ''}, ''),
            ('spaces only', {'completion_note': '   '}, ''), ('padded', {'completion_note': f'  {NOTE}  '}, NOTE),
            ('1000 characters', {'completion_note': 'x' * 1000}, 'x' * 1000),
        ):
            with self.subTest(label):
                activity = Activity.objects.create(
                    lead=self.a_lead, title='Call back', type='PHONE_CALL', assigned_to=self.staff_a,
                    due_date=date(2026, 10, 10), created_by=self.admin,
                )
                done = self.client.post(reverse('activity-complete', args=[activity.pk]), body, format='json')
                self.assertEqual((done.status_code, done.data['completion_note']), (status.HTTP_200_OK, saved))
                self.assertEqual(Activity.objects.get(pk=activity.pk).completion_note, saved)

    def test_a_note_over_1000_characters_is_refused_and_nothing_is_saved(self):
        before = self.stored()
        self.as_user(self.staff_a)

        refused = self.complete(self.for_a, completion_note='x' * 1001)

        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refused.data, {'completion_note': ['Ensure this field has no more than 1000 characters.']})
        self.assertEqual(self.stored(), before)
        self.assertFalse(self.notices().exists())

    def test_staff_cannot_complete_another_staff_members_the_admins_or_an_unassigned_activity(self):
        before = self.stored()
        self.as_user(self.staff_a)

        for activity in (self.on_a_lead_for_b, self.for_b, self.for_admin, self.unassigned, self.work_for_b,
                         self.work_for_admin, self.work_unassigned):
            with self.subTest(activity=str(activity)):
                refused = self.complete(activity, completion_note=NOTE)
                self.assertEqual((refused.status_code, refused.data), (status.HTTP_403_FORBIDDEN, {'detail': NOT_YOURS}))
        unknown = self.client.post(reverse('activity-complete', args=[999999]), {'completion_note': NOTE}, format='json')
        self.assertEqual(unknown.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.stored(), before)
        self.assertFalse(Notification.objects.exists())

    def test_staff_cannot_complete_take_over_or_edit_any_activity_by_editing_it_even_their_own(self):
        before = self.stored()
        a = self.staff_a.pk
        self.as_user(self.staff_a)

        for activity in (self.for_a, self.work_for_a, self.for_b, self.work_for_b, self.for_admin, self.work_for_admin,
                         self.unassigned, self.work_unassigned):
            whole = (
                {'lead': activity.lead_id, 'title': 'Taken', 'type': 'NOTE', 'assigned_to': a, 'due_date': '2026-10-10',
                 'status': 'COMPLETED'}
                if activity.lead_id else
                {'work': activity.work_id, 'type': 'NOTE', 'description': 'Taken.', 'assigned_to': a, 'status': 'COMPLETED'}
            )
            url = reverse('activity-detail', args=[activity.pk])
            for method, body in (
                ('patch', {'status': 'COMPLETED'}), ('patch', {'assigned_to': a}),
                ('patch', {'assigned_to': a, 'status': 'COMPLETED'}), ('patch', {'completion_note': NOTE}), ('put', whole),
            ):
                with self.subTest(activity=str(activity), method=method, body=body):
                    self.assertEqual(getattr(self.client, method)(url, body, format='json').status_code, 403)
        self.assertEqual(self.stored(), before)
        self.assertFalse(Notification.objects.exists())

    def test_staff_cannot_add_or_delete_activities_even_for_themselves(self):
        before = self.stored()
        self.as_user(self.staff_a)

        for body in (
            {'lead': self.a_lead.pk, 'title': 'Call again', 'type': 'PHONE_CALL', 'assigned_to': self.staff_a.pk,
             'due_date': '2026-10-10'},
            {'work': self.work.pk, 'type': 'NOTE', 'description': 'Measured the roof.', 'assigned_to': self.staff_a.pk},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(reverse('activity-list'), body, format='json').status_code, 403)
        for activity in (self.for_a, self.done_for_a, self.work_for_a, self.for_b, self.unassigned):
            with self.subTest(activity=str(activity)):
                self.assertEqual(self.client.delete(reverse('activity-detail', args=[activity.pk])).status_code, 403)
        self.assertEqual(self.stored(), before)
        self.assertFalse(Notification.objects.exists())

    def test_the_completion_note_is_written_only_by_completing(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.complete(self.for_a, completion_note=NOTE).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(self.for_a, completion_note='Rewritten.').status_code, status.HTTP_403_FORBIDDEN)
        # An admin's edit leaves it as it is: it is read-only, even when the edit completes the activity.
        self.as_user(self.admin)
        for activity, body in ((self.for_a, {}), (self.for_b, {'status': 'COMPLETED'})):
            edited = self.patch(activity, completion_note='Rewritten by the admin.', **body)
            self.assertEqual(edited.status_code, status.HTTP_200_OK, edited.data)
        self.assertEqual(Activity.objects.get(pk=self.for_a.pk).completion_note, NOTE)
        self.assertEqual(
            Activity.objects.filter(pk=self.for_b.pk).values_list('status', 'completion_note').get(), ('COMPLETED', ''),
        )

    def test_completing_twice_is_refused_and_keeps_the_first_note(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.complete(self.for_a, completion_note=NOTE).status_code, status.HTTP_200_OK)
        first = Activity.objects.get(pk=self.for_a.pk)

        again = self.complete(self.for_a, completion_note='Something else.')

        self.assertEqual((again.status_code, again.data), (400, {'status': ['This activity is already completed.']}))
        stored = Activity.objects.get(pk=self.for_a.pk)
        self.assertEqual(
            (stored.completion_note, stored.completed_at, stored.completed_by), (NOTE, first.completed_at, self.staff_a),
        )
        # One completed earlier is refused the same way, and so is an admin's second try.
        self.assertEqual(self.complete(self.done_for_a).status_code, status.HTTP_400_BAD_REQUEST)
        self.as_user(self.admin)
        self.assertEqual(self.complete(self.for_a, completion_note='Admin note.').status_code, 400)
        self.assertEqual(Activity.objects.get(pk=self.for_a.pk).completion_note, NOTE)
        self.assertEqual(self.notices().count(), 2)  # the first completion's only: one to each admin

    def test_each_completion_tells_the_admins_and_the_assignee_once_never_whoever_did_it(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.complete(self.for_a, completion_note=NOTE).status_code, status.HTTP_200_OK)
        self.assertEqual(self.complete(self.work_for_a).status_code, status.HTTP_200_OK)
        # Staff A did both: each admin is told of each once; she isn't, and nobody else is.
        for admin in (self.admin, self.admin_2):
            self.assertEqual(
                [(notice.activity_id, notice.title, notice.message) for notice in self.notices(admin).order_by('id')],
                [
                    (self.for_a.pk, 'Activity completed', 'Staff A marked Call Asha for Asha Menon as completed.'),
                    (self.work_for_a.pk, 'Activity completed', 'Staff A marked Note for Neha Thomas as completed.'),
                ],
            )
        self.assertEqual(self.notices().count(), 4)
        # An admin completing Staff B's tells Staff B and the other admin.
        self.as_user(self.admin)
        self.assertEqual(self.complete(self.for_b, completion_note=NOTE).status_code, status.HTTP_200_OK)
        self.assertEqual(
            sorted(self.notices().filter(activity=self.for_b).values_list('recipient__email', flat=True)),
            ['admin2@example.com', 'b@example.com'],
        )
        self.assertEqual(self.notices(self.staff_b).get().message, 'Admin marked Proposal discussion for Ravi Kumar as completed.')
        self.assertEqual(self.notices().count(), 6)

    def test_admins_complete_anyones_activity_with_a_note(self):
        self.as_user(self.admin)

        for activity in (self.for_a, self.for_b, self.for_admin, self.unassigned, self.work_for_b, self.work_unassigned):
            with self.subTest(activity=str(activity)):
                done = self.complete(activity, completion_note=f' {NOTE} ')
                self.assertEqual(
                    (done.status_code, done.data['status'], done.data['completed_by_name'], done.data['completion_note']),
                    (status.HTTP_200_OK, 'COMPLETED', 'Admin', NOTE),
                )
                stored = Activity.objects.get(pk=activity.pk)
                self.assertEqual((stored.completed_by, stored.completion_note), (self.admin, NOTE))

    def test_staff_without_the_activities_module_complete_nothing(self):
        Role.objects.get(pk=Role.STAFF).permissions.clear()
        self.as_user(self.staff_a)

        self.assertEqual(self.complete(self.for_a, completion_note=NOTE).status_code, status.HTTP_403_FORBIDDEN)
        for name in ('activity-list', 'activity-works'):
            self.assertEqual(self.client.get(reverse(name)).status_code, status.HTTP_403_FORBIDDEN, name)
        self.assertEqual(Activity.objects.get(pk=self.for_a.pk).status, ActivityStatus.PENDING)


class ReopenTests(StaffAccessTestCase):
    """Only an admin reopens a completed Work activity, which clears its completion and note; a lead's follow-up stays
    completed."""

    def test_an_admin_reopens_a_completed_work_activity_which_clears_its_completion_and_note(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.complete(self.work_for_a, completion_note=NOTE).status_code, status.HTTP_200_OK)
        self.as_user(self.admin)

        reopened = self.patch(self.work_for_a, status='PENDING')

        self.assertEqual(reopened.status_code, status.HTTP_200_OK, reopened.data)
        self.assertEqual(
            (reopened.data['status'], reopened.data['completed_at'], reopened.data['completed_by_name'],
             reopened.data['completion_note']),
            ('PENDING', None, None, ''),
        )
        stored = Activity.objects.get(pk=self.work_for_a.pk)
        self.assertEqual(
            (stored.status, stored.completed_at, stored.completed_by, stored.completion_note),
            (ActivityStatus.PENDING, None, None, ''),
        )
        # As before, the other admin and the assignee are told it was reopened.
        told = Notification.objects.filter(kind=NotificationKind.ACTIVITY_STATUS, title='Activity reopened')
        self.assertEqual(sorted(told.values_list('recipient__email', flat=True)), ['a@example.com', 'admin2@example.com'])
        self.assertEqual({notice.message for notice in told}, {'Admin marked Note for Neha Thomas as pending.'})
        # Its staff can complete it again, with a new note.
        self.as_user(self.staff_a)
        again = self.complete(self.work_for_a, completion_note='Second visit done.')
        self.assertEqual((again.status_code, again.data['completion_note']), (status.HTTP_200_OK, 'Second visit done.'))

    def test_staff_cannot_reopen_even_their_own(self):
        self.as_user(self.staff_a)

        for activity in (self.work_for_a, self.for_a):
            with self.subTest(activity=str(activity)):
                self.assertEqual(self.complete(activity, completion_note=NOTE).status_code, status.HTTP_200_OK)
                self.assertEqual(self.patch(activity, status='PENDING').status_code, status.HTTP_403_FORBIDDEN)
                stored = Activity.objects.get(pk=activity.pk)
                self.assertEqual(
                    (stored.status, stored.completed_by, stored.completion_note), (ActivityStatus.COMPLETED, self.staff_a, NOTE),
                )
        self.assertFalse(Notification.objects.filter(title='Activity reopened').exists())

    def test_a_completed_follow_up_still_cant_be_reopened_even_by_an_admin(self):
        self.as_user(self.staff_a)
        self.assertEqual(self.complete(self.for_a, completion_note=NOTE).status_code, status.HTTP_200_OK)
        self.as_user(self.admin)

        refused = self.patch(self.for_a, status='PENDING')

        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refused.data, {'status': ['A completed follow-up stays completed.']})
        stored = Activity.objects.get(pk=self.for_a.pk)
        self.assertEqual((stored.status, stored.completion_note), (ActivityStatus.COMPLETED, NOTE))


class AdminActivityTests(StaffAccessTestCase):
    """What admins do with activities is unchanged by the staff rules."""

    def assigned_notices(self, user):
        return Notification.objects.filter(kind=NotificationKind.ACTIVITY_ASSIGNED, recipient=user).count()

    def test_an_admin_adds_edits_reassigns_completes_and_deletes_a_follow_up(self):
        self.as_user(self.admin)
        body = {
            'lead': self.b_lead.pk, 'title': 'Call about the quotation', 'type': 'PHONE_CALL',
            'assigned_to': self.staff_b.pk, 'due_date': '2026-10-12', 'description': 'Customer asked for a callback.',
        }

        created = self.client.post(reverse('activity-list'), body, format='json')

        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(
            (created.data['status'], created.data['created_by_name'], created.data['completion_note']), ('PENDING', 'Admin', ''),
        )
        self.assertEqual(self.assigned_notices(self.staff_b), 1)
        activity = Activity.objects.get(pk=created.data['id'])
        url = reverse('activity-detail', args=[activity.pk])
        edited = self.client.put(url, {**body, 'title': 'Call about the final quotation'}, format='json')
        self.assertEqual((edited.status_code, edited.data['title']), (status.HTTP_200_OK, 'Call about the final quotation'))
        self.assertEqual(self.assigned_notices(self.staff_b), 1)  # the same staff: not told again
        moved = self.patch(activity, assigned_to=self.staff_a.pk)
        self.assertEqual((moved.status_code, moved.data['assigned_to_name']), (status.HTTP_200_OK, 'Staff A'))
        self.assertEqual(self.assigned_notices(self.staff_a), 1)
        done = self.patch(activity, status='COMPLETED')
        self.assertEqual(
            (done.status_code, done.data['status'], done.data['completed_by_name']), (status.HTTP_200_OK, 'COMPLETED', 'Admin'),
        )
        told = Notification.objects.filter(kind=NotificationKind.ACTIVITY_STATUS, activity=activity)
        self.assertEqual(sorted(told.values_list('recipient__email', flat=True)), ['a@example.com', 'admin2@example.com'])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Activity.objects.filter(pk=activity.pk).exists())

    def test_an_admin_adds_and_edits_work_activities_which_are_never_deleted(self):
        self.as_user(self.admin)
        body = {'work': self.work.pk, 'type': 'SITE_VISIT', 'description': 'Roof measured.', 'assigned_to': self.staff_b.pk,
                'status': 'COMPLETED'}

        added = self.client.post(reverse('activity-list'), body, format='json')

        self.assertEqual(added.status_code, status.HTTP_201_CREATED, added.data)
        self.assertEqual((added.data['status'], added.data['completed_by_name']), ('COMPLETED', 'Admin'))
        self.assertEqual(self.assigned_notices(self.staff_b), 1)
        activity = Activity.objects.get(pk=added.data['id'])
        edited = self.patch(activity, description='Roof measured again.', assigned_to=self.staff_a.pk)
        self.assertEqual((edited.status_code, edited.data['description']), (status.HTTP_200_OK, 'Roof measured again.'))
        self.assertEqual(self.assigned_notices(self.staff_a), 1)
        refused = self.client.delete(reverse('activity-detail', args=[activity.pk]))
        self.assertEqual(
            (refused.status_code, refused.data['detail']),
            (status.HTTP_403_FORBIDDEN, "A Work's activities are kept as its history. Edit the activity instead."),
        )
        self.assertTrue(Activity.objects.filter(pk=activity.pk).exists())

    def test_an_admins_lists_are_unaffected_by_the_staff_rules(self):
        self.as_user(self.admin)
        # Everyone's, newest first; on the Work Activities page each Work's together, newest Work first.
        self.assertEqual(
            self.titles(),
            ['Legacy note', 'Roof survey', 'Proposal discussion', 'Survey by B', 'Site visit with Ravi', 'Send brochure',
             'Call Asha'],
        )
        self.assertEqual(
            self.descriptions(),
            ['Structure visit.', 'Panel delivery check.', 'Collect KSEB documents.', 'Meter photo.', 'Subsidy papers.'],
        )
        self.assertEqual(sorted(self.titles(assigned_to=self.staff_b.pk)), ['Proposal discussion', 'Survey by B'])
        self.assertEqual(self.descriptions(assigned_to=self.staff_b.pk), ['Panel delivery check.'])
        self.assertEqual(self.titles(search='Legacy'), ['Legacy note'])
        self.assertEqual(self.descriptions(search='Staff B'), ['Panel delivery check.'])

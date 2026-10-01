from django.contrib.auth import authenticate
from django.contrib.auth.hashers import identify_hasher
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.leads.models import Lead, SolarPlan

from .models import Department, Role, User
from .permissions import MODULES

PASSWORD = 'Solar-Panel-2026'
SENSITIVE_FIELDS = {
    'password', 'confirm_password', 'is_staff', 'is_superuser', 'groups', 'user_permissions', 'last_login',
}


def grant(user, *codenames):
    """Gives the user's role these permissions: access comes from roles only."""
    user.role.permissions.add(*Permission.objects.filter(content_type__app_label='accounts', codename__in=codenames))
    # The role caches its permissions per instance, so hand back a fresh user.
    return User.objects.get(pk=user.pk)


class UserModelTests(TestCase):
    def test_create_user_normalizes_email_hashes_password_and_defaults_to_staff(self):
        user = User.objects.create_user(email='  Asha@Example.COM ', password=PASSWORD, name='Asha')

        self.assertEqual(user.email, 'asha@example.com')
        self.assertNotEqual(user.password, PASSWORD)
        identify_hasher(user.password)  # raises ValueError unless it is a real Django password hash
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.role_id, Role.STAFF)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_the_two_roles_exist_and_users_reference_one(self):
        self.assertEqual(list(Role.objects.order_by('pk').values_list('pk', flat=True)), [Role.ADMIN, Role.STAFF])
        self.assertIs(User._meta.get_field('role').related_model, Role)

    def test_permissions_given_to_a_user_directly_grant_nothing(self):
        staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')
        staff.user_permissions.add(Permission.objects.get(content_type__app_label='accounts', codename='view_user'))

        self.assertFalse(User.objects.get(pk=staff.pk).has_perm('accounts.view_user'))

    def test_duplicate_email_is_rejected_regardless_of_case(self):
        User.objects.create_user(email='asha@example.com', password=PASSWORD, name='Asha')

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email='ASHA@example.com', password=PASSWORD, name='Other')

    def test_create_superuser_is_a_crm_admin_with_django_admin_access(self):
        user = User.objects.create_superuser(email='owner@example.com', password=PASSWORD, name='Owner')

        self.assertEqual(user.role_id, Role.ADMIN)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_authentication_uses_email_case_insensitively_and_rejects_inactive_users(self):
        user = User.objects.create_user(email='asha@example.com', password=PASSWORD, name='Asha')

        self.assertEqual(authenticate(email='ASHA@example.com', password=PASSWORD), user)
        self.assertIsNone(authenticate(email='asha@example.com', password='wrong-password'))

        user.is_active = False
        user.save()
        self.assertIsNone(authenticate(email='asha@example.com', password=PASSWORD))

    def test_admin_role_holds_every_permission_while_staff_holds_only_granted_ones(self):
        admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')

        self.assertFalse(admin.is_superuser)
        self.assertTrue(admin.has_perm('accounts.delete_department'))
        self.assertFalse(staff.has_perm('accounts.view_department'))

        staff = grant(staff, 'view_department')
        self.assertTrue(staff.has_perm('accounts.view_department'))
        self.assertFalse(staff.has_perm('accounts.delete_department'))

    def test_inactive_admin_holds_no_permissions(self):
        admin = User.objects.create_user(
            email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN, is_active=False,
        )

        self.assertFalse(admin.has_perm('accounts.view_department'))


class AuthApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='asha@example.com', password=PASSWORD, name='Asha')

    def login(self, email, password=PASSWORD):
        return self.client.post(reverse('auth-login'), {'email': email, 'password': password}, format='json')

    def test_login_with_email_returns_access_and_refresh_tokens(self):
        response = self.login('ASHA@example.com')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data), {'access', 'refresh'})

    def test_admin_logs_in_and_me_lists_every_module_while_new_staff_has_none(self):
        User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)

        for email, role, modules in (('admin@example.com', Role.ADMIN, list(MODULES)), ('asha@example.com', Role.STAFF, [])):
            with self.subTest(role=role):
                login = self.login(email)
                self.assertEqual(login.status_code, status.HTTP_200_OK)
                self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {login.data["access"]}')

                me = self.client.get(reverse('auth-me')).data

                self.assertEqual((me['role'], me['modules']), (role, modules))
                self.assertFalse(SENSITIVE_FIELDS & set(me))

    def test_wrong_password_and_unknown_email_get_the_same_response(self):
        wrong_password = self.login('asha@example.com', 'wrong-password')
        unknown_email = self.login('nobody@example.com')

        self.assertEqual(wrong_password.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(unknown_email.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(wrong_password.data, unknown_email.data)

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()

        self.assertEqual(self.login('asha@example.com').status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_token_returns_a_new_access_token(self):
        refresh = self.login('asha@example.com').data['refresh']

        response = self.client.post(reverse('auth-refresh'), {'refresh': refresh}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

    def test_me_returns_the_authenticated_user_without_sensitive_fields(self):
        access = self.login('asha@example.com').data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

        response = self.client.get(reverse('auth-me'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['email'], 'asha@example.com')
        self.assertEqual(response.data['role'], Role.STAFF)
        self.assertFalse(SENSITIVE_FIELDS & set(response.data))

    def test_me_requires_authentication(self):
        self.assertEqual(self.client.get(reverse('auth-me')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_token_stops_working_once_the_user_is_deactivated(self):
        access = self.login('asha@example.com').data['access']
        self.user.is_active = False
        self.user.save()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

        self.assertEqual(self.client.get(reverse('auth-me')).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_change_revokes_tokens_issued_before_it(self):
        tokens = self.login('asha@example.com').data
        self.user.set_password('Fresh-Solar-Key-88')
        self.user.save()
        refreshed_access = self.client.post(reverse('auth-refresh'), {'refresh': tokens['refresh']}, format='json').data['access']

        for access in (tokens['access'], refreshed_access):
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
            self.assertEqual(self.client.get(reverse('auth-me')).status_code, status.HTTP_401_UNAUTHORIZED)


class UserApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')
        cls.department = Department.objects.create(name='Sales')

    def new_user(self, **overrides):
        return {'name': 'Ravi', 'email': 'ravi@example.com', 'password': PASSWORD, 'confirm_password': PASSWORD, **overrides}

    def test_admin_creates_a_staff_user_who_gets_the_staff_role_access(self):
        Role.objects.get(pk=Role.STAFF).permissions.add(Permission.objects.get(codename='view_department'))
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            reverse('user-list'), self.new_user(role=Role.STAFF, department=self.department.pk), format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(SENSITIVE_FIELDS & set(response.data))
        self.assertEqual((response.data['role'], response.data['modules']), (Role.STAFF, []))
        user = User.objects.get(email='ravi@example.com')
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.department, self.department)
        self.assertTrue(user.has_perm('accounts.view_department'))
        self.assertFalse(user.has_perm('accounts.view_user'))

    def test_admin_creates_an_admin(self):
        self.client.force_authenticate(self.admin)

        response = self.client.post(reverse('user-list'), self.new_user(role=Role.ADMIN), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual((response.data['role'], response.data['modules']), (Role.ADMIN, list(MODULES)))

    def test_create_rejects_duplicate_email_invalid_role_weak_password_and_inactive_department(self):
        inactive = Department.objects.create(name='Closed', is_active=False)
        self.client.force_authenticate(self.admin)
        cases = {
            'email': self.new_user(email='STAFF@example.com'),
            'role': self.new_user(role='MANAGER'),
            'password': self.new_user(password='12345'),
            'department': self.new_user(department=inactive.pk),
        }

        for field, payload in cases.items():
            with self.subTest(field=field):
                response = self.client.post(reverse('user-list'), payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_staff_cannot_create_or_edit_users_even_with_user_model_permissions(self):
        self.client.force_authenticate(grant(self.staff, 'add_user', 'change_user'))

        create = self.client.post(reverse('user-list'), self.new_user(), format='json')
        create_admin = self.client.post(reverse('user-list'), self.new_user(role=Role.ADMIN), format='json')
        promote = self.client.patch(
            reverse('user-detail', args=[self.staff.pk]), {'role': Role.ADMIN}, format='json',
        )

        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(create_admin.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(promote.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(email='ravi@example.com').exists())
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.role_id, Role.STAFF)

    def test_staff_needs_view_permission_to_list_users(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(reverse('user-list')).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(grant(self.staff, 'view_user'))
        response = self.client.get(reverse('user-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({row['email'] for row in response.data['results']}, {'admin@example.com', 'staff@example.com'})
        for row in response.data['results']:
            self.assertFalse(SENSITIVE_FIELDS & set(row))

    def test_admin_deactivates_a_user_but_cannot_deactivate_or_demote_themselves(self):
        self.client.force_authenticate(self.admin)

        deactivate = self.client.patch(
            reverse('user-detail', args=[self.staff.pk]), {'is_active': False}, format='json',
        )
        self_deactivate = self.client.patch(
            reverse('user-detail', args=[self.admin.pk]), {'is_active': False}, format='json',
        )
        self_demote = self.client.patch(
            reverse('user-detail', args=[self.admin.pk]), {'role': Role.STAFF}, format='json',
        )

        self.assertEqual(deactivate.status_code, status.HTTP_200_OK)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        self.assertEqual(self_deactivate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self_demote.status_code, status.HTTP_400_BAD_REQUEST)

    def test_demoting_a_superuser_to_staff_removes_superuser_access(self):
        owner = User.objects.create_superuser(email='owner@example.com', password=PASSWORD, name='Owner')
        self.client.force_authenticate(self.admin)

        response = self.client.patch(reverse('user-detail', args=[owner.pk]), {'role': Role.STAFF}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        owner.refresh_from_db()
        self.assertFalse(owner.is_superuser)
        self.assertFalse(owner.is_staff)
        self.assertFalse(owner.has_perm('accounts.view_department'))

    def test_user_keeps_a_since_deactivated_department_on_a_full_update(self):
        self.staff.department = self.department
        self.staff.save()
        Department.objects.filter(pk=self.department.pk).update(is_active=False)
        self.client.force_authenticate(self.admin)
        url = reverse('user-detail', args=[self.staff.pk])
        body = {**self.client.get(url).data, 'phone': '98765 43210'}

        response = self.client.put(url, body, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['department'], self.department.pk)
        self.assertEqual(response.data['phone'], '98765 43210')

    def test_admin_password_update_is_hashed_and_must_be_confirmed(self):
        self.client.force_authenticate(self.admin)
        url = reverse('user-detail', args=[self.staff.pk])

        mismatch = self.client.patch(url, {'password': 'New-Solar-Key-77', 'confirm_password': 'New-Solar-Key-78'}, format='json')
        response = self.client.patch(url, {'password': 'New-Solar-Key-77', 'confirm_password': 'New-Solar-Key-77'}, format='json')

        self.assertEqual(mismatch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_password', mismatch.data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password('New-Solar-Key-77'))

    def test_confirm_password_must_match_and_is_never_stored(self):
        self.client.force_authenticate(self.admin)

        mismatch = self.client.post(reverse('user-list'), self.new_user(confirm_password='Solar-Panel-2027'), format='json')
        without_confirm = {key: value for key, value in self.new_user().items() if key != 'confirm_password'}
        missing = self.client.post(reverse('user-list'), without_confirm, format='json')
        created = self.client.post(reverse('user-list'), self.new_user(), format='json')

        self.assertEqual(mismatch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_password', mismatch.data)
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_password', missing.data)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('confirm_password', created.data)
        self.assertNotIn('confirm_password', {field.name for field in User._meta.get_fields()})
        self.assertEqual(User.objects.filter(email='ravi@example.com').count(), 1)

    def test_admin_deletes_staff_without_records(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(reverse('user-detail', args=[self.staff.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=self.staff.pk).exists())

    def test_staff_with_crm_records_admins_and_staff_requests_cannot_delete(self):
        plan = SolarPlan.objects.get(capacity=3)  # created by the leads data migration
        Lead.objects.create(name='Customer', phone='9876543210', district='Kochi', plan=plan, amount=180000, created_by=self.staff)
        other_staff = User.objects.create_user(email='other@example.com', password=PASSWORD, name='Other')

        self.client.force_authenticate(self.admin)
        with_records = self.client.delete(reverse('user-detail', args=[self.staff.pk]))
        admin = self.client.delete(reverse('user-detail', args=[self.admin.pk]))
        self.client.force_authenticate(self.staff)
        by_staff = self.client.delete(reverse('user-detail', args=[other_staff.pk]))

        self.assertEqual(with_records.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(admin.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(by_staff.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(User.objects.filter(pk__in=[self.staff.pk, self.admin.pk, other_staff.pk]).count(), 3)

    def test_unauthenticated_requests_are_rejected(self):
        for url in (reverse('user-list'), reverse('department-list')):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)


class RoleAccessTests(APITestCase):
    # Most use the Settings module: its permissions belong to this app, so they exist in every test database.

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')

    def set_role_modules(self, role, modules):
        return self.client.patch(reverse('role-detail', args=[role]), {'modules': modules}, format='json')

    def test_admin_lists_both_roles_with_their_access(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse('role-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(role['name'], role['label'], role['modules']) for role in response.data],
            [(Role.ADMIN, 'Admin', list(MODULES)), (Role.STAFF, 'Staff', [])],
        )

    def test_staff_role_access_is_stored_on_the_role_and_reaches_every_staff_user(self):
        other_staff = User.objects.create_user(email='other@example.com', password=PASSWORD, name='Other')
        self.client.force_authenticate(self.admin)

        response = self.set_role_modules(Role.STAFF, ['settings'])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['modules'], ['settings'])
        self.assertEqual(Role.objects.get(pk=Role.STAFF).permission_names, set(MODULES['settings']['permissions']))
        for user in (self.staff, other_staff):
            with self.subTest(user=user.email):
                self.assertTrue(User.objects.get(pk=user.pk).has_perms(MODULES['settings']['permissions']))
        self.assertFalse(User.objects.get(pk=self.staff.pk).user_permissions.exists())

        self.set_role_modules(Role.STAFF, [])
        self.assertFalse(User.objects.get(pk=self.staff.pk).has_perm('accounts.view_user'))

    def test_role_access_is_enforced_by_the_api(self):
        self.client.force_authenticate(self.staff)
        for url in (reverse('user-list'), reverse('department-list')):
            with self.subTest(url=url, access=False):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin)
        self.set_role_modules(Role.STAFF, ['settings'])
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))

        for url in (reverse('user-list'), reverse('department-list')):
            with self.subTest(url=url, access=True):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        # Settings access is read-only: user and role management stay ADMIN-only.
        create = self.client.post(
            reverse('user-list'),
            {'name': 'Ravi', 'email': 'ravi@example.com', 'password': PASSWORD, 'confirm_password': PASSWORD},
            format='json',
        )
        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(reverse('role-list')).status_code, status.HTTP_403_FORBIDDEN)

    def test_leads_access_on_the_staff_role_opens_the_leads_api(self):
        url = reverse('lead-list')
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin)
        self.assertEqual(self.set_role_modules(Role.STAFF, ['leads']).data['modules'], ['leads'])
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))

        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_modules_without_their_own_backend_grant_their_access_permission(self):
        self.client.force_authenticate(self.admin)

        response = self.set_role_modules(Role.STAFF, ['dashboard', 'work', 'activities'])

        self.assertEqual(response.data['modules'], ['dashboard', 'work', 'activities'])
        staff = User.objects.get(pk=self.staff.pk)
        self.assertTrue(staff.has_perms(['accounts.access_dashboard', 'accounts.access_work', 'accounts.access_activities']))
        self.assertFalse(staff.has_perm('accounts.access_reports'))

    def test_staff_cannot_view_or_change_roles_even_with_settings_access(self):
        self.client.force_authenticate(self.admin)
        self.set_role_modules(Role.STAFF, ['settings'])
        self.client.force_authenticate(User.objects.get(pk=self.staff.pk))

        responses = [
            self.client.get(reverse('role-list')),
            self.set_role_modules(Role.STAFF, list(MODULES)),
            self.client.patch(reverse('user-detail', args=[self.staff.pk]), {'role': Role.ADMIN}, format='json'),
        ]

        self.assertEqual([response.status_code for response in responses], [status.HTTP_403_FORBIDDEN] * 3)
        self.assertEqual(Role.objects.get(pk=Role.STAFF).permission_names, set(MODULES['settings']['permissions']))
        self.assertEqual(User.objects.get(pk=self.staff.pk).role_id, Role.STAFF)

    def test_admin_role_access_cannot_change(self):
        self.client.force_authenticate(self.admin)

        response = self.set_role_modules(Role.ADMIN, ['leads'])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('modules', response.data)

    def test_unknown_module_is_rejected(self):
        self.client.force_authenticate(self.admin)

        response = self.set_role_modules(Role.STAFF, ['payroll'])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('modules', response.data)

    def test_access_cannot_be_set_per_user(self):
        self.client.force_authenticate(self.admin)

        response = self.client.patch(reverse('user-detail', args=[self.staff.pk]), {'modules': ['settings']}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['modules'], [])
        self.assertFalse(User.objects.get(pk=self.staff.pk).has_perm('accounts.view_user'))

    def test_admin_keeps_full_access_to_user_and_role_management(self):
        self.client.force_authenticate(self.admin)

        for url in (reverse('user-list'), reverse('user-modules'), reverse('department-list'), reverse('role-list')):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        edit = self.client.patch(reverse('user-detail', args=[self.staff.pk]), {'name': 'Staff Two'}, format='json')
        deactivate = self.client.patch(reverse('user-detail', args=[self.staff.pk]), {'is_active': False}, format='json')

        self.assertEqual(edit.status_code, status.HTTP_200_OK)
        self.assertEqual(deactivate.status_code, status.HTTP_200_OK)
        self.assertFalse(User.objects.get(pk=self.staff.pk).is_active)

    def test_module_list_needs_the_view_user_permission(self):
        url = reverse('user-modules')
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([module['key'] for module in response.data], list(MODULES))


class DepartmentApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Staff')

    def test_admin_creates_a_department_and_duplicate_names_are_rejected_regardless_of_case(self):
        self.client.force_authenticate(self.admin)

        created = self.client.post(reverse('department-list'), {'name': 'Installation'}, format='json')
        duplicate = self.client.post(reverse('department-list'), {'name': 'INSTALLATION'}, format='json')

        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertTrue(created.data['is_active'])
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', duplicate.data)

    def test_staff_access_follows_granted_department_permissions(self):
        url = reverse('department-list')
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(grant(self.staff, 'view_department'))
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post(url, {'name': 'Sales'}, format='json').status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(grant(self.staff, 'add_department'))
        self.assertEqual(self.client.post(url, {'name': 'Sales'}, format='json').status_code, status.HTTP_201_CREATED)

    def test_form_encoded_writes_are_rejected(self):
        # Form parsing would read the omitted is_active as False and silently create an inactive record.
        self.client.force_authenticate(self.admin)

        response = self.client.post(reverse('department-list'), {'name': 'Sales'}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        self.assertFalse(Department.objects.exists())

    def test_department_with_users_cannot_be_deleted_but_an_unused_one_can(self):
        used = Department.objects.create(name='Sales')
        unused = Department.objects.create(name='Temporary')
        User.objects.filter(pk=self.staff.pk).update(department=used)
        self.client.force_authenticate(self.admin)

        blocked = self.client.delete(reverse('department-detail', args=[used.pk]))
        deleted = self.client.delete(reverse('department-detail', args=[unused.pk]))

        self.assertEqual(blocked.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Department.objects.filter(pk=used.pk).exists())
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Department.objects.filter(pk=unused.pk).exists())

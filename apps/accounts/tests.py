from django.contrib.auth import authenticate
from django.contrib.auth.hashers import identify_hasher
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Department, Role, User

PASSWORD = 'Solar-Panel-2026'
SENSITIVE_FIELDS = {'password', 'is_staff', 'is_superuser', 'groups', 'user_permissions', 'last_login'}


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label='accounts', codename__in=codenames))
    # Django caches permissions per instance, so hand back a fresh one.
    return User.objects.get(pk=user.pk)


class UserModelTests(TestCase):
    def test_create_user_normalizes_email_hashes_password_and_defaults_to_staff(self):
        user = User.objects.create_user(email='  Asha@Example.COM ', password=PASSWORD, name='Asha')

        self.assertEqual(user.email, 'asha@example.com')
        self.assertNotEqual(user.password, PASSWORD)
        identify_hasher(user.password)  # raises ValueError unless it is a real Django password hash
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.role, Role.STAFF)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_duplicate_email_is_rejected_regardless_of_case(self):
        User.objects.create_user(email='asha@example.com', password=PASSWORD, name='Asha')

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email='ASHA@example.com', password=PASSWORD, name='Other')

    def test_create_superuser_is_a_crm_admin_with_django_admin_access(self):
        user = User.objects.create_superuser(email='owner@example.com', password=PASSWORD, name='Owner')

        self.assertEqual(user.role, Role.ADMIN)
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
        return {'name': 'Ravi', 'email': 'ravi@example.com', 'password': PASSWORD, **overrides}

    def test_admin_creates_a_user_with_department_and_permissions(self):
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            reverse('user-list'),
            self.new_user(department=self.department.pk, permissions=['accounts.view_department']),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(SENSITIVE_FIELDS & set(response.data))
        self.assertEqual(response.data['permissions'], ['accounts.view_department'])
        user = User.objects.get(email='ravi@example.com')
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.department, self.department)
        self.assertEqual(user.role, Role.STAFF)
        self.assertTrue(user.has_perm('accounts.view_department'))

    def test_create_rejects_duplicate_email_invalid_role_weak_password_and_inactive_department(self):
        inactive = Department.objects.create(name='Closed', is_active=False)
        self.client.force_authenticate(self.admin)
        cases = {
            'email': self.new_user(email='STAFF@example.com'),
            'role': self.new_user(role='MANAGER'),
            'password': self.new_user(password='12345'),
            'department': self.new_user(department=inactive.pk),
            'permissions': self.new_user(permissions=['accounts.fly_rocket']),
        }

        for field, payload in cases.items():
            with self.subTest(field=field):
                response = self.client.post(reverse('user-list'), payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_staff_cannot_create_or_edit_users_even_with_user_model_permissions(self):
        self.client.force_authenticate(grant(self.staff, 'add_user', 'change_user'))

        create = self.client.post(reverse('user-list'), self.new_user(), format='json')
        promote = self.client.patch(
            reverse('user-detail', args=[self.staff.pk]), {'role': Role.ADMIN}, format='json',
        )

        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(promote.status_code, status.HTTP_403_FORBIDDEN)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.role, Role.STAFF)

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

    def test_admin_password_update_is_hashed(self):
        self.client.force_authenticate(self.admin)

        response = self.client.patch(
            reverse('user-detail', args=[self.staff.pk]), {'password': 'New-Solar-Key-77'}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password('New-Solar-Key-77'))

    def test_users_cannot_be_deleted(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(reverse('user-detail', args=[self.staff.pk]))

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())

    def test_unauthenticated_requests_are_rejected(self):
        for url in (reverse('user-list'), reverse('department-list')):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)


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

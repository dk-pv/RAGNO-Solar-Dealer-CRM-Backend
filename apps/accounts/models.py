from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import Permission, PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.functional import cached_property


class Role(models.Model):
    """A user's role, which decides what they can open (Settings → Roles & Access). There are exactly two.

    The name is the primary key, so users store "ADMIN" or "STAFF" and `filter(role=Role.ADMIN)` reads naturally."""

    ADMIN = 'ADMIN'
    STAFF = 'STAFF'

    name = models.CharField(max_length=10, primary_key=True, choices=[(ADMIN, 'Admin'), (STAFF, 'Staff')])
    description = models.CharField(max_length=200, blank=True)
    # The permissions of the modules this role's users can open. ADMIN needs none: it holds every permission.
    permissions = models.ManyToManyField(Permission, blank=True, related_name='roles')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.get_name_display()

    @cached_property
    def permission_names(self):
        """Its permissions as "app_label.codename", the strings has_perm() takes. Cached for this instance only."""
        names = self.permissions.values_list('content_type__app_label', 'codename')
        return {f'{app_label}.{codename}' for app_label, codename in names}


phone_validator = RegexValidator(
    r'^\+?[0-9][0-9 -]{5,18}$',
    'Enter a valid phone number: digits, spaces or hyphens, optionally starting with +.',
)


class Department(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower('name'), name='accounts_department_name_ci_unique'),
        ]

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        # Email is the login identity; storing it lowercased makes login and uniqueness case-insensitive.
        return (email or '').strip().lower()

    def get_by_natural_key(self, email):
        return self.get(email=self.normalize_email(email))

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Users must have an email address.')
        # The role may be given by name, as in create_user(..., role=Role.ADMIN).
        if isinstance(extra_fields.get('role'), str):
            extra_fields['role_id'] = extra_fields.pop('role')
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role_id', Role.ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, validators=[phone_validator])
    role = models.ForeignKey(Role, on_delete=models.PROTECT, default=Role.STAFF, related_name='users')
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='users',
    )
    is_active = models.BooleanField(default=True)
    # Grants access to the Django admin site only. It is unrelated to the CRM STAFF role.
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    EMAIL_FIELD = 'email'
    REQUIRED_FIELDS = ['name']

    class Meta:
        # Access to the modules that have no models of their own yet (see MODULES in permissions.py).
        permissions = [
            ('access_dashboard', 'Can open the Dashboard'),
            ('access_work', 'Can open Work'),
            ('access_activities', 'Can open Activities'),
            ('access_reports', 'Can open Reports'),
        ]

    def has_perm(self, perm, obj=None):
        # Access comes from the role alone: ADMIN holds every permission, STAFF what its role is given.
        # Per-user and group permissions are not used, so one role change reaches every user in it.
        # ponytail: sync checks only; mirror this in ahas_perm if async views are ever added.
        if not self.is_active:
            return False
        return self.role_id == Role.ADMIN or perm in self.role.permission_names

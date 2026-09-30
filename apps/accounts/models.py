from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    ADMIN = 'ADMIN', 'Admin'
    STAFF = 'STAFF', 'Staff'


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
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', Role.ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, validators=[phone_validator])
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
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
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=Role.values), name='accounts_user_role_valid'),
        ]

    def has_perm(self, perm, obj=None):
        # ADMIN is the CRM's full-access role, so it passes every Django permission check, like a superuser.
        # ponytail: sync checks only; mirror this in ahas_perm if async views are ever added.
        if self.is_active and self.role == Role.ADMIN:
            return True
        return super().has_perm(perm, obj)

from django.contrib.auth.models import Permission
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import serializers

from .models import Department, Role, User
from .permissions import MODULES


def permission_name(permission):
    return f'{permission.content_type.app_label}.{permission.codename}'


class ModulesField(serializers.ListField):
    """The modules a role (or a user, through their role) can open, as MODULES keys.
    Written keys are resolved to the modules' permissions."""

    child = serializers.ChoiceField(choices=list(MODULES))

    def get_attribute(self, instance):
        return instance if isinstance(instance, Role) else instance.role

    def to_representation(self, role):
        if role.pk == Role.ADMIN:
            return list(MODULES)
        # Read from the prefetched permissions, so a list doesn't query each role's permissions.
        held = {permission_name(permission) for permission in role.permissions.all()}
        return [key for key, module in MODULES.items() if held.issuperset(module['permissions'])]

    def to_internal_value(self, data):
        permissions = []
        for key in dict.fromkeys(super().to_internal_value(data)):
            names = MODULES[key]['permissions']
            match = Q()
            for name in names:
                app_label, _, codename = name.partition('.')
                match |= Q(content_type__app_label=app_label, codename=codename)
            found = list(Permission.objects.filter(match))
            # A module whose app isn't migrated yet has no permissions: refuse it rather than grant part of it.
            if len(found) != len(names):
                raise serializers.ValidationError(
                    f"{MODULES[key]['label']} access can't be given yet: that module isn't set up on the server."
                )
            permissions += found
        return permissions


class RoleSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source='get_name_display', read_only=True)
    modules = ModulesField(required=False)

    class Meta:
        model = Role
        fields = ['name', 'label', 'description', 'modules']
        read_only_fields = ['name', 'description']

    def validate(self, attrs):
        # ADMIN holds every permission through User.has_perm, so an admin can never be locked out of Settings.
        if self.instance.pk == Role.ADMIN and 'modules' in attrs:
            raise serializers.ValidationError({'modules': "Admins always have every module, so their access can't change."})
        # Staff work only on the activities assigned to them: Activities is the one module they can be given, and Leads,
        # Work, Dashboard, Reports and Settings stay with admins.
        granted = {permission_name(permission) for permission in attrs.get('modules', [])}
        if not granted <= set(MODULES['activities']['permissions']):
            raise serializers.ValidationError({'modules': 'Staff can only be given the Activities module.'})
        return attrs

    def update(self, role, validated_data):
        if 'modules' in validated_data:
            role.permissions.set(validated_data['modules'])
            role.save(update_fields=['updated_at'])
        return role


class UserSerializer(serializers.ModelSerializer):
    # Declared explicitly so uniqueness is checked on the normalized value in validate_email().
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    # Only catches a mistyped password: it must match `password` and is never stored.
    confirm_password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        pk_field=serializers.IntegerField(),
        required=False,
        allow_null=True,
    )
    department_name = serializers.SerializerMethodField()
    # What the user can open, from their role. Access is changed on the role, never per user.
    modules = ModulesField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'name', 'email', 'phone', 'role', 'department', 'department_name', 'is_active',
            'modules', 'password', 'confirm_password', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_department_name(self, user):
        return user.department.name if user.department else None

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        # Lowercasing can lengthen some Unicode characters, so re-check the column limit.
        if len(email) > 254:
            raise serializers.ValidationError('Ensure this field has no more than 254 characters.')
        if User.objects.filter(email=email).exclude(pk=getattr(self.instance, 'pk', None)).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return email

    def validate_department(self, department):
        # An inactive department may stay on a user who already has it, but cannot be newly assigned.
        if department and not department.is_active and department != getattr(self.instance, 'department', None):
            raise serializers.ValidationError('Select an active department.')
        return department

    def validate(self, attrs):
        confirm_password = attrs.pop('confirm_password', None)
        if self.instance is None and not attrs.get('password'):
            raise serializers.ValidationError({'password': 'This field is required.'})

        # Blocks the only ways an admin could lock themselves (or the last admin) out.
        if self.instance is not None and self.instance == self.context['request'].user:
            if getattr(attrs.get('role'), 'pk', Role.ADMIN) != Role.ADMIN or attrs.get('is_active') is False:
                raise serializers.ValidationError(
                    'You cannot remove your own admin role or deactivate your own account.'
                )

        if 'password' in attrs:
            # An unsaved user carrying the incoming name/email, so similarity checks see the new values.
            candidate = User(
                name=attrs.get('name', getattr(self.instance, 'name', '')),
                email=attrs.get('email', getattr(self.instance, 'email', '')),
            )
            try:
                validate_password(attrs['password'], candidate)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': exc.messages})
            if confirm_password != attrs['password']:
                raise serializers.ValidationError({'confirm_password': 'Enter the same password again.'})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        if password:
            instance.set_password(password)
        if getattr(validated_data.get('role'), 'pk', None) == Role.STAFF:
            # A createsuperuser account would otherwise keep every permission through is_superuser.
            instance.is_superuser = instance.is_staff = False
        return super().update(instance, validated_data)


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'name', 'description', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_name(self, value):
        duplicate = Department.objects.filter(name__iexact=value).exclude(pk=getattr(self.instance, 'pk', None))
        if duplicate.exists():
            raise serializers.ValidationError('A department with this name already exists.')
        return value

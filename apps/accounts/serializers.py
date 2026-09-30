from django.contrib.auth.models import Permission
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import Department, Role, User


class PermissionField(serializers.RelatedField):
    """A Django permission as "app_label.codename", the same string user.has_perm() takes."""

    default_error_messages = {'does_not_exist': 'Unknown permission "{value}".'}

    def to_representation(self, value):
        return f'{value.content_type.app_label}.{value.codename}'

    def to_internal_value(self, data):
        # CharField validation rejects non-strings, NUL and surrogate characters before they reach the database.
        app_label, _, codename = serializers.CharField().run_validation(data).partition('.')
        try:
            return self.get_queryset().get(content_type__app_label=app_label, codename=codename)
        except Permission.DoesNotExist:
            self.fail('does_not_exist', value=data)


class UserSerializer(serializers.ModelSerializer):
    # Declared explicitly so uniqueness is checked on the normalized value in validate_email().
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        pk_field=serializers.IntegerField(),
        required=False,
        allow_null=True,
    )
    permissions = PermissionField(
        source='user_permissions',
        many=True,
        required=False,
        queryset=Permission.objects.all(),
    )

    class Meta:
        model = User
        fields = [
            'id', 'name', 'email', 'phone', 'role', 'department', 'is_active',
            'permissions', 'password', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

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
        if self.instance is None and not attrs.get('password'):
            raise serializers.ValidationError({'password': 'This field is required.'})

        # Blocks the only ways an admin could lock themselves (or the last admin) out.
        if self.instance is not None and self.instance == self.context['request'].user:
            if attrs.get('role', Role.ADMIN) != Role.ADMIN or attrs.get('is_active') is False:
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
        return attrs

    def create(self, validated_data):
        permissions = validated_data.pop('user_permissions', [])
        user = User.objects.create_user(**validated_data)
        user.user_permissions.set(permissions)
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        if password:
            instance.set_password(password)
        if validated_data.get('role') == Role.STAFF:
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

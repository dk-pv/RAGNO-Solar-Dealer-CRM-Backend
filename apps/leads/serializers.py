import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import Role

from .models import Lead, LeadSource, LeadStatus, SolarPlan

User = get_user_model()

ORDERINGS = ['name', 'created_at', 'status', 'amount', 'next_follow_up', 'plan__capacity', 'assigned_to__name']


class SolarPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SolarPlan
        fields = ['id', 'name', 'capacity', 'amount', 'is_active']


class LeadSerializer(serializers.ModelSerializer):
    # Declared here so spaces, hyphens and a leading 0 are removed before the digits-only check.
    phone = serializers.CharField(max_length=20)
    plan = serializers.PrimaryKeyRelatedField(queryset=SolarPlan.objects.all(), pk_field=serializers.IntegerField())
    # Optional on create: a new lead without an amount gets its plan's current price.
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'), required=False)
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        pk_field=serializers.IntegerField(),
        required=False,
        allow_null=True,
    )
    plan_name = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    allowed_transitions = serializers.SerializerMethodField()
    # The Work created by converting the lead; null until it is converted.
    work = serializers.PrimaryKeyRelatedField(read_only=True)
    # What the signed-in user may do with this lead, so the screens match what the API allows. Visibility is already
    # limited to their own leads for staff, so these follow from the role's permissions.
    can_edit = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()
    can_assign = serializers.SerializerMethodField()
    can_convert = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'country_code', 'phone', 'email', 'state', 'district', 'area', 'pin_code',
            'plan', 'plan_name', 'amount', 'status', 'allowed_transitions', 'source',
            'assigned_to', 'assigned_to_name', 'next_follow_up', 'notes', 'is_pinned', 'work',
            'created_by_name', 'created_at', 'updated_at',
            'assigned_to', 'assigned_to_name', 'next_follow_up', 'notes', 'is_pinned',
            'can_edit', 'can_delete', 'can_assign', 'can_convert', 'created_by_name', 'created_at', 'updated_at',
        ]
        # The status changes only through the status and convert actions, which apply the pipeline rules.
        read_only_fields = ['id', 'status', 'created_at', 'updated_at']

    def get_plan_name(self, lead):
        return lead.plan.name

    def get_assigned_to_name(self, lead):
        return lead.assigned_to.name if lead.assigned_to else None

    def get_created_by_name(self, lead):
        return lead.created_by.name

    def get_allowed_transitions(self, lead):
        # Someone who can't change leads is offered no moves.
        request = self.context.get('request')
        if request and not request.user.has_perm('leads.change_lead'):
            return []
        return lead.allowed_transitions()

    def validate_phone(self, value):
        number = re.sub(r'[\s().-]', '', value)
        if not re.fullmatch(r'[0-9]+', number):
            raise serializers.ValidationError('Use digits only, without the country code.')
        # A leading 0 is the domestic trunk prefix, not part of the number: 098765 43210 -> 9876543210.
        return number.lstrip('0')

    def validate_plan(self, plan):
        # An inactive plan may stay on a lead that already has it, but can't be newly chosen.
        if not plan.is_active and plan != getattr(self.instance, 'plan', None):
            raise serializers.ValidationError('Select an active plan.')
        return plan

    def get_can_edit(self, lead):
        return self.context['request'].user.has_perm('leads.change_lead')

    def get_can_delete(self, lead):
        return self.context['request'].user.has_perm('leads.delete_lead')

    def get_can_assign(self, lead):
        return self.context['request'].user.role_id == Role.ADMIN

    def get_can_convert(self, lead):
        # Converting needs both: a Won lead (convert() checks the status) and someone who can change it.
        return lead.status == LeadStatus.WON and self.get_can_edit(lead)

    def validate_assigned_to(self, user):
        current = getattr(self.instance, 'assigned_to', None)
        requester = self.context['request'].user
        if requester.role_id != Role.ADMIN:
            # Staff can't hand a lead to someone else: a new lead is theirs, an existing one keeps its assignee.
            keep = current if self.instance else requester
            if user != keep and not (self.instance is None and user is None):
                raise serializers.ValidationError('Only an admin can assign a lead to someone else.')
            return keep
        if user and not user.is_active and user != current:
            raise serializers.ValidationError('Select an active user.')
        return user

    def validate(self, attrs):
        if 'phone' in attrs or 'country_code' in attrs:
            country_code = attrs.get('country_code', getattr(self.instance, 'country_code', '91'))
            phone = attrs.get('phone', getattr(self.instance, 'phone', ''))
            if country_code == '91' and len(phone) != 10:
                raise serializers.ValidationError({'phone': 'Enter the 10-digit phone number.'})
            # E.164: at most 15 digits in all, country code included.
            if not 6 <= len(phone) <= 15 - len(country_code):
                raise serializers.ValidationError({'phone': 'Enter a valid phone number for this country code.'})
        return attrs

    def create(self, validated_data):
        validated_data.setdefault('amount', validated_data['plan'].amount)
        return super().create(validated_data)


class StatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=LeadStatus.choices)


class LeadQuerySerializer(serializers.Serializer):
    """The list's query parameters: an unknown status or a malformed date is a 400, never a silently ignored filter."""

    search = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=LeadStatus.choices, required=False)
    plan = serializers.IntegerField(required=False)
    assigned_to = serializers.IntegerField(required=False)
    source = serializers.ChoiceField(choices=LeadSource.choices, required=False)
    created_after = serializers.DateField(required=False)
    created_before = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=[*ORDERINGS, *(f'-{field}' for field in ORDERINGS)], required=False)

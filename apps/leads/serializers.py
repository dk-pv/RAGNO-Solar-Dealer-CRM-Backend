import re
from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from apps.accounts.models import Role
from apps.accounts.serializers import AssigneesSerializer
from apps.activities.models import Activity, ActivityStatus
from apps.activities.serializers import FollowUpSerializer

from .models import Lead, LeadSource, LeadStatus, SolarPlan

# assigned_to__name: by the first assignee's name (see filter_leads).
ORDERINGS = ['name', 'created_at', 'status', 'amount', 'next_follow_up', 'plan__capacity', 'assigned_to__name']


class SolarPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SolarPlan
        fields = ['id', 'name', 'capacity', 'amount', 'is_active']


class LeadSerializer(AssigneesSerializer, serializers.ModelSerializer):
    """A lead. Only the name and phone number are required. The plan is optional; once a lead has a plan, an amount left
    out (or null) is the plan's current price, from the database. The assignees are any number of active users (see
    AssigneesSerializer); staff can't change them."""

    # Declared here so spaces, hyphens and a leading 0 are removed before the digits-only check.
    phone = serializers.CharField(max_length=20)
    plan = serializers.PrimaryKeyRelatedField(
        queryset=SolarPlan.objects.all(), pk_field=serializers.IntegerField(), required=False, allow_null=True,
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True,
    )
    plan_name = serializers.SerializerMethodField()
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
    # Add Lead can add the lead's first follow-up too: created with the lead in one transaction, and Pending.
    initial_follow_up = FollowUpSerializer(required=False, write_only=True)

    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'country_code', 'phone', 'email', 'state', 'district', 'area', 'pin_code', 'dealer_name',
            'plan', 'plan_name', 'amount', 'status', 'allowed_transitions', 'source',
            'assignee_ids', 'assignees', 'assigned_to', 'assigned_to_name', 'next_follow_up', 'notes', 'is_pinned', 'work',
            'can_edit', 'can_delete', 'can_assign', 'can_convert', 'created_by_name', 'created_at', 'updated_at',
            'initial_follow_up',
        ]
        # The status changes only through the status and convert actions, which apply the pipeline rules.
        read_only_fields = ['id', 'status', 'created_at', 'updated_at']

    def get_plan_name(self, lead):
        return lead.plan.name if lead.plan else None

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
        if plan and not plan.is_active and plan != getattr(self.instance, 'plan', None):
            raise serializers.ValidationError('Select an active plan.')
        return plan

    def get_can_edit(self, lead):
        return self.context['request'].user.has_perm('leads.change_lead')

    def get_can_delete(self, lead):
        return self.context['request'].user.has_perm('leads.delete_lead')

    def get_can_assign(self, lead):
        return self.context['request'].user.role_id == Role.ADMIN

    def get_can_convert(self, lead):
        # Converting needs a Won lead without its Work yet, with a plan and an amount (convert() checks them all), and
        # someone who can change it.
        return (
            lead.status == LeadStatus.WON and getattr(lead, 'work', None) is None
            and lead.plan_id is not None and lead.amount is not None and self.get_can_edit(lead)
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        requester = self.context['request'].user
        if 'assignees' in attrs and requester.role_id != Role.ADMIN:
            # Staff can't hand a lead to someone else: a new lead is theirs (the view assigns it to them), an existing
            # one keeps its assignees.
            keep = self.current_assignees() if self.instance else {requester}
            chosen = set(attrs['assignees'])
            if chosen != keep and not (self.instance is None and not chosen):
                raise serializers.ValidationError({self.assignees_field: 'Only an admin can assign a lead to someone else.'})
        if refused := self.check_assignees(attrs, staff_may_add=[requester]):
            raise serializers.ValidationError({self.assignees_field: refused})
        # A lead with a plan always has an amount: left out, it is the plan's current price (never one made up here).
        plan = attrs['plan'] if 'plan' in attrs else getattr(self.instance, 'plan', None)
        amount = attrs['amount'] if 'amount' in attrs else getattr(self.instance, 'amount', None)
        if plan is not None and amount is None:
            attrs['amount'] = plan.amount
        if 'phone' in attrs or 'country_code' in attrs:
            country_code = attrs.get('country_code', getattr(self.instance, 'country_code', '91'))
            phone = attrs.get('phone', getattr(self.instance, 'phone', ''))
            if country_code == '91' and len(phone) != 10:
                raise serializers.ValidationError({'phone': 'Enter the 10-digit phone number.'})
            # E.164: at most 15 digits in all, country code included.
            if not 6 <= len(phone) <= 15 - len(country_code):
                raise serializers.ValidationError({'phone': 'Enter a valid phone number for this country code.'})
        return attrs

    def validate_initial_follow_up(self, follow_up):
        if self.instance:
            raise serializers.ValidationError("Add follow-ups to an existing lead from its page.")
        return follow_up

    def create(self, validated_data):
        follow_up = validated_data.pop('initial_follow_up', None)
        # Both or neither: a lead is never left without the follow-up it was added with, nor a follow-up without its lead.
        with transaction.atomic():
            lead = super().create(validated_data)
            if follow_up:
                assignees = follow_up.pop('assignees')
                activity = Activity.objects.create(
                    lead=lead, created_by=lead.created_by, status=ActivityStatus.PENDING, **follow_up,
                )
                activity.assignees.set(assignees)
        return lead


class StatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=LeadStatus.choices)


class BulkIdsSerializer(serializers.Serializer):
    """The records a bulk action is for: the ids of the rows selected on one page (the largest page holds 100)."""

    ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=100)

    def validate_ids(self, ids):
        return list(dict.fromkeys(ids))  # each once, in the order sent


class BulkStatusSerializer(BulkIdsSerializer):
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

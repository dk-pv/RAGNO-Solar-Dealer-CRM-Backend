from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import Role
from apps.leads.models import Lead
from apps.works.models import Work

from .models import Activity, ActivityStatus, ActivityType

User = get_user_model()

ORDERINGS = ['created_at', 'due_date', 'status']


class FollowUpSerializer(serializers.ModelSerializer):
    """A follow-up's own fields and rules: a heading, a type, the staff member who does it, a due date and notes.
    Used for follow-ups added to a lead and for the initial follow-up added with a new lead."""

    title = serializers.CharField(max_length=150)
    assigned_to = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), pk_field=serializers.IntegerField())
    due_date = serializers.DateField()
    description = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Activity
        fields = ['title', 'type', 'assigned_to', 'due_date', 'description']

    def validate_assigned_to(self, user):
        # As with leads: an admin assigns any active user; staff assign a follow-up only to themselves (or keep whoever
        # an admin assigned it to).
        requester = self.context['request'].user
        current = getattr(self.instance, 'assigned_to', None)
        if user == current:
            return user
        if requester.role_id != Role.ADMIN and user != requester:
            raise serializers.ValidationError('Only an admin can assign a follow-up to someone else.')
        if not user.is_active:
            raise serializers.ValidationError('Select an active user.')
        return user


class ActivitySerializer(FollowUpSerializer):
    lead = serializers.PrimaryKeyRelatedField(queryset=Lead.objects.none(), pk_field=serializers.IntegerField())
    # The lead, so a list of follow-ups across leads shows whose each one is without asking for every lead.
    lead_name = serializers.CharField(source='lead.name', read_only=True)
    lead_country_code = serializers.CharField(source='lead.country_code', read_only=True)
    lead_phone = serializers.CharField(source='lead.phone', read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    completed_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    # What the signed-in user may do with it, so the screens match what the API allows.
    can_edit = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()
    can_open_lead = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = [
            'id', 'lead', 'lead_name', 'lead_country_code', 'lead_phone',
            'title', 'type', 'type_display', 'assigned_to', 'assigned_to_name', 'due_date', 'description', 'status',
            'created_by_name', 'created_at', 'updated_at', 'can_edit', 'can_delete', 'can_open_lead',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_fields(self):
        fields = super().get_fields()
        # Follow-ups are added only to the requester's own leads; any other id reads as "does not exist".
        fields['lead'].queryset = Lead.objects.visible_to(self.context['request'].user)
        return fields

    def get_assigned_to_name(self, activity):
        return activity.assigned_to.name if activity.assigned_to else None

    def get_created_by_name(self, activity):
        return activity.created_by.name

    def works_the_lead(self, activity):
        user = self.context['request'].user
        return user.role_id == Role.ADMIN or activity.lead.assigned_to_id == user.pk

    def get_can_edit(self, activity):
        return self.context['request'].user.has_perm('leads.change_lead')

    def get_can_delete(self, activity):
        # Whoever works the lead; staff who only do the follow-up can complete and edit it, not delete it.
        return self.get_can_edit(activity) and self.works_the_lead(activity)

    def get_can_open_lead(self, activity):
        return self.works_the_lead(activity)

    def validate_lead(self, lead):
        if self.instance and lead != self.instance.lead:
            raise serializers.ValidationError("A follow-up can't be moved to another lead.")
        return lead

    def validate_status(self, value):
        if self.instance and self.instance.status == ActivityStatus.COMPLETED and value != ActivityStatus.COMPLETED:
            raise serializers.ValidationError("A completed follow-up stays completed.")
        return value


class ActivityQuerySerializer(serializers.Serializer):
    """The list's query parameters: an unknown status or a malformed date is a 400, never a silently ignored filter."""

    lead = serializers.IntegerField(required=False)
    search = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=ActivityStatus.choices, required=False)
    type = serializers.ChoiceField(choices=ActivityType.choices, required=False)
    assigned_to = serializers.IntegerField(required=False)
    due_after = serializers.DateField(required=False)
    due_before = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=[*ORDERINGS, *(f'-{field}' for field in ORDERINGS)], required=False)

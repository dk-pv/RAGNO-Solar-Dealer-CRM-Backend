from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Work, WorkStage

User = get_user_model()

ORDERINGS = ['created_at', 'due_date', 'amount', 'customer_name', 'stage']


class WorkSerializer(serializers.ModelSerializer):
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        pk_field=serializers.IntegerField(),
        required=False,
        allow_null=True,
    )
    plan_name = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    # The Work's activities in brief, for the cards and the list (annotated by the view; the activities API has them all).
    activity_count = serializers.IntegerField(read_only=True)
    pending_activity_count = serializers.IntegerField(read_only=True)
    next_activity_due = serializers.DateField(read_only=True)  # the earliest due date among the pending activities

    class Meta:
        model = Work
        fields = [
            'id', 'lead', 'customer_name', 'country_code', 'phone', 'email', 'state', 'district', 'area', 'pin_code',
            'plan', 'plan_name', 'amount', 'stage', 'assigned_to', 'assigned_to_name', 'due_date', 'is_pinned',
            'activity_count', 'pending_activity_count', 'next_activity_due', 'created_at', 'updated_at',
        ]
        # The customer, plan and confirmed amount are the job's record from conversion: only the pipeline fields and the
        # pin change.
        read_only_fields = [
            'id', 'lead', 'customer_name', 'country_code', 'phone', 'email', 'state', 'district', 'area', 'pin_code',
            'plan', 'amount', 'created_at', 'updated_at',
        ]

    def get_plan_name(self, work):
        return work.plan.name

    def get_assigned_to_name(self, work):
        return work.assigned_to.name if work.assigned_to else None

    def validate_assigned_to(self, user):
        # An inactive user may stay on a Work that already has them, but can't be newly assigned.
        if user and not user.is_active and user != getattr(self.instance, 'assigned_to', None):
            raise serializers.ValidationError('Select an active user.')
        return user


class WorkQuerySerializer(serializers.Serializer):
    """The list's query parameters: an unknown stage or ordering is a 400, never a silently ignored filter."""

    search = serializers.CharField(required=False, allow_blank=True)
    stage = serializers.ChoiceField(choices=WorkStage.choices, required=False)
    assigned_to = serializers.IntegerField(required=False)
    plan = serializers.IntegerField(required=False)
    created_after = serializers.DateField(required=False)
    created_before = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=[*ORDERINGS, *(f'-{field}' for field in ORDERINGS)], required=False)

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from apps.leads.models import Lead
from apps.works.models import Work

from .models import Activity, ActivityStatus, ActivityType

User = get_user_model()


class ActivitySerializer(serializers.ModelSerializer):
    # Exactly one of the two: the lead or the Work the activity belongs to.
    lead = serializers.PrimaryKeyRelatedField(queryset=Lead.objects.none(), pk_field=serializers.IntegerField(), required=False)
    work = serializers.PrimaryKeyRelatedField(queryset=Work.objects.none(), pk_field=serializers.IntegerField(), required=False)
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        pk_field=serializers.IntegerField(),
        required=False,
        allow_null=True,
    )
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    completed_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    # Which Work a Work activity belongs to, for the tables that list several Works' activities; null for a lead's.
    work_summary = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = [
            'id', 'lead', 'work', 'type', 'type_display', 'description', 'assigned_to', 'assigned_to_name', 'due_date',
            'status', 'completed_at', 'completed_by_name', 'created_by_name', 'created_at', 'updated_at', 'work_summary',
        ]
        read_only_fields = ['id', 'completed_at', 'created_at', 'updated_at']

    def get_fields(self):
        fields = super().get_fields()
        user = self.context['request'].user
        # Only what the requester can work on can be chosen; any other id reads as "does not exist".
        # Leads: the requester's own leads (all for an admin). Works: every Work, with the Work module.
        if user.has_perm('leads.change_lead'):
            fields['lead'].queryset = Lead.objects.visible_to(user)
        if user.has_perm('accounts.access_work'):
            fields['work'].queryset = Work.objects.all()
        return fields

    def get_assigned_to_name(self, activity):
        return activity.assigned_to.name if activity.assigned_to else None

    def get_completed_by_name(self, activity):
        return activity.completed_by.name if activity.completed_by else None

    def get_created_by_name(self, activity):
        return activity.created_by.name

    def get_work_summary(self, work_activity):
        work = work_activity.work
        if work is None:
            return None
        return {
            'id': work.id, 'customer_name': work.customer_name, 'country_code': work.country_code, 'phone': work.phone,
            'plan_name': work.plan.name, 'stage': work.stage,
        }

    def validate_lead(self, lead):
        if self.instance and lead != self.instance.lead:
            raise serializers.ValidationError("An activity can't be moved to another lead.")
        return lead

    def validate_work(self, work):
        if self.instance and work != self.instance.work:
            raise serializers.ValidationError("An activity can't be moved to another Work.")
        return work

    def validate_assigned_to(self, user):
        # An inactive user may stay on an activity that already has them, but can't be newly assigned.
        if user and not user.is_active and user != getattr(self.instance, 'assigned_to', None):
            raise serializers.ValidationError('Select an active user.')
        return user

    def validate(self, attrs):
        if self.instance is None and bool(attrs.get('lead')) == bool(attrs.get('work')):
            raise serializers.ValidationError('Choose the lead or the Work this activity is for.')
        # Completing records when and by whom; reopening clears it.
        status = attrs.get('status')
        if status and status != getattr(self.instance, 'status', ActivityStatus.PENDING):
            completed = status == ActivityStatus.COMPLETED
            attrs['completed_at'] = timezone.now() if completed else None
            attrs['completed_by'] = self.context['request'].user if completed else None
        return attrs


WORK_ACTIVITY_ORDERINGS = ['-work', 'work', 'customer_name', 'due_date']


class WorkActivityQuerySerializer(serializers.Serializer):
    """The Work Activities page's query parameters: an unknown status or a malformed date is a 400, never ignored."""

    search = serializers.CharField(required=False, allow_blank=True)
    work = serializers.IntegerField(required=False)
    assigned_to = serializers.IntegerField(required=False)
    type = serializers.ChoiceField(choices=ActivityType.choices, required=False)
    status = serializers.ChoiceField(choices=ActivityStatus.choices, required=False)
    due_after = serializers.DateField(required=False)
    due_before = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=WORK_ACTIVITY_ORDERINGS, required=False)

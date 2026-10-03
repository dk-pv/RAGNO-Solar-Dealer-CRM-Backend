from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import Role
from apps.leads.models import Lead
from apps.works.models import Work

from .models import Activity, ActivityStatus, ActivityType

User = get_user_model()

ORDERINGS = ['created_at', 'due_date', 'status']


def check_follow_up_assignee(user, requester, current):
    """A lead's follow-up is assigned as leads are: an admin assigns any active user; staff assign one only to themselves
    (or keep whoever an admin assigned it to)."""
    if user == current:
        return
    if requester.role_id != Role.ADMIN and user != requester:
        raise serializers.ValidationError('Only an admin can assign a follow-up to someone else.')
    if not user.is_active:
        raise serializers.ValidationError('Select an active user.')


class FollowUpSerializer(serializers.ModelSerializer):
    """A lead's follow-up's own fields and rules: a heading, a type, the staff member who does it, a due date and notes.
    Used for the initial follow-up added with a new lead; ActivitySerializer applies the same rules to a lead's
    follow-ups."""

    title = serializers.CharField(max_length=150)
    assigned_to = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), pk_field=serializers.IntegerField())
    due_date = serializers.DateField()
    description = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Activity
        fields = ['title', 'type', 'assigned_to', 'due_date', 'description']

    def validate_assigned_to(self, user):
        check_follow_up_assignee(user, self.context['request'].user, getattr(self.instance, 'assigned_to', None))
        return user


class ActivitySerializer(serializers.ModelSerializer):
    """One activity of the log: a lead's follow-up or a Work's activity, each kind with its own rules.
    - A lead's follow-up needs a heading, its staff (assigned as leads are) and a due date; notes are optional. It starts
      Pending whatever the request says, and Completed is final.
    - A Work's activity needs notes; its staff and due date are optional. It can be added completed, and reopened.
    Completing records when and by whom; reopening clears it."""

    # Exactly one of the two, chosen when it is added: the lead or the Work it belongs to.
    lead = serializers.PrimaryKeyRelatedField(queryset=Lead.objects.none(), pk_field=serializers.IntegerField(), required=False)
    work = serializers.PrimaryKeyRelatedField(queryset=Work.objects.none(), pk_field=serializers.IntegerField(), required=False)
    # Which fields are required depends on the kind; validate() checks them.
    title = serializers.CharField(max_length=150, required=False, allow_blank=True)
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), pk_field=serializers.IntegerField(), required=False, allow_null=True,
    )
    due_date = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True)
    # The lead, so a list of follow-ups across leads shows whose each one is without asking for every lead. Null for a
    # Work's activity, which has work_summary instead.
    lead_name = serializers.CharField(source='lead.name', read_only=True, allow_null=True)
    lead_country_code = serializers.CharField(source='lead.country_code', read_only=True, allow_null=True)
    lead_phone = serializers.CharField(source='lead.phone', read_only=True, allow_null=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    completed_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    # Which Work a Work activity belongs to, for the tables that list several Works' activities; null for a lead's.
    work_summary = serializers.SerializerMethodField()
    # What the signed-in user may do with it, so the screens match what the API allows.
    can_edit = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()
    can_open_lead = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = [
            'id', 'lead', 'work', 'lead_name', 'lead_country_code', 'lead_phone',
            'title', 'type', 'type_display', 'assigned_to', 'assigned_to_name', 'due_date', 'description', 'status',
            'completed_at', 'completed_by_name', 'created_by_name', 'created_at', 'updated_at', 'work_summary',
            'can_edit', 'can_delete', 'can_open_lead',
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

    def get_work_summary(self, activity):
        work = activity.work
        if work is None:
            return None
        return {
            'id': work.id, 'customer_name': work.customer_name, 'country_code': work.country_code, 'phone': work.phone,
            'plan_name': work.plan.name, 'stage': work.stage,
        }

    def works_the_lead(self, activity):
        user = self.context['request'].user
        return user.role_id == Role.ADMIN or activity.lead.assigned_to_id == user.pk

    def get_can_edit(self, activity):
        permission = 'accounts.access_work' if activity.work_id else 'leads.change_lead'
        return self.context['request'].user.has_perm(permission)

    def get_can_delete(self, activity):
        # A Work's activities are kept as its history. A lead's follow-up is deleted by whoever works the lead; staff
        # who only do the follow-up can complete and edit it, not delete it.
        return activity.lead_id is not None and self.get_can_edit(activity) and self.works_the_lead(activity)

    def get_can_open_lead(self, activity):
        return activity.lead_id is not None and self.works_the_lead(activity)

    def validate_lead(self, lead):
        if self.instance and lead != self.instance.lead:
            raise serializers.ValidationError("A follow-up can't be moved to another lead.")
        return lead

    def validate_work(self, work):
        if self.instance and work != self.instance.work:
            raise serializers.ValidationError("An activity can't be moved to another Work.")
        return work

    def validate(self, attrs):
        if self.instance is None:
            if bool(attrs.get('lead')) == bool(attrs.get('work')):
                raise serializers.ValidationError('Choose the lead or the Work this activity is for.')
            on_work = attrs.get('work') is not None
        else:
            on_work = self.instance.work_id is not None
        errors = self.check_work_activity(attrs) if on_work else self.check_follow_up(attrs)
        if errors:
            raise serializers.ValidationError(errors)
        # Completing records when and by whom; reopening (a Work's activity) clears it.
        status = attrs.get('status')
        if status and status != getattr(self.instance, 'status', ActivityStatus.PENDING):
            completed = status == ActivityStatus.COMPLETED
            attrs['completed_at'] = timezone.now() if completed else None
            attrs['completed_by'] = self.context['request'].user if completed else None
        return attrs

    def check_follow_up(self, attrs):
        """A lead's follow-up: the field errors, if any."""
        errors = {}
        creating = self.instance is None
        for field, empty in (('title', 'This field may not be blank.'), ('assigned_to', 'This field may not be null.'),
                             ('due_date', 'This field may not be null.')):
            if field in attrs:
                if attrs[field] in (None, ''):
                    errors[field] = [empty]
            elif creating:
                errors[field] = ['This field is required.']
        if attrs.get('assigned_to'):
            try:
                check_follow_up_assignee(
                    attrs['assigned_to'], self.context['request'].user, getattr(self.instance, 'assigned_to', None),
                )
            except serializers.ValidationError as error:
                errors['assigned_to'] = error.detail
        if creating:
            # Every new follow-up starts Pending, whatever the request says; it is completed by changing its status after.
            attrs['status'] = ActivityStatus.PENDING
        elif self.instance.status == ActivityStatus.COMPLETED and attrs.get('status', ActivityStatus.COMPLETED) != ActivityStatus.COMPLETED:
            errors['status'] = ['A completed follow-up stays completed.']
        return errors

    def check_work_activity(self, attrs):
        """A Work's activity: the field errors, if any."""
        errors = {}
        if 'description' in attrs:
            if not attrs['description']:
                errors['description'] = ['This field may not be blank.']
        elif self.instance is None:
            errors['description'] = ['This field is required.']
        # An inactive user may stay on an activity that already has them, but can't be newly assigned.
        user = attrs.get('assigned_to')
        if user and not user.is_active and user != getattr(self.instance, 'assigned_to', None):
            errors['assigned_to'] = ['Select an active user.']
        return errors


class ActivityQuerySerializer(serializers.Serializer):
    """The lead follow-ups list's query parameters: an unknown status or a malformed date is a 400, never a silently
    ignored filter."""

    lead = serializers.IntegerField(required=False)
    search = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=ActivityStatus.choices, required=False)
    type = serializers.ChoiceField(choices=ActivityType.choices, required=False)
    assigned_to = serializers.IntegerField(required=False)
    due_after = serializers.DateField(required=False)
    due_before = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=[*ORDERINGS, *(f'-{field}' for field in ORDERINGS)], required=False)


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

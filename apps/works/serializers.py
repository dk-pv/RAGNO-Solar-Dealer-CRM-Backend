from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.leads.serializers import BulkIdsSerializer  # noqa: F401 (the Works views take it from here)

from .documents import summarize
from .models import Work, WorkDocument, WorkStage

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
    # What the customer pays: offer_amount when an admin applied an offer, otherwise `amount`, the standard price.
    effective_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    # The Work's activities in brief, for the cards and the list (annotated by the view; the activities API has them all).
    activity_count = serializers.IntegerField(read_only=True)
    pending_activity_count = serializers.IntegerField(read_only=True)
    next_activity_due = serializers.DateField(read_only=True)  # the earliest due date among the pending activities
    # How complete the Work's required documents are (documents.summarize), for the list, the board and the Work.
    document_summary = serializers.SerializerMethodField()

    class Meta:
        model = Work
        fields = [
            'id', 'lead', 'customer_name', 'country_code', 'phone', 'email', 'state', 'district', 'area', 'pin_code',
            'plan', 'plan_name', 'amount', 'offer_amount', 'offer_note', 'effective_amount', 'stage', 'assigned_to',
            'assigned_to_name', 'due_date', 'is_pinned', 'activity_count', 'pending_activity_count',
            'next_activity_due', 'document_summary', 'created_at', 'updated_at',
        ]
        # The customer, plan and confirmed amount are the job's record from conversion: only the pipeline fields, the
        # pin and the email change. The email is one of the required documents (Email ID), so a Work converted without
        # one can still be completed; it is validated as a lead's is. An offer changes only through the offer action
        # (admins).
        read_only_fields = [
            'id', 'lead', 'customer_name', 'country_code', 'phone', 'state', 'district', 'area', 'pin_code',
            'plan', 'amount', 'offer_amount', 'offer_note', 'created_at', 'updated_at',
        ]

    def get_plan_name(self, work):
        return work.plan.name

    def get_assigned_to_name(self, work):
        return work.assigned_to.name if work.assigned_to else None

    def get_document_summary(self, work):
        return summarize(work)

    def validate_assigned_to(self, user):
        # An inactive user may stay on a Work that already has them, but can't be newly assigned.
        if user and not user.is_active and user != getattr(self.instance, 'assigned_to', None):
            raise serializers.ValidationError('Select an active user.')
        return user


class OfferSerializer(serializers.Serializer):
    """An admin's offer for one Work (the offer action): a special price above zero and at most the Work's standard
    amount, with an optional note on why. A null offer_amount removes the offer. `expected_offer_amount` is the offer
    the admin saw when deciding (null: none); if someone changed it meanwhile, the request is refused rather than
    overwriting their decision. The context's `work` is the Work, locked."""

    offer_amount = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    offer_note = serializers.CharField(max_length=1000, allow_blank=True, default='')
    expected_offer_amount = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True, required=False)

    def validate_offer_amount(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError('Enter an offer price greater than zero.')
        if value is not None and value > self.context['work'].amount:
            raise serializers.ValidationError("The offer price can't be more than the Work's standard amount.")
        return value

    def validate(self, attrs):
        if attrs['offer_amount'] is None:
            attrs['offer_note'] = ''  # no offer, no note
        return attrs


class BulkStageSerializer(BulkIdsSerializer):
    stage = serializers.ChoiceField(choices=WorkStage.choices)


class WorkQuerySerializer(serializers.Serializer):
    """The list's query parameters: an unknown stage or ordering is a 400, never a silently ignored filter."""

    search = serializers.CharField(required=False, allow_blank=True)
    stage = serializers.ChoiceField(choices=WorkStage.choices, required=False)
    assigned_to = serializers.IntegerField(required=False)
    plan = serializers.IntegerField(required=False)
    created_after = serializers.DateField(required=False)
    created_before = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=[*ORDERINGS, *(f'-{field}' for field in ORDERINGS)], required=False)


class WorkDocumentSerializer(serializers.ModelSerializer):
    """An uploaded document as the Documents page shows it. Where the file is stored is never sent."""

    uploaded_by_name = serializers.CharField(source='uploaded_by.name', read_only=True)

    class Meta:
        model = WorkDocument
        fields = ['original_filename', 'content_type', 'file_size', 'uploaded_at', 'uploaded_by_name']

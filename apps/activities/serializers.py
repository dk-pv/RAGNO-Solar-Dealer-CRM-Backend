from rest_framework import serializers

from apps.leads.models import Lead

from .models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    lead = serializers.PrimaryKeyRelatedField(queryset=Lead.objects.none(), pk_field=serializers.IntegerField())
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = ['id', 'lead', 'type', 'type_display', 'description', 'created_by_name', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_fields(self):
        fields = super().get_fields()
        # Only the requester's own leads can be chosen; any other id reads as "does not exist".
        fields['lead'].queryset = Lead.objects.visible_to(self.context['request'].user)
        return fields

    def get_created_by_name(self, activity):
        return activity.created_by.name

    def validate_lead(self, lead):
        if self.instance and lead != self.instance.lead:
            raise serializers.ValidationError("An activity can't be moved to another lead.")
        return lead

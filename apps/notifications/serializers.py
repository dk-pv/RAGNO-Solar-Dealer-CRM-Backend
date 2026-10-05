from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        # The lead, Work and activity it is about, as ids: the screen builds the link from them.
        fields = ['id', 'kind', 'title', 'message', 'lead', 'work', 'activity', 'is_read', 'read_at', 'created_at']
        read_only_fields = fields

    def get_is_read(self, notification):
        return notification.read_at is not None

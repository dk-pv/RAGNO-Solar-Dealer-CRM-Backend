from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdminRole

from .reset import crm_record_counts, reset_crm_data

# Typed by the admin, so the reset can't be triggered by a stray or replayed request.
CONFIRMATION = 'RESET CRM DATA'


class ResetSerializer(serializers.Serializer):
    confirm = serializers.CharField()

    def validate_confirm(self, value):
        if value.strip() != CONFIRMATION:
            raise serializers.ValidationError(f'Type {CONFIRMATION} to confirm.')
        return value


class CrmResetView(APIView):
    """Admins only (the project default, stated here on purpose). GET says what a reset would delete; POST deletes it."""

    permission_classes = [IsAdminRole]

    def get(self, request):
        return Response({'counts': crm_record_counts(), 'confirmation': CONFIRMATION})

    def post(self, request):
        body = ResetSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response({'deleted': reset_crm_data(request.user)})

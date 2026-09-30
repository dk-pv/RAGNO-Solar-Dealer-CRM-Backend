from django.db.models import ProtectedError
from rest_framework import mixins, status, viewsets
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Department, User
from .permissions import IsAdminRole, ModelPermissions
from .serializers import DepartmentSerializer, UserSerializer


class MeView(RetrieveAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    # No destroy: users are deactivated (is_active=false), never deleted, so business history stays attributable.
    queryset = User.objects.prefetch_related('user_permissions__content_type').order_by('name', 'id')
    serializer_class = UserSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [ModelPermissions()]
        # Any user write can grant a role or permissions, so writes stay ADMIN-only
        # even for staff who hold add_user/change_user.
        return [IsAdminRole()]


class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.order_by('name')
    serializer_class = DepartmentSerializer
    permission_classes = [ModelPermissions]

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {'detail': 'This department has users assigned. Deactivate it instead of deleting it.'},
                status=status.HTTP_409_CONFLICT,
            )

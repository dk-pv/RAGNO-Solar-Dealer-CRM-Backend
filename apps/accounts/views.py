from django.db.models import ProtectedError
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import Department, Role, User
from .permissions import MODULES, IsAdminRole, ModelPermissions
from .serializers import DepartmentSerializer, RoleSerializer, UserSerializer


class LoginView(TokenObtainPairView):
    """Sign-in, limited to a few attempts a minute per address (the 'login' rate in settings) against password guessing."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'


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
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    # Only STAFF with no CRM records can be deleted. Anyone with records (leads they created or are assigned) is
    # deactivated instead, so business history stays attributable; the database's PROTECT keys enforce it.
    queryset = (
        User.objects.select_related('role', 'department')
        .prefetch_related('role__permissions__content_type')
        .order_by('name', 'id')
    )
    serializer_class = UserSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'modules'):
            return [ModelPermissions()]
        # Any user write can assign a role, so writes stay ADMIN-only even for staff who hold add_user/change_user.
        return [IsAdminRole()]

    @action(detail=False)
    def modules(self, request):
        """The modules access can be given to, in display order."""
        return Response([{'key': key, 'label': module['label']} for key, module in MODULES.items()])

    def destroy(self, request, *args, **kwargs):
        # Also keeps an admin from deleting themselves.
        if self.get_object().role_id != Role.STAFF:
            return Response({'detail': "Admins can't be deleted."}, status=status.HTTP_409_CONFLICT)
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {'detail': 'This user has records in the CRM, such as leads. Deactivate them instead of deleting them.'},
                status=status.HTTP_409_CONFLICT,
            )


class RoleViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    # ADMIN-only through the project default. The two roles are fixed: they can't be created or deleted.
    queryset = Role.objects.prefetch_related('permissions__content_type').order_by('name')
    serializer_class = RoleSerializer
    pagination_class = None


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

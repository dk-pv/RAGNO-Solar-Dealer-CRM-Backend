from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import LeadViewSet, SolarPlanListView

router = SimpleRouter()
router.register('leads', LeadViewSet, basename='lead')

urlpatterns = [
    path('plans/', SolarPlanListView.as_view(), name='plan-list'),
    *router.urls,
]

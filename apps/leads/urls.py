from rest_framework.routers import SimpleRouter

from .views import LeadViewSet, SolarPlanViewSet

router = SimpleRouter()
router.register('leads', LeadViewSet, basename='lead')
router.register('plans', SolarPlanViewSet, basename='plan')

urlpatterns = router.urls

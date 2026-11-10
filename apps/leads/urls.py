from rest_framework.routers import SimpleRouter

from .views import LeadViewSet, PipelineStageViewSet, SolarPlanViewSet

router = SimpleRouter()
router.register('leads', LeadViewSet, basename='lead')
router.register('plans', SolarPlanViewSet, basename='plan')
router.register('pipeline-stages', PipelineStageViewSet, basename='pipeline-stage')

urlpatterns = router.urls

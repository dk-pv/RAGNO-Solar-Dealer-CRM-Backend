from rest_framework.routers import SimpleRouter

from .views import WorkViewSet

router = SimpleRouter()
router.register('works', WorkViewSet, basename='work')

urlpatterns = router.urls

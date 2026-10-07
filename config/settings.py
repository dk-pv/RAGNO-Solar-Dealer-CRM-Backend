"""
Django settings for the Ragno Power System CRM backend.

Secrets and environment-specific values come from environment variables;
backend/.env.example lists every variable.
"""

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f'The {name} environment variable is required.')
    return value


def env_list(name, default):
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


SECRET_KEY = required_env('DJANGO_SECRET_KEY')

DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() in ('1', 'true')

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1')

# Production (DJANGO_DEBUG off) runs behind an HTTPS proxy, such as Render's, that passes requests on over HTTP:
# its X-Forwarded-Proto header tells Django the visitor's connection was HTTPS. The CRM signs requests with JWTs, not
# cookies; the cookies the Django admin uses are sent over HTTPS only.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'apps.accounts',
    'apps.leads',
    'apps.works',
    'apps.activities',
    'apps.dashboard',
    'apps.reports',
    'apps.notifications',
    'apps.maintenance',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': required_env('POSTGRES_DB'),
        'USER': required_env('POSTGRES_USER'),
        'PASSWORD': required_env('POSTGRES_PASSWORD'),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),

        'OPTIONS': {
            'sslmode': 'require',
        },

        # One transaction per request: a failed request never leaves half-written business records.
        'ATOMIC_REQUESTS': True,
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Authentication

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
        'OPTIONS': {'user_attributes': ('email', 'name')},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# API

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    # Deny by default: an endpoint that forgets its permission_classes is ADMIN-only, never open.
    'DEFAULT_PERMISSION_CLASSES': [
        'apps.accounts.permissions.IsAdminRole',
    ],
    # JSON only: form parsing turns an omitted boolean into False, which would silently deactivate records.
    # A future file-upload view can enable MultiPartParser on that view alone.
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    # Sign-in attempts per address (apps.accounts.views.LoginView), so a password can't be guessed by brute force.
    # Every other endpoint needs a valid token. Counted per process: a multi-worker server allows a few times this.
    'DEFAULT_THROTTLE_RATES': {'login': '10/min'},
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    # A password change (e.g. an admin resetting a compromised account) invalidates every earlier token.
    'CHECK_REVOKE_TOKEN': True,
}

CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS', 'http://localhost:3000')


# Work documents (apps/works/storage.py) are kept privately in Cloudinary. Not required to start: until all three are
# set, uploads and downloads fail with a message in the server log, and the rest of the CRM works. Django's error pages
# hide settings named like *_KEY and *_SECRET.
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')


# Logging

# Errors go to the server's output (which gunicorn and the host capture). Django's defaults send them only by email to
# ADMINS, which isn't configured, so a production 500 would otherwise leave no trace. Request bodies and secrets are
# never logged: only the exception and the request path.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'console': {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'console'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {
        # 5xx responses with their traceback; 4xx are normal and stay quiet.
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        # Refused hosts and the like: a misconfigured ALLOWED_HOSTS shows up here.
        'django.security': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
    },
}


# Internationalization

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kolkata'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = 'static/'

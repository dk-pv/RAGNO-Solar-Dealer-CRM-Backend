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
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    # A password change (e.g. an admin resetting a compromised account) invalidates every earlier token.
    'CHECK_REVOKE_TOKEN': True,
}

CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS', 'http://localhost:3000')


# Internationalization

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kolkata'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = 'static/'

from pathlib import Path
import os
import sys

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(os.path.join(BASE_DIR, 'aicaller'))

load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-#n+(li_)zj2l&6fekaqkr0fn@2-(_tc8@&cuz0^b*etkw0w6ps")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"

HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "")
HF_MODEL_NAME = os.getenv("HF_MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct")

# Local dev: your ngrok URL. Production (Railway): set BASE_URL to the
# deployed https URL, e.g. https://your-app.up.railway.app
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# RestoPOS Supabase project, used to fetch the live menu (rpc/voice_get_menu)
# and submit confirmed orders (rpc/voice_create_order) -- same project and
# api_key pattern as the ai-calling-agent (Gemini Live) voice agent.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
RESTAURANT_ID = os.getenv("RESTAURANT_ID", "")
RESTAURANT_NAME = os.getenv("RESTAURANT_NAME", "")
ORDER_WEBHOOK_API_KEY = os.getenv("ORDER_WEBHOOK_API_KEY", "")
ORDER_WEBHOOK_URL = os.getenv(
    "ORDER_WEBHOOK_URL",
    "https://lxurfpnlvmrvarwbzygl.supabase.co/functions/v1/voice-order-webhook",
)

_allowed_hosts = os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts.split(",") if h.strip()]
# Railway's domain isn't known until deploy time; trust it automatically.
ALLOWED_HOSTS.append(".up.railway.app")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "aicaller.apps.AicallerConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "aicaller.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [Path.joinpath(BASE_DIR, "aicaller/templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "aicaller.wsgi.application"

# Database
# https://docs.djangoproject.com/en/5.0/ref/settings/#databases

DATABASES = {
    # Railway provides DATABASE_URL automatically once a Postgres plugin is
    # attached; without it (local dev) this falls back to the SQLite file.
    # SQLite alone is not durable on Railway -- its filesystem is ephemeral
    # across redeploys, so leads/call history would vanish on every deploy.
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
    )
}


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/

STATIC_URL = "static/"

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
